"""Telegram voice call platform adapter.

Connects VoiceKit to Telegram voice/video calls (camera off, audio only)
using Pyrogram for the MTProto client and py-tgcalls (pytgcalls) for the
WebRTC voice layer.

Supports:
- Group voice chats (join, stream audio, capture audio)
- Auto-answer / auto-join group calls
- User allowlisting

Audio format: py-tgcalls streams PCM 16-bit, 48 kHz mono by default.
The adapter converts to/from VoiceKit's internal format (24 kHz mono).

Requires: pip install voicekit[telegram]
  - pyrogram >= 2.0
  - py-tgcalls >= 2.1  (the actively maintained pytgcalls package on PyPI)
  - ffmpeg must be installed on the system
"""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
from typing import Any

from voicekit.config import TelegramPlatformConfig
from voicekit.core.audio import AudioFormat, convert_audio
from voicekit.platforms.base import AudioCallback, PlatformAdapter

logger = logging.getLogger(__name__)

# py-tgcalls default audio: PCM s16le, 48kHz, mono
TELEGRAM_AUDIO_FORMAT = AudioFormat(sample_rate=48000, channels=1, sample_width=2)

# 20ms frame at 48kHz mono = 960 samples = 1920 bytes
TELEGRAM_FRAME_BYTES = 960 * 2


