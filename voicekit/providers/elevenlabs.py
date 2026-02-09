"""ElevenLabs Conversational AI voice provider.

Connects to ElevenLabs' Conversational AI WebSocket API for real-time
voice conversations with low-latency TTS and STT.

Reference: https://elevenlabs.io/docs/conversational-ai

Requires: pip install elevenlabs websockets
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from typing import AsyncIterator

from voicekit.config import ProviderConfig
from voicekit.providers.base import VoiceProvider

logger = logging.getLogger(__name__)

ELEVENLABS_CONV_URL = "wss://api.elevenlabs.io/v1/convai/conversation"


class ElevenLabsProvider(VoiceProvider):
    """ElevenLabs Conversational AI voice provider.

    Streams PCM audio over a WebSocket connection using the ElevenLabs
    Conversational AI protocol. Handles agent configuration, audio input,
    and response audio streaming.
    """

    def __init__(self, config: ProviderConfig) -> None:
        self._config = config
        self._ws: object | None = None
        self._audio_queue: asyncio.Queue[bytes] = asyncio.Queue()
        self._connected = False
        self._receive_task: asyncio.Task[None] | None = None
        # ElevenLabs agent_id from model field or config
        self._agent_id = config.model or ""

    @property
    def name(self) -> str:
        return "elevenlabs"

    @property
    def is_connected(self) -> bool:
        return self._connected

    async def connect(self) -> None:
        """Connect to the ElevenLabs Conversational AI WebSocket."""
        if self._connected:
            return

        try:
            import websockets
        except ImportError:
            raise RuntimeError(
                "websockets is required for ElevenLabs support."
            )

        if not self._agent_id:
            raise ValueError(
                "ElevenLabs requires an agent_id. Set provider.model to your agent ID."
            )

        url = f"{ELEVENLABS_CONV_URL}?agent_id={self._agent_id}"
        headers = {}
        if self._config.api_key:
            headers["xi-api-key"] = self._config.api_key

        logger.info(
            "Connecting to ElevenLabs Conversational AI (agent=%s)...",
            self._agent_id,
        )

        try:
            self._ws = await websockets.connect(
                url,
                additional_headers=headers,
                max_size=None,
            )
            self._connected = True
            logger.info("Connected to ElevenLabs Conversational AI")

            self._receive_task = asyncio.create_task(self._receive_loop())

        except Exception as exc:
            self._connected = False
            raise ConnectionError(
                f"Failed to connect to ElevenLabs: {exc}"
            ) from exc

    async def send_audio(self, audio: bytes) -> None:
        """Send audio to ElevenLabs.

        Audio is base64-encoded and sent as a user_audio_chunk message.

        Args:
            audio: PCM 16-bit, 24 kHz, mono audio data.
        """
        if not self._ws or not self._connected:
            return

        encoded = base64.b64encode(audio).decode("ascii")
        msg = {
            "user_audio_chunk": encoded,
        }

        try:
            await self._ws.send(json.dumps(msg))  # type: ignore[union-attr]
        except Exception:
            logger.warning("WebSocket closed while sending audio")
            self._connected = False

    async def receive_audio(self) -> AsyncIterator[bytes]:
        """Yield audio chunks received from ElevenLabs."""
        while self._connected:
            try:
                chunk = await asyncio.wait_for(self._audio_queue.get(), timeout=0.1)
                yield chunk
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break

    async def _receive_loop(self) -> None:
        """Background task that processes incoming WebSocket messages."""
        if not self._ws:
            return

        try:
            async for raw_message in self._ws:  # type: ignore[union-attr]
                if not self._connected:
                    break

                try:
                    message = json.loads(raw_message)
                except json.JSONDecodeError:
                    continue

                await self._handle_message(message)

        except asyncio.CancelledError:
            logger.debug("ElevenLabs receive loop cancelled")
        except Exception:
            logger.exception("Unexpected error in ElevenLabs receive loop")
        finally:
            self._connected = False

    async def _handle_message(self, message: dict) -> None:
        """Process a single ElevenLabs message."""
        msg_type = message.get("type", "")

        if msg_type == "conversation_initiation_metadata":
            conv_id = message.get("conversation_initiation_metadata_event", {}).get(
                "conversation_id", ""
            )
            logger.info("ElevenLabs conversation started: %s", conv_id)

        elif msg_type == "audio":
            audio_event = message.get("audio_event", {})
            audio_b64 = audio_event.get("audio_base_64", "")
            if audio_b64:
                audio_bytes = base64.b64decode(audio_b64)
                await self._audio_queue.put(audio_bytes)

        elif msg_type == "agent_response":
            text = message.get("agent_response_event", {}).get("agent_response", "")
            if text:
                logger.debug("ElevenLabs agent: %s", text[:100])

        elif msg_type == "user_transcript":
            text = message.get("user_transcription_event", {}).get("user_transcript", "")
            if text:
                logger.debug("ElevenLabs user: %s", text[:100])

        elif msg_type == "interruption":
            logger.debug("ElevenLabs: user interrupted agent")
            # Drain queued audio
            while not self._audio_queue.empty():
                try:
                    self._audio_queue.get_nowait()
                except asyncio.QueueEmpty:
                    break

        elif msg_type == "ping":
            # Respond to keepalive pings
            event_id = message.get("ping_event", {}).get("event_id")
            if event_id and self._ws:
                pong = {"type": "pong", "event_id": event_id}
                try:
                    await self._ws.send(json.dumps(pong))  # type: ignore[union-attr]
                except Exception:
                    pass

        elif msg_type == "error":
            logger.error("ElevenLabs error: %s", message)

    async def disconnect(self) -> None:
        """Close the WebSocket connection."""
        self._connected = False

        if self._receive_task and not self._receive_task.done():
            self._receive_task.cancel()
            try:
                await self._receive_task
            except asyncio.CancelledError:
                pass
            self._receive_task = None

        if self._ws:
            try:
                await self._ws.close()  # type: ignore[union-attr]
            except Exception:
                logger.debug("Error closing ElevenLabs WebSocket", exc_info=True)
            self._ws = None

        while not self._audio_queue.empty():
            try:
                self._audio_queue.get_nowait()
            except asyncio.QueueEmpty:
                break

        logger.info("Disconnected from ElevenLabs Conversational AI")

    async def send_text(self, text: str) -> None:
        """Not directly supported by ElevenLabs Conversational AI."""
        logger.debug("send_text not supported by ElevenLabs provider")

    async def interrupt(self) -> None:
        """Signal an interruption to ElevenLabs."""
        while not self._audio_queue.empty():
            try:
                self._audio_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
