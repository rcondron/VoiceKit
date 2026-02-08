"""Abstract base class for AI voice providers."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import AsyncIterator


class VoiceProvider(ABC):
    """Base class for AI voice providers.

    A voice provider connects to an AI model that can process and generate
    speech in real time. Audio flows bidirectionally: the platform sends
    user speech to the provider, and the provider streams AI speech back.

    All audio exchanged with the router is in the internal format:
    PCM 16-bit signed, 24 kHz, mono.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Human-readable name for this provider."""
        ...

    @property
    def is_connected(self) -> bool:
        """Whether the provider is currently connected."""
        return False

    @abstractmethod
    async def connect(self) -> None:
        """Establish connection to the AI provider.

        Raises:
            ConnectionError: If the connection cannot be established.
        """
        ...

    @abstractmethod
    async def send_audio(self, audio: bytes) -> None:
        """Send an audio chunk to the AI provider.

        Args:
            audio: PCM 16-bit, 24 kHz, mono audio data.
        """
        ...

    @abstractmethod
    async def receive_audio(self) -> AsyncIterator[bytes]:
        """Yield audio chunks from the AI provider.

        Yields:
            PCM 16-bit, 24 kHz, mono audio data.
        """
        ...
        # Make this a valid async generator (yield is needed for type checking)
        if False:  # pragma: no cover
            yield b""

    @abstractmethod
    async def disconnect(self) -> None:
        """Close connection and release resources."""
        ...

    async def send_text(self, text: str) -> None:
        """Optionally send a text message to the AI (e.g., instructions).

        Not all providers support this. Default is a no-op.
        """

    async def interrupt(self) -> None:
        """Interrupt the AI's current response.

        Useful for barge-in scenarios where the user starts speaking while
        the AI is still outputting audio. Default is a no-op.
        """