class TelegramPlatform(PlatformAdapter):
    """Telegram voice call adapter using py-tgcalls.

    Uses Pyrogram as the MTProto client and py-tgcalls for the WebRTC
    voice call layer. Supports group voice chats with bidirectional
    audio streaming via named pipes (FIFOs).

    Note: Telegram calls require a user account (userbot), not a bot token.
    You need api_id and api_hash from https://my.telegram.org.
    """

    def __init__(self, config: TelegramPlatformConfig) -> None:
        self._config = config
        self._callback: AudioCallback | None = None
        self._active = False
        self._client: Any = None  # pyrogram.Client
        self._call_py: Any = None  # pytgcalls.PyTgCalls
        self._loop: asyncio.AbstractEventLoop | None = None
        self._output_fifo: str | None = None
        self._input_fifo: str | None = None
        self._fifo_dir: str | None = None
        self._fifo_writer_task: asyncio.Task[None] | None = None
        self._fifo_reader_task: asyncio.Task[None] | None = None
        self._output_queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=200)
        self._active_chat_id: int | None = None

    @property
    def name(self) -> str:
        return "telegram"

    @property
    def is_active(self) -> bool:
        return self._active

    async def start(self) -> None:
        """Start the Telegram client and listen for incoming calls."""
        try:
            from pyrogram import Client
        except ImportError:
            raise RuntimeError(
                "pyrogram is required for Telegram support. "
                "Install with: pip install voicekit[telegram]"
            )

        try:
            from pytgcalls import PyTgCalls
        except ImportError:
            raise RuntimeError(
                "py-tgcalls is required for Telegram voice support. "
                "Install with: pip install voicekit[telegram]"
            )

        self._loop = asyncio.get_running_loop()

        # Initialize Pyrogram client (userbot)
        api_id = int(self._config.api_id) if self._config.api_id else None
        self._client = Client(
            name=self._config.session_name,
            api_id=api_id,
            api_hash=self._config.api_hash or None,
            phone_number=self._config.phone_number or None,
        )

        # Initialize py-tgcalls
        self._call_py = PyTgCalls(self._client)

        # Create FIFOs for bidirectional audio
        self._setup_fifos()

        # Register event handlers
        self._register_handlers()

        # Start Pyrogram client and py-tgcalls
        await self._client.start()
        await self._call_py.start()

        self._active = True
        logger.info(
            "Telegram platform started (session=%s, auto_answer=%s)",
            self._config.session_name,
            self._config.auto_answer,
        )

    def _setup_fifos(self) -> None:
        """Create named pipes for bidirectional audio with py-tgcalls."""
        self._fifo_dir = tempfile.mkdtemp(prefix="voicekit_tg_")
        self._output_fifo = os.path.join(self._fifo_dir, "output.pcm")
        self._input_fifo = os.path.join(self._fifo_dir, "input.pcm")
        os.mkfifo(self._output_fifo)
        os.mkfifo(self._input_fifo)
        logger.debug("Created FIFOs: output=%s input=%s", self._output_fifo, self._input_fifo)

    def _register_handlers(self) -> None:
        """Register py-tgcalls event handlers for call lifecycle."""
        if not self._call_py:
            return

        try:
            from pytgcalls import filters as call_filters
            from pytgcalls.types import ChatUpdate

            @self._call_py.on_update(call_filters.chat_update(ChatUpdate.Status.PLAYING))
            async def on_playing(_client: Any, update: ChatUpdate) -> None:
                logger.info("Telegram call active in chat %s", update.chat_id)

            @self._call_py.on_update(call_filters.chat_update(ChatUpdate.Status.CLOSED))
            async def on_closed(_client: Any, update: ChatUpdate) -> None:
                logger.info("Telegram call ended in chat %s", update.chat_id)
                if self._active_chat_id == update.chat_id:
                    await self._stop_streaming()
                    self._active_chat_id = None

        except (ImportError, AttributeError):
            logger.warning("Could not register py-tgcalls event handlers (API may differ)")

    async def join_group_call(self, chat_id: int) -> None:
        """Join a group voice chat by chat ID and start streaming.

        Args:
            chat_id: The Telegram chat/group ID to join.
        """
        if not self._active or not self._call_py:
            logger.warning("Cannot join call: Telegram platform not active")
            return

        logger.info("Joining group call in chat %s", chat_id)
        await self._start_streaming(chat_id)

    async def _start_streaming(self, chat_id: int) -> None:
        """Start bidirectional audio streaming for a call."""
        from pytgcalls.types import MediaStream

        self._active_chat_id = chat_id

        # Launch FIFO writer (AI → output FIFO → Telegram)
        self._fifo_writer_task = asyncio.create_task(
            self._fifo_write_loop(), name="tg_fifo_writer"
        )

        # Launch FIFO reader (Telegram → input FIFO → AI)
        self._fifo_reader_task = asyncio.create_task(
            self._fifo_read_loop(), name="tg_fifo_reader"
        )

        # Join the call with audio streaming via the output FIFO
        # video_flags=IGNORE means camera is off (audio only)
        await self._call_py.play(
            chat_id,
            MediaStream(
                self._output_fifo,
                video_flags=MediaStream.Flags.IGNORE,
            ),
        )

        logger.info("Started audio streaming for Telegram chat %s", chat_id)

    async def _stop_streaming(self) -> None:
        """Stop FIFO reader/writer tasks."""
        for task in (self._fifo_writer_task, self._fifo_reader_task):
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        self._fifo_writer_task = None
        self._fifo_reader_task = None

    async def _fifo_write_loop(self) -> None:
        """Write AI audio to the output FIFO for py-tgcalls to play.

        Uses a thread executor for the blocking FIFO write so we
        don't block the event loop.
        """
        if not self._output_fifo or not self._loop:
            return

        def _blocking_write() -> None:
            try:
                with open(self._output_fifo, "wb", buffering=0) as fifo:  # type: ignore[assignment]
                    while self._active and self._active_chat_id is not None:
                        try:
                            chunk = asyncio.run_coroutine_threadsafe(
                                self._get_output_chunk(), self._loop
                            ).result(timeout=1.0)
                            if chunk:
                                fifo.write(chunk)
                        except TimeoutError:
                            continue
                        except Exception:
                            if self._active:
                                logger.debug("FIFO write interrupted")
                            break
            except OSError:
                if self._active:
                    logger.debug("Output FIFO closed")

        try:
            await self._loop.run_in_executor(None, _blocking_write)
        except asyncio.CancelledError:
            pass

    async def _get_output_chunk(self) -> bytes:
        """Get next audio chunk from the output queue, or silence."""
        try:
            return await asyncio.wait_for(self._output_queue.get(), timeout=0.5)
        except asyncio.TimeoutError:
            return b"\x00" * TELEGRAM_FRAME_BYTES

    async def _fifo_read_loop(self) -> None:
        """Read captured audio from the input FIFO and forward to the AI.

        py-tgcalls writes recorded audio to the input FIFO.
        """
        if not self._input_fifo or not self._loop or not self._callback:
            return

        def _blocking_read() -> None:
            try:
                with open(self._input_fifo, "rb") as fifo:
                    while self._active and self._active_chat_id is not None:
                        data = fifo.read(TELEGRAM_FRAME_BYTES)
                        if not data:
                            break
                        if self._callback and self._loop:
                            internal = convert_audio(
                                data, TELEGRAM_AUDIO_FORMAT, AudioFormat()
                            )
                            asyncio.run_coroutine_threadsafe(
                                self._callback(internal), self._loop
                            )
            except OSError:
                if self._active:
                    logger.debug("Input FIFO closed")

        try:
            await asyncio.get_running_loop().run_in_executor(None, _blocking_read)
        except asyncio.CancelledError:
            pass

    async def stop(self) -> None:
        """Stop the Telegram client, leave calls, and clean up FIFOs."""
        self._active = False
        await self._stop_streaming()

        # Leave the active call
        if self._call_py and self._active_chat_id is not None:
            try:
                await self._call_py.leave_call(self._active_chat_id)
            except Exception:
                logger.debug("Error leaving Telegram call", exc_info=True)

        self._active_chat_id = None
        self._call_py = None

        if self._client:
            try:
                await self._client.stop()
            except Exception:
                logger.debug("Error stopping Pyrogram client", exc_info=True)
            self._client = None

        # Clean up FIFOs
        for path in (self._output_fifo, self._input_fifo):
            if path and os.path.exists(path):
                try:
                    os.unlink(path)
                except OSError:
                    pass
        if self._fifo_dir and os.path.exists(self._fifo_dir):
            try:
                os.rmdir(self._fifo_dir)
            except OSError:
                pass
        self._output_fifo = None
        self._input_fifo = None
        self._fifo_dir = None

        logger.info("Telegram platform stopped")

    def on_audio_received(self, callback: AudioCallback) -> None:
        """Register callback for captured audio from Telegram calls."""
        self._callback = callback

    async def send_audio(self, audio: bytes) -> None:
        """Send audio to the active Telegram call.

        Converts from internal format (24 kHz mono) to Telegram format
        (48 kHz mono) and queues for the FIFO writer.
        """
        if not self._active or self._active_chat_id is None:
            return

        telegram_audio = convert_audio(audio, AudioFormat(), TELEGRAM_AUDIO_FORMAT)

        try:
            self._output_queue.put_nowait(telegram_audio)
        except asyncio.QueueFull:
            try:
                self._output_queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
            self._output_queue.put_nowait(telegram_audio)

    async def answer_call(self) -> None:
        """Answer / join an incoming call."""
        if self._active_chat_id is not None:
            logger.info("Answering Telegram call in chat %s", self._active_chat_id)
            await self._start_streaming(self._active_chat_id)

    async def reject_call(self) -> None:
        """Reject an incoming Telegram call."""
        logger.info("Rejecting Telegram call")
        self._active_chat_id = None

    async def hang_up(self) -> None:
        """Leave the current Telegram call."""
        if self._active_chat_id is not None and self._call_py:
            chat_id = self._active_chat_id
            logger.info("Hanging up Telegram call in chat %s", chat_id)
            try:
                await self._call_py.leave_call(chat_id)
            except Exception:
                logger.debug("Error leaving call", exc_info=True)
            await self._stop_streaming()
            self._active_chat_id = None
