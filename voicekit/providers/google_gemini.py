"""Google Gemini Live API voice provider.

Connects to Google's Gemini Multimodal Live API via WebSocket for
real-time bidirectional voice conversations. The API streams PCM audio
in and out, similar to the OpenAI Realtime API.

Reference: https://ai.google.dev/gemini-api/docs/multimodal-live

Requires: pip install google-genai
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

GEMINI_LIVE_URL = "wss://generativelanguage.googleapis.com/ws/google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent"


class GeminiLiveProvider(VoiceProvider):
    """Google Gemini Multimodal Live API voice provider.

    Streams PCM audio over a WebSocket connection using the Gemini Live
    protocol. Handles session configuration, audio input buffering,
    and response audio streaming.
    """

    def __init__(self, config: ProviderConfig) -> None:
        self._config = config
        self._ws: object | None = None
        self._audio_queue: asyncio.Queue[bytes] = asyncio.Queue()
        self._connected = False
        self._receive_task: asyncio.Task[None] | None = None

    @property
    def name(self) -> str:
        return "google_gemini"

    @property
    def is_connected(self) -> bool:
        return self._connected

    async def connect(self) -> None:
        """Connect to the Gemini Live API WebSocket."""
        if self._connected:
            return

        try:
            import websockets
        except ImportError:
            raise RuntimeError(
                "websockets is required for Gemini Live support."
            )

        model = self._config.model or "gemini-2.0-flash-exp"
        url = f"{GEMINI_LIVE_URL}?key={self._config.api_key}"

        logger.info("Connecting to Gemini Live API (model=%s)...", model)

        try:
            self._ws = await websockets.connect(url, max_size=None)
            self._connected = True
            logger.info("Connected to Gemini Live API")

            # Start background message handler
            self._receive_task = asyncio.create_task(self._receive_loop())

            # Send setup message
            await self._configure_session(model)

        except Exception as exc:
            self._connected = False
            raise ConnectionError(
                f"Failed to connect to Gemini Live API: {exc}"
            ) from exc

    async def _configure_session(self, model: str) -> None:
        """Send setup configuration to the Gemini Live API."""
        if not self._ws:
            return

        setup_msg = {
            "setup": {
                "model": f"models/{model}",
                "generationConfig": {
                    "responseModalities": ["AUDIO"],
                    "speechConfig": {
                        "voiceConfig": {
                            "prebuiltVoiceConfig": {
                                "voiceName": self._config.voice or "Aoede",
                            }
                        }
                    },
                },
                "systemInstruction": {
                    "parts": [{"text": self._config.instructions}]
                },
            }
        }

        await self._ws.send(json.dumps(setup_msg))  # type: ignore[union-attr]
        logger.debug("Gemini session configured: voice=%s", self._config.voice)

    async def send_audio(self, audio: bytes) -> None:
        """Send audio to the Gemini Live API.

        Audio is base64-encoded and sent as a realtimeInput message.

        Args:
            audio: PCM 16-bit, 24 kHz, mono audio data.
        """
        if not self._ws or not self._connected:
            return

        encoded = base64.b64encode(audio).decode("ascii")
        msg = {
            "realtimeInput": {
                "mediaChunks": [
                    {
                        "mimeType": "audio/pcm;rate=24000",
                        "data": encoded,
                    }
                ]
            }
        }

        try:
            await self._ws.send(json.dumps(msg))  # type: ignore[union-attr]
        except Exception:
            logger.warning("WebSocket closed while sending audio")
            self._connected = False

    async def receive_audio(self) -> AsyncIterator[bytes]:
        """Yield audio chunks received from the Gemini Live API."""
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
                    logger.warning("Received non-JSON message from Gemini API")
                    continue

                await self._handle_message(message)

        except asyncio.CancelledError:
            logger.debug("Gemini receive loop cancelled")
        except Exception:
            logger.exception("Unexpected error in Gemini receive loop")
        finally:
            self._connected = False

    async def _handle_message(self, message: dict) -> None:
        """Process a single Gemini API message."""
        # Server setup complete
        if "setupComplete" in message:
            logger.info("Gemini Live session setup complete")
            return

        # Audio response data
        server_content = message.get("serverContent")
        if server_content:
            model_turn = server_content.get("modelTurn", {})
            parts = model_turn.get("parts", [])
            for part in parts:
                inline_data = part.get("inlineData", {})
                if inline_data.get("mimeType", "").startswith("audio/"):
                    audio_b64 = inline_data.get("data", "")
                    if audio_b64:
                        audio_bytes = base64.b64decode(audio_b64)
                        await self._audio_queue.put(audio_bytes)

            if server_content.get("turnComplete"):
                logger.debug("Gemini audio response complete")
            return

        # Tool calls (future use)
        if "toolCall" in message:
            logger.debug("Gemini tool call received (not handled)")

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
                logger.debug("Error closing Gemini WebSocket", exc_info=True)
            self._ws = None

        while not self._audio_queue.empty():
            try:
                self._audio_queue.get_nowait()
            except asyncio.QueueEmpty:
                break

        logger.info("Disconnected from Gemini Live API")

    async def send_text(self, text: str) -> None:
        """Send a text message to Gemini."""
        if not self._ws or not self._connected:
            return

        msg = {
            "clientContent": {
                "turns": [
                    {
                        "role": "user",
                        "parts": [{"text": text}],
                    }
                ],
                "turnComplete": True,
            }
        }
        await self._ws.send(json.dumps(msg))  # type: ignore[union-attr]

    async def interrupt(self) -> None:
        """Interrupt the current Gemini response."""
        # Gemini Live handles barge-in via VAD on the audio stream
        while not self._audio_queue.empty():
            try:
                self._audio_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
