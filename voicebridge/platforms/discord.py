"""Discord voice channel platform adapter.

Connects VoiceBridge to Discord voice channels using discord.py.
The bot can join voice channels, capture user audio, and play AI
responses back.

Requires: pip install voicebridge[discord]
"""

from __future__ import annotations

import asyncio
import logging

from voicebridge.config import DiscordPlatformConfig
from voicebridge.platforms.base import AudioCallback, PlatformAdapter

logger = logging.getLogger(__name__)


class DiscordPlatform(PlatformAdapter):
    """Discord voice channel adapter.

    Uses discord.py to connect to voice channels and stream audio.
    """

    def __init__(self, config: DiscordPlatformConfig) -> None:
        self._config = config
        self._callback: AudioCallback | None = None
        self._active = False
        self._bot = None
        self._voice_client = None

    @property
    def name(self) -> str:
        return "discord"

    @property
    def is_active(self) -> bool:
        return self._active

    async def start(self) -> None:
        """Start the Discord bot and prepare for voice connections."""
        try:
            import discord  # noqa: F401
        except ImportError:
            raise RuntimeError(
                "discord.py[voice] is required for Discord support. "
                "Install with: pip install voicebridge[discord]"
            )

        logger.info("Discord platform starting...")
        logger.warning(
            "Discord platform is a stub implementation. "
            "Full discord.py voice integration is planned for Phase 2."
        )

        # TODO: Phase 2 implementation
        # 1. Create discord.Bot with voice intents
        # 2. Register commands (!vb join, !vb leave)
        # 3. Implement AudioSink for capturing user voice
        # 4. Implement AudioSource for playing AI responses
        # 5. Auto-join configured channels
        self._active = True

    async def stop(self) -> None:
        """Disconnect from voice and stop the bot."""
        self._active = False
        if self._voice_client:
            # await self._voice_client.disconnect()
            self._voice_client = None
        if self._bot:
            # await self._bot.close()
            self._bot = None
        logger.info("Discord platform stopped")

    def on_audio_received(self, callback: AudioCallback) -> None:
        self._callback = callback

    async def send_audio(self, audio: bytes) -> None:
        """Play audio in the Discord voice channel."""
        if not self._active:
            return
        # TODO: Feed audio to discord.AudioSource

    async def answer_call(self) -> None:
        """Join a voice channel (Discord equivalent of answering)."""
        pass

    async def hang_up(self) -> None:
        """Leave the voice channel."""
        if self._voice_client:
            pass
            # await self._voice_client.disconnect()
