"""Slack Huddles platform adapter.

Connects VoiceKit to Slack Huddles for real-time voice interaction.
Planned for Phase 3 (Desktop App Automation).
"""

from __future__ import annotations

import logging

from voicekit.config import SlackPlatformConfig
from voicekit.platforms.base import AudioCallback, PlatformAdapter

logger = logging.getLogger(__name__)


class SlackPlatform(PlatformAdapter):
    """Slack Huddles adapter (stub).

    Phase 3 implementation will support:
    - Joining Slack Huddles via bot
    - Capturing huddle audio
    - Playing AI responses into huddles
    """

    def __init__(self, config: SlackPlatformConfig) -> None:
        self._config = config
        self._callback: AudioCallback | None = None
        self._active = False

    @property
    def name(self) -> str:
        return "slack"

    @property
    def is_active(self) -> bool:
        return self._active

    async def start(self) -> None:
        logger.warning(
            "Slack platform is a stub. Full implementation planned for Phase 3."
        )
        self._active = True

    async def stop(self) -> None:
        self._active = False
        logger.info("Slack platform stopped")

    def on_audio_received(self, callback: AudioCallback) -> None:
        self._callback = callback

    async def send_audio(self, audio: bytes) -> None:
        pass
