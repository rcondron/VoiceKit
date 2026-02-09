"""Audio router — the heart of VoiceKit.

Routes audio bidirectionally between platform adapters and AI providers,
handling format conversion and buffering along the way.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from voicekit.core.audio import (
    INTERNAL_FORMAT,
    AudioBuffer,
    AudioFormat,
    convert_audio,
)
from voicekit.core.events import Event, EventBus, EventType

if TYPE_CHECKING:
    from voicekit.platforms.base import PlatformAdapter
    from voicekit.providers.base import VoiceProvider

logger = logging.getLogger(__name__)


class AudioRoute:
    """A single active route between a platform adapter and a provider."""

    def __init__(
        self,
        platform: PlatformAdapter,
        provider: VoiceProvider,
        event_bus: EventBus,
        platform_format: AudioFormat | None = None,
        middleware: object | None = None,
    ) -> None:
        self.platform = platform
        self.provider = provider
        self.event_bus = event_bus
        self.platform_format = platform_format or INTERNAL_FORMAT
        self._middleware = middleware  # AudioPipeline instance (optional)
        self._inbound_buffer = AudioBuffer()
        self._active = False
        self._tasks: list[asyncio.Task[None]] = []

    async def start(self) -> None:
        """Start routing audio between platform and provider."""
        if self._active:
            return

        self._active = True
        logger.info(
            "Starting audio route: %s <-> %s",
            self.platform.name,
            self.provider.name,
        )

        # Register platform audio callback
        self.platform.on_audio_received(self._handle_platform_audio)

        # Start receiving audio from provider
        task = asyncio.create_task(self._provider_receive_loop())
        self._tasks.append(task)

    async def stop(self) -> None:
        """Stop routing and clean up."""
        if not self._active:
            return

        self._active = False
        logger.info(
            "Stopping audio route: %s <-> %s",
            self.platform.name,
            self.provider.name,
        )

        for task in self._tasks:
            task.cancel()

        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        self._inbound_buffer.clear()

    async def _handle_platform_audio(self, audio: bytes) -> None:
        """Process audio received from the platform and forward to AI provider.

        Converts from platform format to internal format, buffers, then sends.
        """
        if not self._active:
            return

        # Convert to internal format if needed
        if self.platform_format != INTERNAL_FORMAT:
            audio = convert_audio(audio, self.platform_format, INTERNAL_FORMAT)

        # Run through middleware pipeline (inbound = user → provider)
        if self._middleware is not None:
            audio = await self._middleware.process_inbound(audio)

        # Buffer and send complete chunks
        chunks = self._inbound_buffer.write(audio)
        for chunk in chunks:
            try:
                await self.provider.send_audio(chunk)
                await self.event_bus.emit(
                    Event(
                        type=EventType.AUDIO_SENT,
                        platform=self.platform.name,
                        data={"direction": "to_provider", "bytes": len(chunk)},
                    )
                )
            except Exception:
                logger.exception("Failed to send audio to provider")

    async def _provider_receive_loop(self) -> None:
        """Continuously receive audio from the AI provider and send to platform."""
        try:
            async for audio_chunk in self.provider.receive_audio():
                if not self._active:
                    break

                # Run through middleware pipeline (outbound = provider → user)
                outbound = audio_chunk
                if self._middleware is not None:
                    outbound = await self._middleware.process_outbound(outbound)

                # Convert from internal format to platform format if needed
                if self.platform_format != INTERNAL_FORMAT:
                    outbound = convert_audio(audio_chunk, INTERNAL_FORMAT, self.platform_format)

                try:
                    await self.platform.send_audio(outbound)
                    await self.event_bus.emit(
                        Event(
                            type=EventType.AUDIO_RECEIVED,
                            platform=self.platform.name,
                            data={"direction": "from_provider", "bytes": len(outbound)},
                        )
                    )
                except Exception:
                    logger.exception("Failed to send audio to platform %s", self.platform.name)

        except asyncio.CancelledError:
            logger.debug("Provider receive loop cancelled for %s", self.platform.name)
        except Exception:
            logger.exception("Provider receive loop error for %s", self.platform.name)
            await self.event_bus.emit(
                Event(
                    type=EventType.PROVIDER_ERROR,
                    platform=self.platform.name,
                    data={"error": "receive_loop_failed"},
                )
            )


class AudioRouter:
    """Manages multiple audio routes between platforms and providers."""

    def __init__(self, event_bus: EventBus) -> None:
        self.event_bus = event_bus
        self._routes: dict[str, AudioRoute] = {}
        self._middleware: object | None = None

    def set_middleware(self, middleware: object) -> None:
        """Set the audio middleware pipeline for all routes."""
        self._middleware = middleware

    async def add_route(
        self,
        platform: PlatformAdapter,
        provider: VoiceProvider,
        platform_format: AudioFormat | None = None,
    ) -> AudioRoute:
        """Create and start a new audio route.

        Args:
            platform: The platform adapter.
            provider: The AI voice provider.
            platform_format: Audio format used by the platform.

        Returns:
            The created AudioRoute.
        """
        route_key = f"{platform.name}:{id(platform)}"

        if route_key in self._routes:
            logger.warning("Route already exists for %s, stopping old route", route_key)
            await self._routes[route_key].stop()

        route = AudioRoute(platform, provider, self.event_bus, platform_format, self._middleware)
        self._routes[route_key] = route
        await route.start()

        logger.info("Audio route added: %s", route_key)
        return route

    async def remove_route(self, platform: PlatformAdapter) -> None:
        """Stop and remove a route for a platform."""
        route_key = f"{platform.name}:{id(platform)}"
        route = self._routes.pop(route_key, None)
        if route:
            await route.stop()
            logger.info("Audio route removed: %s", route_key)

    async def stop_all(self) -> None:
        """Stop all active routes."""
        for route in self._routes.values():
            await route.stop()
        self._routes.clear()
        logger.info("All audio routes stopped")

    @property
    def active_routes(self) -> int:
        return len(self._routes)
