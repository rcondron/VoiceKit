"""Abstract base class for voice/call platform adapters."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Awaitable, Callable


# Callback type: receives raw PCM audio bytes
AudioCallback = Callable[[bytes], Awaitable[None]]


class PlatformAdapter(ABC):
    """Base class for voice/call platform adapters.

    A platform adapter connects VoiceBridge to a communication platform
    (Telegram, Discord, virtual audio device, SIP phone, etc.). It handles
    platform-specific call management and audio I/O, exposing a uniform
    interface to the audio router.

    All audio exchanged with the router is in the internal format:
    PCM 16-bit signed, 24 kHz, mono (unless the adapter declares a
    different platform_format for the router to convert).
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Human-readable name for this platform."""
        ...

    @property
    def is_active(self) -> bool:
        """Whether the platform is currently active and processing audio."""
        return False

    @abstractmethod
    async def start(self) -> None:
        """Start the platform adapter.

        This should begin listening for incoming calls or audio activity.
        """
        ...

    @abstractmethod
    async def stop(self) -> None:
        """Stop the platform adapter and release resources."""
        ...

    @abstractmethod
    def on_audio_received(self, callback: AudioCallback) -> None:
        """Register a callback for incoming audio from the platform.

        The callback receives raw PCM audio bytes whenever audio is
        available from the platform (e.g., microphone input, call audio).

        Args:
            callback: Async function that receives audio bytes.
        """
        ...

    @abstractmethod
    async def send_audio(self, audio: bytes) -> None:
        """Send audio to the platform (e.g., play through speaker, send to call).

        Args:
            audio: Raw PCM audio bytes.
        """
        ...

    async def answer_call(self) -> None:
        """Answer an incoming call, if applicable.

        Not all platforms have a call model. Default is a no-op.
        """

    async def reject_call(self) -> None:
        """Reject an incoming call, if applicable.

        Default is a no-op.
        """

    async def hang_up(self) -> None:
        """End the current call, if applicable.

        Default is a no-op.
        """
