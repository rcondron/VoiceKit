"""Deepgram Voice Agent API provider.

Connects to Deepgram's Voice Agent API via WebSocket for real-time
voice conversations combining Deepgram STT, an LLM, and Deepgram TTS
in a single low-latency pipeline.

Reference: https://developers.deepgram.com/docs/voice-agent

Requires: pip install websockets
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

DEEPGRAM_AGENT_URL = "wss://agent.deepgram.com/agent"


class DeepgramVoiceAgentProvider(VoiceProvider):
    """Deepgram Voice Agent API provider.

    Streams PCM audio over a WebSocket connection using the Deepgram
    Voice Agent protocol. The agent handles STT, LLM reasoning, and
    TTS in a single round-trip.
    """

    def __init__(self, config: ProviderConfig) -> None:
        self._config = config
        self._ws: object | None = None
        self._audio_queue: asyncio.Queue[bytes] = asyncio.Queue()
        self._connected = False
        self._receive_task: asyncio.Task[None] | None = None

    @property
    def name(self) -> str:
        return "deepgram"

    @property
    def is_connected(self) -> bool:
        return self._connected

    async def connect(self) -> None:
        """Connect to the Deepgram Voice Agent WebSocket."""
        if self._connected:
            return

        try:
            import websockets
        except ImportError:
            raise RuntimeError(
                "websockets is required for Deepgram support."
            )

        headers = {"Authorization": f"Token {self._config.api_key}"}

        logger.info("Connecting to Deepgram Voice Agent API...")

        try:
            self._ws = await websockets.connect(
                DEEPGRAM_AGENT_URL,
                additional_headers=headers,
                max_size=None,
            )
            self._connected = True
            logger.info("Connected to Deepgram Voice Agent API")

            self._receive_task = asyncio.create_task(self._receive_loop())

            await self._configure_session()

        except Exception as exc:
            self._connected = False
            raise ConnectionError(
                f"Failed to connect to Deepgram Voice Agent: {exc}"
            ) from exc

    async def _configure_session(self) -> None:
        """Send settings configuration to the Deepgram Voice Agent."""
        if not self._ws:
            return

        settings = {
            "type": "SettingsConfiguration",
            "audio": {
                "input": {
                    "encoding": "linear16",
                    "sample_rate": 24000,
                },
                "output": {
                    "encoding": "linear16",
                    "sample_rate": 24000,
                    "container": "none",
                },
            },
            "agent": {
                "listen": {
                    "model": "nova-3",
                },
                "think": {
                    "provider": {
                        "type": self._config.model or "open_ai",
                    },
                    "model": "gpt-4o-mini",
                    "instructions": self._config.instructions,
                },
                "speak": {
                    "model": "aura-2-en",
                },
            },
        }

        await self._ws.send(json.dumps(settings))  # type: ignore[union-attr]
        logger.debug("Deepgram Voice Agent session configured")

    async def send_audio(self, audio: bytes) -> None:
        """Send audio to the Deepgram Voice Agent.

        Deepgram accepts raw binary PCM frames directly over the WebSocket.

        Args:
            audio: PCM 16-bit, 24 kHz, mono audio data.
        """
        if not self._ws or not self._connected:
            return

        try:
            await self._ws.send(audio)  # type: ignore[union-attr]
        except Exception:
            logger.warning("WebSocket closed while sending audio to Deepgram")
            self._connected = False

    async def receive_audio(self) -> AsyncIterator[bytes]:
        """Yield audio chunks received from the Deepgram Voice Agent."""
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

                # Deepgram sends binary audio frames directly
                if isinstance(raw_message, bytes):
                    if raw_message:
                        await self._audio_queue.put(raw_message)
                    continue

                # Text messages are JSON control messages
                try:
                    message = json.loads(raw_message)
                except json.JSONDecodeError:
                    continue

                await self._handle_message(message)

        except asyncio.CancelledError:
            logger.debug("Deepgram receive loop cancelled")
        except Exception:
            logger.exception("Unexpected error in Deepgram receive loop")
        finally:
            self._connected = False

    async def _handle_message(self, message: dict) -> None:
        """Process a single Deepgram control message."""
        msg_type = message.get("type", "")

        if msg_type == "Welcome":
            logger.info("Deepgram Voice Agent session started")

        elif msg_type == "SettingsApplied":
            logger.debug("Deepgram settings applied")

        elif msg_type == "ConversationText":
            role = message.get("role", "")
            content = message.get("content", "")
            if content:
                logger.debug("Deepgram %s: %s", role, content[:100])

        elif msg_type == "UserStartedSpeaking":
            logger.debug("Deepgram: user started speaking")

        elif msg_type == "AgentStartedSpeaking":
            logger.debug("Deepgram: agent started speaking")

        elif msg_type == "AgentAudioDone":
            logger.debug("Deepgram: agent audio complete")

        elif msg_type == "Error":
            description = message.get("description", "")
            logger.error("Deepgram error: %s", description)

        else:
            logger.debug("Deepgram unhandled message: %s", msg_type)

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
                # Send close frame
                close_msg = json.dumps({"type": "CloseStream"})
                await self._ws.send(close_msg)  # type: ignore[union-attr]
                await self._ws.close()  # type: ignore[union-attr]
            except Exception:
                logger.debug("Error closing Deepgram WebSocket", exc_info=True)
            self._ws = None

        while not self._audio_queue.empty():
            try:
                self._audio_queue.get_nowait()
            except asyncio.QueueEmpty:
                break

        logger.info("Disconnected from Deepgram Voice Agent API")

    async def send_text(self, text: str) -> None:
        """Inject a text message into the conversation."""
        if not self._ws or not self._connected:
            return

        msg = {
            "type": "InjectAgentMessage",
            "message": text,
        }
        await self._ws.send(json.dumps(msg))  # type: ignore[union-attr]

    async def interrupt(self) -> None:
        """Interrupt the current agent response."""
        while not self._audio_queue.empty():
            try:
                self._audio_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
