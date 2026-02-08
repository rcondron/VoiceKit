"""Signal Desktop automation platform adapter.

Automates Signal Desktop to answer voice calls and route audio.
Planned for Phase 3 (Desktop App Automation).
"""

from __future__ import annotations

import logging

from voicekit.config import SignalPlatformConfig
from voicekit.platforms.base import AudioCallback, PlatformAdapter

logger = logging.getLogger(__name__)


class SignalPlatform(PlatformAdapter):
    """Signal Desktop adapter (stub).

    Phase 3 implementation will support:
    - Detecting incoming Signal calls
    - Auto-answering calls
    - Routing audio via virtual audio device bridge
    - Contact allowlisting
    """

    def __init__(self, config: SignalPlatformConfig) -> None:
        self._config = config
        self._callback: AudioCallback | None = None
        self._active = False

    @property
    def name(self) -> str:
        return "signal"

    @property
    def is_active(self) -> bool:
        return self._active

    async def start(self) -> None:
        logger.warning(
            "Signal platform is a stub. Full implementation planned for Phase 3."
        )
        self._active = True

    async def stop(self) -> None:
        self._active = False
        logger.info("Signal platform stopped")

    def on_audio_received(self, callback: AudioCallback) -> None:
        self._callback = callback

    async def send_audio(self, audio: bytes) -> None:
        pass

    async def answer_call(self) -> None:
        logger.info("Signal call auto-answer not yet implemented")
