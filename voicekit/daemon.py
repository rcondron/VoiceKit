"""VoiceKit daemon — main application loop.

Orchestrates platform adapters, AI providers, and the audio router.
Handles graceful startup, shutdown, signal handling, and health checks.
"""

from __future__ import annotations

import asyncio
import json
import logging
import signal
import time
from typing import Any

from voicekit.config import VoiceKitConfig
from voicekit.core.events import Event, EventBus, EventType
from voicekit.core.router import AudioRouter
from voicekit.platforms.base import PlatformAdapter
from voicekit.providers.base import VoiceProvider
from voicekit.providers.openai_realtime import OpenAIRealtimeProvider

logger = logging.getLogger(__name__)


def _create_provider(config: VoiceKitConfig) -> VoiceProvider:
    """Create an AI voice provider from configuration."""
    provider_type = config.provider.type

    if provider_type == "openai_realtime":
        return OpenAIRealtimeProvider(config.provider)
    elif provider_type == "google_gemini":
        from voicekit.providers.google_gemini import GeminiLiveProvider

        return GeminiLiveProvider(config.provider)
    elif provider_type == "elevenlabs":
        from voicekit.providers.elevenlabs import ElevenLabsProvider

        return ElevenLabsProvider(config.provider)
    elif provider_type == "deepgram":
        from voicekit.providers.deepgram import DeepgramVoiceAgentProvider

        return DeepgramVoiceAgentProvider(config.provider)
    else:
        raise ValueError(f"Unknown provider type: {provider_type}")


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

    return platforms


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
