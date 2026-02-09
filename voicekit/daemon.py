"""VoiceKit daemon — main application loop.

Orchestrates platform adapters, AI providers, and the audio router.
Handles graceful startup, shutdown, signal handling, and health checks.
"""

from __future__ import annotations

import asyncio
import json
import logging
import signal
import sys
import time
from typing import Any

from voicekit.config import VoiceKitConfig
from voicekit.core.events import Event, EventBus, EventType
from voicekit.core.router import AudioRouter
from voicekit.platforms.base import PlatformAdapter
from voicekit.providers.base import VoiceProvider
from voicekit.providers.openai_realtime import OpenAIRealtimeProvider

logger = logging.getLogger(__name__)


def _create_single_provider(provider_type: str, provider_config: Any) -> VoiceProvider:
    """Create a single AI voice provider by type name."""
    if provider_type == "openai_realtime":
        return OpenAIRealtimeProvider(provider_config)
    elif provider_type == "google_gemini":
        from voicekit.providers.google_gemini import GeminiLiveProvider

        return GeminiLiveProvider(provider_config)
    elif provider_type == "elevenlabs":
        from voicekit.providers.elevenlabs import ElevenLabsProvider

        return ElevenLabsProvider(provider_config)
    elif provider_type == "deepgram":
        from voicekit.providers.deepgram import DeepgramVoiceAgentProvider

        return DeepgramVoiceAgentProvider(provider_config)
    elif provider_type == "anthropic_claude":
        from voicekit.providers.anthropic_claude import AnthropicClaudeProvider

        return AnthropicClaudeProvider(provider_config)
    else:
        raise ValueError(f"Unknown provider type: {provider_type}")


def _create_provider(config: VoiceKitConfig) -> VoiceProvider:
    """Create an AI voice provider from configuration.

    If failover_providers are configured, wraps them in a FailoverProvider.
    """
    primary = _create_single_provider(config.provider.type, config.provider)

    if not config.provider.failover_providers:
        return primary

    from voicekit.providers.failover import FailoverProvider

    providers: list[VoiceProvider] = [primary]
    for fp in config.provider.failover_providers:
        providers.append(_create_single_provider(fp.type, fp))

    return FailoverProvider(providers)


def _create_platforms(config: VoiceKitConfig) -> list[PlatformAdapter]:
    """Create enabled platform adapters from configuration."""
    platforms: list[PlatformAdapter] = []

    if config.platforms.virtual_audio.enabled:
        from voicekit.platforms.virtual_audio import VirtualAudioPlatform

        platforms.append(VirtualAudioPlatform(config.platforms.virtual_audio))

    if config.platforms.telegram.enabled:
        from voicekit.platforms.telegram import TelegramPlatform

        platforms.append(TelegramPlatform(config.platforms.telegram))

    if config.platforms.discord.enabled:
        from voicekit.platforms.discord import DiscordPlatform

        platforms.append(DiscordPlatform(config.platforms.discord))

    if config.platforms.zoom.enabled:
        from voicekit.platforms.zoom import ZoomPlatform

        platforms.append(ZoomPlatform(config.platforms.zoom))

    if config.platforms.whatsapp.enabled:
        from voicekit.platforms.whatsapp import WhatsAppPlatform

        platforms.append(WhatsAppPlatform(config.platforms.whatsapp))

    if config.platforms.signal.enabled:
        from voicekit.platforms.signal import SignalPlatform

        platforms.append(SignalPlatform(config.platforms.signal))

    if config.platforms.slack.enabled:
        from voicekit.platforms.slack import SlackPlatform

        platforms.append(SlackPlatform(config.platforms.slack))

    if config.platforms.sip.enabled:
        from voicekit.platforms.sip import SipPlatform

        platforms.append(SipPlatform(config.platforms.sip))

    if config.platforms.teams.enabled:
        from voicekit.platforms.teams import TeamsPlatform

        platforms.append(TeamsPlatform(config.platforms.teams))

    if config.platforms.webrtc.enabled:
        from voicekit.platforms.webrtc import WebRTCPlatform

        platforms.append(WebRTCPlatform(config.platforms.webrtc))

    if config.platforms.google_meet.enabled:
        from voicekit.platforms.google_meet import GoogleMeetPlatform

        platforms.append(GoogleMeetPlatform(config.platforms.google_meet))

    if config.platforms.facetime.enabled:
        from voicekit.platforms.facetime import FaceTimePlatform

        platforms.append(FaceTimePlatform(config.platforms.facetime))

    return platforms


