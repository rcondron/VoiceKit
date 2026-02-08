"""Zoom meeting platform adapter.

Connects VoiceKit to Zoom meetings via the Zoom Bot SDK or
browser automation. Planned for Phase 4 (Professional).

Requires: Zoom Bot SDK credentials or browser automation setup.
"""

from __future__ import annotations

import logging

from voicekit.config import ZoomPlatformConfig
from voicekit.platforms.base import AudioCallback, PlatformAdapter

logger = logging.getLogger(__name__)


class ZoomPlatform(PlatformAdapter):
    """Zoom meeting adapter (stub).

    Phase 4 implementation will support:
    - Joining meetings via Zoom Bot SDK
    - Capturing meeting audio
    - Playing AI responses into the meeting
    - Meeting lifecycle management
    """

    def __init__(self, config: ZoomPlatformConfig) -> None:
        self._config = config
        self._callback: AudioCallback | None = None
        self._active = False

    @property
    def name(self) -> str:
        return "zoom"

    @property
    def is_active(self) -> bool:
        return self._active

    async def start(self) -> None:
        logger.warning(
            "Zoom platform is a stub. Full implementation planned for Phase 4."
        )
        self._active = True

    async def stop(self) -> None:
        self._active = False
        logger.info("Zoom platform stopped")

    def on_audio_received(self, callback: AudioCallback) -> None:
        self._callback = callback

    async def send_audio(self, audio: bytes) -> None:
        pass
