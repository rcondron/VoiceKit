"""WhatsApp Desktop automation platform adapter.

Automates WhatsApp Desktop to answer voice calls and route audio.
Uses browser automation or accessibility APIs to interact with
the desktop application.

Planned for Phase 3 (Desktop App Automation).

Requires: pip install voicebridge[browser]
"""

from __future__ import annotations

import logging

from voicebridge.config import WhatsAppPlatformConfig
from voicebridge.platforms.base import AudioCallback, PlatformAdapter

logger = logging.getLogger(__name__)


class WhatsAppPlatform(PlatformAdapter):
    """WhatsApp Desktop adapter (stub).

    Phase 3 implementation will support:
    - Detecting incoming WhatsApp calls
    - Auto-answering calls
    - Routing audio via virtual audio device bridge
    - Contact allowlisting
    """

    def __init__(self, config: WhatsAppPlatformConfig) -> None:
        self._config = config
        self._callback: AudioCallback | None = None
        self._active = False

    @property
    def name(self) -> str:
        return "whatsapp"

    @property
    def is_active(self) -> bool:
        return self._active

    async def start(self) -> None:
        logger.warning(
            "WhatsApp platform is a stub. Full implementation planned for Phase 3."
        )
        self._active = True

    async def stop(self) -> None:
        self._active = False
        logger.info("WhatsApp platform stopped")

    def on_audio_received(self, callback: AudioCallback) -> None:
        self._callback = callback

    async def send_audio(self, audio: bytes) -> None:
        pass

    async def answer_call(self) -> None:
        logger.info("WhatsApp call auto-answer not yet implemented")

    async def reject_call(self) -> None:
        logger.info("WhatsApp call reject not yet implemented")