def _create_middleware_pipeline(config: VoiceKitConfig) -> Any:
    """Create audio middleware pipeline from configuration."""
    from voicekit.core.middleware import (
        AudioPipeline,
        AudioRecorder,
        EchoCanceller,
        NoiseGate,
        RateLimiter,
        TranscriptLogger,
    )

    pipeline = AudioPipeline()
    mw = config.middleware

    if mw.echo_cancellation:
        pipeline.add(EchoCanceller(tail_ms=mw.echo_tail_ms))
        logger.info("Middleware: echo cancellation enabled (tail=%dms)", mw.echo_tail_ms)

    if mw.noise_gate:
        pipeline.add(NoiseGate(threshold_db=mw.noise_gate_threshold_db))
        logger.info("Middleware: noise gate enabled (threshold=%.0fdB)", mw.noise_gate_threshold_db)

    if mw.recording:
        pipeline.add(AudioRecorder(output_dir=mw.recording_dir))
        logger.info("Middleware: audio recording enabled → %s", mw.recording_dir)

    if mw.transcript_logging:
        pipeline.add(TranscriptLogger(output_dir=mw.transcript_dir, fmt=mw.transcript_format))
        logger.info("Middleware: transcript logging enabled → %s", mw.transcript_dir)

    if mw.rate_limiting:
        pipeline.add(RateLimiter(max_bytes_per_second=mw.rate_limit_bytes_per_second))
        logger.info(
            "Middleware: rate limiting enabled (%d B/s)", mw.rate_limit_bytes_per_second
        )

    return pipeline


