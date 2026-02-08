"""Telegram voice call platform adapter.

Connects VoiceBridge to Telegram voice calls using pyrogram and tgcalls.
Supports auto-answering incoming calls and routing audio to the AI provider.

Requires: pip install voicebridge[telegram]
"""

from __future__ import annotations

import asyncio
import logging

from voicebridge.config import TelegramPlatformConfig
from voicebridge.platforms.base import AudioCallback, PlatformAdapter

logger = logging.getLogger(__name__)


class TelegramPlatform(PlatformAdapter):
    """Telegram voice call adapter.

    Uses pyrogram for the Telegram client and tgcalls for voice call
    audio streaming.
    """

    def __init__(self, config: TelegramPlatformConfig) -> None:
        self._config = config
        self._callback: AudioCallback | None = None
        self._active = False
        self._client = None
        self._call = None

    @property
    def name(self) -> str:
        return "telegram"

    @property
    def is_active(self) -> bool:
        return self._active

    async def start(self) -> None:
        """Start the Telegram client and listen for incoming calls."""
        try:
            from pyrogram import Client  # noqa: F401
        except ImportError:
            raise RuntimeError(
                "pyrogram and tgcalls are required for Telegram support. "
                "Install with: pip install voicebridge[telegram]"
            )

        logger.info("Telegram platform starting...")
        logger.warning(
            "Telegram platform is a stub implementation. "
            "Full tgcalls integration is planned for Phase 2."
        )

        # TODO: Phase 2 implementation
        # 1. Initialize pyrogram Client with api_id, api_hash
        # 2. Set up tgcalls GroupCallFactory
        # 3. Register incoming call handler
        # 4. Start the client
        self._active = True

    async def stop(self) -> None:
        """Stop the Telegram client."""
        self._active = False
        if self._client:
            # await self._client.stop()
            self._client = None
        logger.info("Telegram platform stopped")

    def on_audio_received(self, callback: AudioCallback) -> None:
        self._callback = callback

    async def send_audio(self, audio: bytes) -> None:
        """Send audio to the active Telegram call."""
        if not self._active:
            return
        # TODO: Write audio to tgcalls raw audio pipe

    async def answer_call(self) -> None:
        """Answer an incoming Telegram call."""
        logger.info("Answering Telegram call...")
        # TODO: Accept call via tgcalls

    async def reject_call(self) -> None:
        """Reject an incoming Telegram call."""
        logger.info("Rejecting Telegram call...")
        # TODO: Reject call via tgcalls

    async def hang_up(self) -> None:
        """End the current Telegram call."""
        logger.info("Hanging up Telegram call...")
        # TODO: End call via tgcalls
