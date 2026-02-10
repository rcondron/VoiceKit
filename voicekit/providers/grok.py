"""xAI Grok Voice Agent API provider.

Connects to xAI's Grok Voice Agent API via WebSocket for real-time
bidirectional voice conversations. The API is compatible with the
OpenAI Realtime API specification, streaming base64-encoded PCM audio
over WebSocket.

The Grok Voice Agent API combines STT, LLM (Grok), and TTS in a single
low-latency pipeline with sub-second time-to-first-audio.

Reference: https://docs.x.ai/docs/guides/voice/agent

Requires: pip install websockets
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from typing import AsyncIterator

import websockets
from websockets.asyncio.client import ClientConnection

from voicekit.config import ProviderConfig
from voicekit.providers.base import VoiceProvider

logger = logging.getLogger(__name__)

GROK_REALTIME_URL = "wss://api.x.ai/v1/realtime"


class GrokVoiceProvider(VoiceProvider):
    """xAI Grok Voice Agent API provider.

    Streams PCM audio over a WebSocket connection using the xAI Realtime
    protocol (OpenAI Realtime API compatible). Handles session
    configuration, audio input buffering, and response audio streaming.

    Available voices: Ara, Rex, Sal, Eve, Leo

    Configuration:
      provider:
        type: grok
        api_key: ${XAI_API_KEY}
        model: grok-3
        voice: Ara
        instructions: "You are a helpful voice assistant."
        turn_detection:
          threshold: 0.5
          silence_duration_ms: 500
          prefix_padding_ms: 300
    """

    MAX_RECONNECT_ATTEMPTS = 5
    RECONNECT_BASE_DELAY = 1.0  # seconds, doubles each attempt

    def __init__(self, config: ProviderConfig) -> None:
        self._config = config
        self._ws: ClientConnection | None = None
        self._audio_queue: asyncio.Queue[bytes] = asyncio.Queue()
        self._connected = False
        self._receive_task: asyncio.Task[None] | None = None
        self._should_reconnect = True
        self._reconnect_attempts = 0

    @property
    def name(self) -> str:
        return "grok"

    @property
    def is_connected(self) -> bool:
        return self._connected

    async def connect(self) -> None:
        """Connect to the Grok Voice Agent API WebSocket."""
        if self._connected:
            return

        model = self._config.model or "grok-3"
        url = f"{GROK_REALTIME_URL}?model={model}"
        headers = {
            "Authorization": f"Bearer {self._config.api_key}",
        }

        logger.info("Connecting to Grok Voice Agent API (model=%s)...", model)

        try:
            self._ws = await websockets.connect(
                url,
                additional_headers=headers,
                max_size=None,
            )
            self._connected = True
            logger.info("Connected to Grok Voice Agent API")

            # Start background message handler
            self._receive_task = asyncio.create_task(self._receive_loop())

            # Configure the session
            await self._configure_session()

        except Exception as exc:
            self._connected = False
            raise ConnectionError(
                f"Failed to connect to Grok Voice Agent API: {exc}"
            ) from exc

    async def _configure_session(self) -> None:
        """Send session configuration to the API."""
        if not self._ws:
            return

        session_config: dict = {
            "type": "session.update",
            "session": {
                "modalities": ["text", "audio"],
                "instructions": self._config.instructions,
                "voice": self._config.voice or "Ara",
                "input_audio_format": "pcm16",
                "output_audio_format": "pcm16",
                "turn_detection": {
                    "type": "server_vad",
                    "silence_duration_ms": self._config.turn_detection.silence_duration_ms,
                    "threshold": self._config.turn_detection.threshold,
                    "prefix_padding_ms": self._config.turn_detection.prefix_padding_ms,
                },
            },
        }

        await self._ws.send(json.dumps(session_config))
        logger.debug("Grok session configured: voice=%s", self._config.voice or "Ara")

    async def send_audio(self, audio: bytes) -> None:
        """Send audio to the Grok Voice Agent API.

        Audio is base64-encoded and sent as an input_audio_buffer.append event.

        Args:
            audio: PCM 16-bit, 24 kHz, mono audio data.
        """
        if not self._ws or not self._connected:
            return

        encoded = base64.b64encode(audio).decode("ascii")
        event = {
            "type": "input_audio_buffer.append",
            "audio": encoded,
        }

        try:
            await self._ws.send(json.dumps(event))
        except websockets.ConnectionClosed:
            logger.warning("Grok WebSocket closed while sending audio")
            self._connected = False

    async def receive_audio(self) -> AsyncIterator[bytes]:
        """Yield audio chunks received from the Grok Voice Agent API.

        Audio is decoded from base64 and yielded as raw PCM bytes.
        """
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
            async for raw_message in self._ws:
                if not self._connected:
                    break

                try:
                    message = json.loads(raw_message)
                except json.JSONDecodeError:
                    logger.warning("Received non-JSON message from Grok API")
                    continue

                await self._handle_message(message)

        except websockets.ConnectionClosed as exc:
            logger.info("Grok WebSocket connection closed: %s", exc)
            self._connected = False
            if self._should_reconnect:
                await self._attempt_reconnect()
                return
        except asyncio.CancelledError:
            logger.debug("Grok receive loop cancelled")
        except Exception:
            logger.exception("Unexpected error in Grok receive loop")
            self._connected = False
            if self._should_reconnect:
                await self._attempt_reconnect()
                return
        finally:
            self._connected = False

    async def _handle_message(self, message: dict) -> None:
        """Process a single API message."""
        msg_type = message.get("type", "")

        if msg_type == "response.audio.delta":
            audio_b64 = message.get("delta", "")
            if audio_b64:
                audio_bytes = base64.b64decode(audio_b64)
                await self._audio_queue.put(audio_bytes)

        elif msg_type == "session.created":
            logger.info(
                "Grok session created: %s",
                message.get("session", {}).get("id", ""),
            )

        elif msg_type == "session.updated":
            logger.debug("Grok session updated")

        elif msg_type == "response.audio.done":
            logger.debug("Grok audio response complete")

        elif msg_type == "response.audio_transcript.delta":
            transcript = message.get("delta", "")
            if transcript:
                logger.debug("Grok AI transcript: %s", transcript)

        elif msg_type == "input_audio_buffer.speech_started":
            logger.debug("Grok: user speech detected")

        elif msg_type == "input_audio_buffer.speech_stopped":
            logger.debug("Grok: user speech ended")

        elif msg_type == "input_audio_buffer.committed":
            logger.debug("Grok: input audio committed")

        elif msg_type == "response.created":
            logger.debug("Grok: AI response started")

        elif msg_type == "response.done":
            logger.debug("Grok: AI response completed")

        elif msg_type == "response.function_call_arguments.delta":
            logger.debug("Grok: function call arguments streaming")

        elif msg_type == "response.function_call_arguments.done":
            logger.debug("Grok: function call arguments complete")

        elif msg_type == "error":
            error = message.get("error", {})
            logger.error(
                "Grok API error: %s — %s",
                error.get("type", "unknown"),
                error.get("message", ""),
            )

        elif msg_type.startswith("rate_limits"):
            pass  # Silently ignore rate limit info

        else:
            logger.debug("Grok unhandled message type: %s", msg_type)

    async def _attempt_reconnect(self) -> None:
        """Try to reconnect with exponential backoff."""
        while (
            self._should_reconnect
            and self._reconnect_attempts < self.MAX_RECONNECT_ATTEMPTS
        ):
            self._reconnect_attempts += 1
            delay = self.RECONNECT_BASE_DELAY * (2 ** (self._reconnect_attempts - 1))
            logger.info(
                "Reconnecting to Grok Voice Agent API (attempt %d/%d, delay %.1fs)...",
                self._reconnect_attempts,
                self.MAX_RECONNECT_ATTEMPTS,
                delay,
            )
            await asyncio.sleep(delay)

            try:
                if self._ws:
                    try:
                        await self._ws.close()
                    except Exception:
                        pass
                    self._ws = None

                await self.connect()
                self._reconnect_attempts = 0
                logger.info("Reconnected to Grok Voice Agent API")
                return
            except Exception:
                logger.warning(
                    "Grok reconnection attempt %d failed",
                    self._reconnect_attempts,
                    exc_info=True,
                )

        if self._reconnect_attempts >= self.MAX_RECONNECT_ATTEMPTS:
            logger.error(
                "Failed to reconnect to Grok after %d attempts — giving up",
                self.MAX_RECONNECT_ATTEMPTS,
            )

    async def disconnect(self) -> None:
        """Close the WebSocket connection."""
        self._should_reconnect = False
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
                await self._ws.close()
            except Exception:
                logger.debug("Error closing Grok WebSocket", exc_info=True)
            self._ws = None

        # Drain the queue
        while not self._audio_queue.empty():
            try:
                self._audio_queue.get_nowait()
            except asyncio.QueueEmpty:
                break

        logger.info("Disconnected from Grok Voice Agent API")

    async def send_text(self, text: str) -> None:
        """Send a text message that the AI should respond to."""
        if not self._ws or not self._connected:
            return

        event = {
            "type": "conversation.item.create",
            "item": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": text}],
            },
        }
        await self._ws.send(json.dumps(event))

        # Trigger a response
        await self._ws.send(json.dumps({"type": "response.create"}))

    async def interrupt(self) -> None:
        """Cancel the current AI response."""
        if not self._ws or not self._connected:
            return

        await self._ws.send(json.dumps({"type": "response.cancel"}))

        # Drain queued audio from the cancelled response
        while not self._audio_queue.empty():
            try:
                self._audio_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
