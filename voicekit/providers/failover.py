"""Multi-provider failover wrapper.

Wraps multiple VoiceProvider instances and automatically fails over
to the next provider when the current one disconnects or errors out.

Usage in config:
  provider:
    type: failover
    failover_providers:
      - type: openai_realtime
        api_key: ${OPENAI_API_KEY}
        model: gpt-4o-realtime-preview
      - type: google_gemini
        api_key: ${GEMINI_API_KEY}
        model: gemini-2.0-flash-exp
      - type: deepgram
        api_key: ${DEEPGRAM_API_KEY}
"""

from __future__ import annotations

import asyncio
import logging
from typing import AsyncIterator

from voicekit.config import ProviderConfig
from voicekit.providers.base import VoiceProvider

logger = logging.getLogger(__name__)


class FailoverProvider(VoiceProvider):
    """Wraps multiple providers with automatic failover.

    Connects to the first (primary) provider. If it disconnects or
    errors, automatically tries the next provider in the list. Cycles
    through all providers before giving up.
    """

    def __init__(self, providers: list[VoiceProvider]) -> None:
        if not providers:
            raise ValueError("FailoverProvider requires at least one provider")
        self._providers = providers
        self._current_index = 0
        self._current: VoiceProvider = providers[0]
        self._connected = False
        self._monitor_task: asyncio.Task[None] | None = None

    @property
    def name(self) -> str:
        return f"failover({self._current.name})"

    @property
    def is_connected(self) -> bool:
        return self._connected and self._current.is_connected

    async def connect(self) -> None:
        """Connect to the primary provider, falling back to others on failure."""
        for i, provider in enumerate(self._providers):
            try:
                logger.info(
                    "Failover: connecting to %s (%d/%d)...",
                    provider.name,
                    i + 1,
                    len(self._providers),
                )
                await provider.connect()
                self._current = provider
                self._current_index = i
                self._connected = True
                logger.info("Failover: connected to %s", provider.name)

                # Start monitoring for disconnections
                self._monitor_task = asyncio.create_task(self._monitor_loop())
                return

            except Exception:
                logger.warning(
                    "Failover: %s failed to connect, trying next...",
                    provider.name,
                    exc_info=True,
                )
                continue

        raise ConnectionError(
            "All providers failed to connect: "
            + ", ".join(p.name for p in self._providers)
        )

    async def _monitor_loop(self) -> None:
        """Background task that monitors the current provider's health."""
        while self._connected:
            await asyncio.sleep(5)

            if not self._current.is_connected and self._connected:
                logger.warning(
                    "Failover: %s disconnected, switching to next provider...",
                    self._current.name,
                )
                await self._failover()

    async def _failover(self) -> None:
        """Switch to the next available provider."""
        original_index = self._current_index

        for offset in range(1, len(self._providers) + 1):
            next_index = (self._current_index + offset) % len(self._providers)
            next_provider = self._providers[next_index]

            try:
                logger.info("Failover: trying %s...", next_provider.name)
                await next_provider.connect()
                self._current = next_provider
                self._current_index = next_index
                logger.info("Failover: switched to %s", next_provider.name)
                return
            except Exception:
                logger.warning(
                    "Failover: %s also failed",
                    next_provider.name,
                    exc_info=True,
                )

        logger.error("Failover: all providers failed — no fallback available")
        self._connected = False

    async def send_audio(self, audio: bytes) -> None:
        if not self._connected:
            return
        try:
            await self._current.send_audio(audio)
        except Exception:
            logger.warning("Failover: send_audio failed on %s", self._current.name)
            await self._failover()

    async def receive_audio(self) -> AsyncIterator[bytes]:
        while self._connected:
            try:
                async for chunk in self._current.receive_audio():
                    yield chunk
                    if not self._connected:
                        return
            except Exception:
                if self._connected:
                    logger.warning(
                        "Failover: receive_audio failed on %s, switching...",
                        self._current.name,
                    )
                    await self._failover()
                    continue
                return

    async def disconnect(self) -> None:
        self._connected = False

        if self._monitor_task and not self._monitor_task.done():
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass

        for provider in self._providers:
            try:
                await provider.disconnect()
            except Exception:
                pass

        logger.info("Failover provider disconnected (all backends)")

    async def send_text(self, text: str) -> None:
        if self._connected:
            await self._current.send_text(text)

    async def interrupt(self) -> None:
        if self._connected:
            await self._current.interrupt()
