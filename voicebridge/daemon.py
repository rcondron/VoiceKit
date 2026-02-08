"""VoiceBridge daemon — main application loop.

Orchestrates platform adapters, AI providers, and the audio router.
Handles graceful startup, shutdown, and signal handling.
"""

from __future__ import annotations

import asyncio
import logging
import signal
from typing import Any

from voicebridge.config import VoiceBridgeConfig
from voicebridge.core.events import Event, EventBus, EventType
from voicebridge.core.router import AudioRouter
from voicebridge.platforms.base import PlatformAdapter
from voicebridge.providers.base import VoiceProvider
from voicebridge.providers.openai_realtime import OpenAIRealtimeProvider

logger = logging.getLogger(__name__)


def _create_provider(config: VoiceBridgeConfig) -> VoiceProvider:
    """Create an AI voice provider from configuration."""
    provider_type = config.provider.type

    if provider_type == "openai_realtime":
        return OpenAIRealtimeProvider(config.provider)
    else:
        raise ValueError(f"Unknown provider type: {provider_type}")


def _create_platforms(config: VoiceBridgeConfig) -> list[PlatformAdapter]:
    """Create enabled platform adapters from configuration."""
    platforms: list[PlatformAdapter] = []

    if config.platforms.virtual_audio.enabled:
        from voicebridge.platforms.virtual_audio import VirtualAudioPlatform

        platforms.append(VirtualAudioPlatform(config.platforms.virtual_audio))

    if config.platforms.telegram.enabled:
        from voicebridge.platforms.telegram import TelegramPlatform

        platforms.append(TelegramPlatform(config.platforms.telegram))

    if config.platforms.discord.enabled:
        from voicebridge.platforms.discord import DiscordPlatform

        platforms.append(DiscordPlatform(config.platforms.discord))

    if config.platforms.zoom.enabled:
        from voicebridge.platforms.zoom import ZoomPlatform

        platforms.append(ZoomPlatform(config.platforms.zoom))

    if config.platforms.whatsapp.enabled:
        from voicebridge.platforms.whatsapp import WhatsAppPlatform

        platforms.append(WhatsAppPlatform(config.platforms.whatsapp))

    if config.platforms.signal.enabled:
        from voicebridge.platforms.signal import SignalPlatform

        platforms.append(SignalPlatform(config.platforms.signal))

    if config.platforms.slack.enabled:
        from voicebridge.platforms.slack import SlackPlatform

        platforms.append(SlackPlatform(config.platforms.slack))

    if config.platforms.sip.enabled:
        from voicebridge.platforms.sip import SipPlatform

        platforms.append(SipPlatform(config.platforms.sip))

    return platforms


class VoiceBridgeDaemon:
    """Main VoiceBridge daemon.

    Manages the lifecycle of all components: provider, platforms,
    event bus, and audio router.
    """

    def __init__(self, config: VoiceBridgeConfig) -> None:
        self.config = config
        self.event_bus = EventBus()
        self.router = AudioRouter(self.event_bus)
        self.provider: VoiceProvider | None = None
        self.platforms: list[PlatformAdapter] = []
        self._shutdown_event = asyncio.Event()

    async def start(self) -> None:
        """Start the daemon: connect provider, start platforms, begin routing."""
        self._setup_logging()
        self._setup_signals()

        logger.info("VoiceBridge daemon starting...")

        # Register event logging
        self.event_bus.on_all(self._log_event)

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
            "VoiceBridge daemon running with %d active platform(s): %s",
            len(active_platforms),
            ", ".join(active_platforms) if active_platforms else "none",
        )

        # Wait for shutdown signal
        await self._shutdown_event.wait()

    async def stop(self) -> None:
        """Gracefully stop the daemon."""
        logger.info("VoiceBridge daemon stopping...")
        await self.event_bus.emit(Event(type=EventType.DAEMON_STOPPING))

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

        logger.info("VoiceBridge daemon stopped")

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

        root_logger = logging.getLogger("voicebridge")
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


async def run_daemon(config: VoiceBridgeConfig) -> None:
    """Run the VoiceBridge daemon until shutdown."""
    daemon = VoiceBridgeDaemon(config)
    try:
        await daemon.start()
    finally:
        await daemon.stop()