class VoiceKitDaemon:
    """Main VoiceKit daemon.

    Manages the lifecycle of all components: provider, platforms,
    event bus, audio router, and health check endpoint.
    """

    def __init__(self, config: VoiceKitConfig) -> None:
        self.config = config
        self.event_bus = EventBus()
        self.router = AudioRouter(self.event_bus)
        self.provider: VoiceProvider | None = None
        self.platforms: list[PlatformAdapter] = []
        self._shutdown_event = asyncio.Event()
        self._start_time: float = 0.0
        self._health_runner: Any = None
        self._middleware_pipeline: Any = None
        self._conversation_store: Any = None

    async def start(self) -> None:
        """Start the daemon: connect provider, start platforms, begin routing."""
        self._start_time = time.monotonic()
        self._setup_logging()
        self._setup_signals()

        logger.info("VoiceKit daemon starting...")

        # Register event logging
        self.event_bus.on_all(self._log_event)

        # Start health check endpoint
        if self.config.daemon.health_port:
            await self._start_health_server()

        # Set up middleware pipeline
        self._middleware_pipeline = _create_middleware_pipeline(self.config)
        if self._middleware_pipeline._middleware:
            await self._middleware_pipeline.start()
            self.router.set_middleware(self._middleware_pipeline)
            logger.info(
                "Audio middleware pipeline active (%d stages)",
                len(self._middleware_pipeline._middleware),
            )

        # Set up conversation persistence
        if self.config.persistence.enabled:
            from voicekit.core.persistence import ConversationStore

            self._conversation_store = ConversationStore(
                storage_dir=self.config.persistence.storage_dir,
                max_history=self.config.persistence.max_history,
                ttl_hours=self.config.persistence.ttl_hours,
            )
            logger.info(
                "Conversation persistence enabled → %s",
                self.config.persistence.storage_dir,
            )

        # Create and connect provider
        self.provider = _create_provider(self.config)
        try:
            await self.provider.connect()
            await self.event_bus.emit(Event(type=EventType.PROVIDER_CONNECTED))
        except Exception:
            logger.exception("Failed to connect to AI provider")
            raise

        # Create and start platforms
        self.platforms = _create_platforms(self.config)

        if not self.platforms:
            logger.warning("No platforms enabled. Enable at least one platform in config.")

        for platform in self.platforms:
            try:
                await platform.start()
                await self.event_bus.emit(
                    Event(type=EventType.PLATFORM_STARTED, platform=platform.name)
                )

                # Set up audio routing
                await self.router.add_route(platform, self.provider)

            except Exception:
                logger.exception("Failed to start platform %s", platform.name)
                await self.event_bus.emit(
                    Event(
                        type=EventType.PLATFORM_ERROR,
                        platform=platform.name,
                        data={"error": "start_failed"},
                    )
                )

        await self.event_bus.emit(Event(type=EventType.DAEMON_STARTED))

        active_platforms = [p.name for p in self.platforms if p.is_active]
        logger.info(
            "VoiceKit daemon running with %d active platform(s): %s",
            len(active_platforms),
            ", ".join(active_platforms) if active_platforms else "none",
        )

        # Wait for shutdown signal
        await self._shutdown_event.wait()

    async def stop(self) -> None:
        """Gracefully stop the daemon."""
        logger.info("VoiceKit daemon stopping...")
        await self.event_bus.emit(Event(type=EventType.DAEMON_STOPPING))

        # Stop health check server
        if self._health_runner:
            await self._health_runner.cleanup()
            self._health_runner = None

        # Stop middleware pipeline
        if self._middleware_pipeline and self._middleware_pipeline._middleware:
            await self._middleware_pipeline.stop()

        # Stop audio routing
        await self.router.stop_all()

        # Stop platforms
        for platform in self.platforms:
            try:
                await platform.stop()
                await self.event_bus.emit(
                    Event(type=EventType.PLATFORM_STOPPED, platform=platform.name)
                )
            except Exception:
                logger.exception("Error stopping platform %s", platform.name)

        # Disconnect provider
        if self.provider:
            try:
                await self.provider.disconnect()
                await self.event_bus.emit(Event(type=EventType.PROVIDER_DISCONNECTED))
            except Exception:
                logger.exception("Error disconnecting provider")

        logger.info("VoiceKit daemon stopped")

    async def _start_health_server(self) -> None:
        """Start a lightweight HTTP health check endpoint."""
        try:
            from aiohttp import web
        except ImportError:
            logger.debug("aiohttp not installed — health check endpoint disabled")
            return

        app = web.Application()
        app.router.add_get("/health", self._health_handler)
        app.router.add_get("/status", self._status_handler)

        self._health_runner = web.AppRunner(app)
        await self._health_runner.setup()
        site = web.TCPSite(
            self._health_runner, "0.0.0.0", self.config.daemon.health_port
        )
        await site.start()
        logger.info("Health check endpoint at http://0.0.0.0:%d/health", self.config.daemon.health_port)

    async def _health_handler(self, request: Any) -> Any:
        """Return 200 OK if the daemon is running."""
        from aiohttp import web

        return web.json_response({"status": "ok"})

    async def _status_handler(self, request: Any) -> Any:
        """Return detailed daemon status."""
        from aiohttp import web

        uptime = time.monotonic() - self._start_time
        status = {
            "status": "running",
            "uptime_seconds": round(uptime, 1),
            "provider": {
                "type": self.config.provider.type,
                "connected": self.provider.is_connected if self.provider else False,
            },
            "platforms": [
                {"name": p.name, "active": p.is_active}
                for p in self.platforms
            ],
            "active_routes": self.router.active_routes,
        }
        return web.json_response(status)

    def _setup_logging(self) -> None:
        """Configure logging based on daemon config."""
        level = getattr(logging, self.config.daemon.log_level.upper(), logging.INFO)

        handler = logging.StreamHandler()
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )

        root_logger = logging.getLogger("voicekit")
        root_logger.setLevel(level)

        # Avoid duplicate handlers on restart
        if not root_logger.handlers:
            root_logger.addHandler(handler)

        # Optionally add file handler
        if self.config.daemon.log_file:
            file_handler = logging.FileHandler(self.config.daemon.log_file)
            file_handler.setFormatter(handler.formatter)
            root_logger.addHandler(file_handler)

    def _setup_signals(self) -> None:
        """Register signal handlers for graceful shutdown."""
        loop = asyncio.get_running_loop()

        if sys.platform == "win32":
            # Windows ProactorEventLoop does not support add_signal_handler.
            # Fall back to signal.signal(), using call_soon_threadsafe to
            # safely schedule the handler on the event loop.
            for sig in (signal.SIGINT, signal.SIGTERM):
                signal.signal(sig, lambda s, _f, _loop=loop: _loop.call_soon_threadsafe(self._signal_handler, s))
        else:
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.add_signal_handler(sig, self._signal_handler, sig)

    def _signal_handler(self, sig: signal.Signals) -> None:
        """Handle shutdown signals."""
        logger.info("Received signal %s, initiating shutdown...", sig.name)
        self._shutdown_event.set()

    async def _log_event(self, event: Event) -> None:
        """Log events for debugging."""
        if event.type in (EventType.AUDIO_RECEIVED, EventType.AUDIO_SENT):
            # Don't log every audio chunk at info level
            logger.debug("Event: %s [%s] %s", event.type.value, event.platform, event.data)
        else:
            logger.info("Event: %s [%s] %s", event.type.value, event.platform, event.data)


async def run_daemon(config: VoiceKitConfig) -> None:
    """Run the VoiceKit daemon until shutdown."""
    daemon = VoiceKitDaemon(config)
    try:
        await daemon.start()
    finally:
        await daemon.stop()
