"""SIP/phone call platform adapter.

Connects VoiceKit to the telephone network via SIP (Session Initiation
Protocol). Supports receiving and making phone calls through a SIP provider.

Planned for Phase 4 (Professional).

Requires: A SIP library such as pjsua2 or aiosip.
"""

from __future__ import annotations

import logging

from voicekit.config import SipPlatformConfig
from voicekit.platforms.base import AudioCallback, PlatformAdapter

logger = logging.getLogger(__name__)


class SipPlatform(PlatformAdapter):
    """SIP phone call adapter (stub).

    Phase 4 implementation will support:
    - Registering with a SIP server
    - Receiving incoming phone calls
    - Auto-answering with AI voice
    - DTMF tone handling
    - Call transfer
    """

    def __init__(self, config: SipPlatformConfig) -> None:
        self._config = config
        self._callback: AudioCallback | None = None
        self._active = False

    @property
    def name(self) -> str:
        return "sip"

    @property
    def is_active(self) -> bool:
        return self._active

    async def start(self) -> None:
        logger.warning(
            "SIP platform is a stub. Full implementation planned for Phase 4."
        )
        self._active = True

    async def stop(self) -> None:
        self._active = False
        logger.info("SIP platform stopped")

    def on_audio_received(self, callback: AudioCallback) -> None:
        self._callback = callback

    async def send_audio(self, audio: bytes) -> None:
        pass

    async def answer_call(self) -> None:
        logger.info("SIP call auto-answer not yet implemented")

    async def reject_call(self) -> None:
        logger.info("SIP call reject not yet implemented")

    async def hang_up(self) -> None:
        logger.info("SIP call hang-up not yet implemented")
