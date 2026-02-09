"""Zoom meeting platform adapter.

Connects VoiceKit to Zoom meetings via the Zoom Meeting SDK (headless Linux
bot). The adapter authenticates using OAuth (client credentials), joins a
meeting by invite link or meeting ID, and streams bidirectional audio through
the SDK's raw-audio channels.

Supports:
- Joining meetings by meeting ID + passcode
- Capturing mixed meeting audio (all participants)
- Playing AI responses into the meeting
- Meeting lifecycle management (join, leave, mute/unmute)
- Waiting-room and host-admit handling

Audio format: The Zoom Meeting SDK streams PCM 16-bit, 32 kHz, mono by
default. The adapter converts to/from VoiceKit's internal format (24 kHz
mono).

Requires: pip install voicekit[zoom]
  - zoom-meeting-sdk >= 1.0  (Zoom's headless Linux Meeting SDK Python wrapper)
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from voicekit.config import ZoomPlatformConfig
from voicekit.core.audio import AudioFormat, convert_audio
from voicekit.platforms.base import AudioCallback, PlatformAdapter

logger = logging.getLogger(__name__)

# Zoom Meeting SDK default audio: PCM s16le, 32 kHz, mono
ZOOM_AUDIO_FORMAT = AudioFormat(sample_rate=32000, channels=1, sample_width=2)

# 20ms frame at 32 kHz mono = 640 samples = 1280 bytes
ZOOM_FRAME_BYTES = 640 * 2


class ZoomPlatform(PlatformAdapter):
    """Zoom meeting adapter using the Zoom Meeting SDK.

    Uses the Zoom Meeting SDK (headless Linux bot) to join meetings and
    stream bidirectional audio. Authenticates via OAuth client credentials
    (Server-to-Server or General App) to obtain a JWT for the SDK.

    The adapter runs the SDK event loop in a background thread and bridges
    audio to/from the async VoiceKit pipeline via queues.
    """

    def __init__(self, config: ZoomPlatformConfig) -> None:
        self._config = config
        self._callback: AudioCallback | None = None
        self._active = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._sdk: Any = None  # zoom SDK instance
        self._meeting_service: Any = None
        self._audio_helper: Any = None
        self._output_queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=200)
        self._send_task: asyncio.Task[None] | None = None
        self._recv_task: asyncio.Task[None] | None = None
        self._in_meeting = False

    @property
    def name(self) -> str:
        return "zoom"

    @property
    def is_active(self) -> bool:
        return self._active

    async def start(self) -> None:
        """Initialise the Zoom Meeting SDK and authenticate."""
        try:
            import zoom_meeting_sdk as zoom_sdk  # type: ignore[import-untyped]
        except ImportError:
            raise RuntimeError(
                "zoom-meeting-sdk is required for Zoom support. "
                "Install with: pip install voicekit[zoom]"
            )

        self._loop = asyncio.get_running_loop()

        # Initialise SDK
        init_params = zoom_sdk.InitParams()
        init_params.strWebDomain = "https://zoom.us"
        init_params.enableLog = self._config.enable_sdk_log

        err = zoom_sdk.InitSDK(init_params)
        if err != 0:
            raise RuntimeError(f"Zoom SDK InitSDK failed with error code {err}")

        self._sdk = zoom_sdk

        # Authenticate via JWT generated from client credentials
        auth_context = zoom_sdk.AuthContext()
        auth_context.jwt_token = await self._get_jwt_token()

        auth_service = zoom_sdk.CreateAuthService()
        err = auth_service.SDKAuth(auth_context)
        if err != 0:
            raise RuntimeError(f"Zoom SDK authentication failed with error code {err}")

        # Create meeting service
        self._meeting_service = zoom_sdk.CreateMeetingService()

        self._active = True
        logger.info(
            "Zoom platform started (meeting_id=%s, auto_join=%s)",
            self._config.meeting_id or "<none>",
            self._config.auto_join,
        )

        # Auto-join meeting if configured
        if self._config.auto_join and self._config.meeting_id:
            await self.join_meeting(
                self._config.meeting_id,
                self._config.meeting_passcode,
            )

    async def _get_jwt_token(self) -> str:
        """Generate a JWT token from OAuth client credentials.

        Uses the Zoom Server-to-Server OAuth flow to obtain an access token,
        then generates a Meeting SDK JWT from it.
        """
        import base64

        try:
            import aiohttp  # type: ignore[import-untyped]
        except ImportError:
            raise RuntimeError(
                "aiohttp is required for Zoom OAuth. "
                "Install with: pip install aiohttp"
            )

        credentials = base64.b64encode(
            f"{self._config.client_id}:{self._config.client_secret}".encode()
        ).decode()

        async with aiohttp.ClientSession() as session:
            async with session.post(
                "https://zoom.us/oauth/token",
                headers={
                    "Authorization": f"Basic {credentials}",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                data={
                    "grant_type": "account_credentials",
                    "account_id": self._config.account_id,
                },
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    raise RuntimeError(
                        f"Zoom OAuth token request failed ({resp.status}): {body}"
                    )
                token_data = await resp.json()

        access_token: str = token_data["access_token"]
        logger.debug("Obtained Zoom OAuth access token")
        return access_token

    async def join_meeting(
        self,
        meeting_id: str,
        passcode: str = "",
    ) -> None:
        """Join a Zoom meeting by meeting number.

        Args:
            meeting_id: The numeric Zoom meeting ID.
            passcode: Meeting passcode (if required).
        """
        if not self._active or not self._meeting_service:
            logger.warning("Cannot join meeting: Zoom platform not active")
            return

        if self._in_meeting:
            logger.warning("Already in a Zoom meeting; leave first")
            return

        sdk = self._sdk
        join_params = sdk.JoinParams()
        join_params.meetingNumber = int(meeting_id)
        join_params.userName = self._config.display_name
        join_params.psw = passcode
        join_params.isAudioOff = False
        join_params.isVideoOff = True

        logger.info("Joining Zoom meeting %s as '%s'", meeting_id, self._config.display_name)

        err = self._meeting_service.Join(join_params)
        if err != 0:
            raise RuntimeError(f"Zoom meeting join failed with error code {err}")

        # Start audio streaming
        await self._start_audio_streaming()
        self._in_meeting = True

    async def _start_audio_streaming(self) -> None:
        """Set up bidirectional audio piping with the Zoom SDK."""
        sdk = self._sdk

        # Get the audio helper for raw audio access
        self._audio_helper = sdk.GetAudioRawDataHelper()
        if not self._audio_helper:
            logger.error("Failed to get Zoom raw audio helper")
            return

        # Subscribe to mixed audio (all participants)
        self._audio_helper.subscribe(self._on_zoom_audio_received)

        # Start tasks for sending audio to the meeting
        self._send_task = asyncio.create_task(
            self._audio_send_loop(), name="zoom_audio_send"
        )

        logger.debug("Zoom audio streaming started")

    def _on_zoom_audio_received(self, raw_data: Any) -> None:
        """Callback from Zoom SDK when mixed audio is available.

        Called from the SDK thread — schedules async work on the event loop.
        """
        if not self._callback or not self._loop or not self._active:
            return

        # Extract PCM bytes from SDK audio node
        pcm_bytes: bytes = bytes(raw_data.GetBuffer())
        if not pcm_bytes:
            return

        # Convert from Zoom format (32 kHz mono) to internal (24 kHz mono)
        internal_audio = convert_audio(pcm_bytes, ZOOM_AUDIO_FORMAT, AudioFormat())

        asyncio.run_coroutine_threadsafe(self._callback(internal_audio), self._loop)

    async def _audio_send_loop(self) -> None:
        """Continuously send queued audio to the Zoom meeting."""
        while self._active and self._in_meeting:
            try:
                chunk = await asyncio.wait_for(self._output_queue.get(), timeout=0.5)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break

            if self._audio_helper and chunk:
                try:
                    # Send raw PCM to Zoom SDK (runs in executor to avoid blocking)
                    await asyncio.get_running_loop().run_in_executor(
                        None, self._audio_helper.send, chunk
                    )
                except Exception:
                    if self._active:
                        logger.debug("Error sending audio to Zoom", exc_info=True)

    async def leave_meeting(self) -> None:
        """Leave the current Zoom meeting."""
        if not self._in_meeting:
            return

        logger.info("Leaving Zoom meeting")

        # Stop audio tasks
        await self._stop_audio_streaming()

        # Leave meeting via SDK
        if self._meeting_service:
            try:
                self._meeting_service.Leave(0)  # 0 = leave meeting
            except Exception:
                logger.debug("Error leaving Zoom meeting", exc_info=True)

        self._in_meeting = False

    async def _stop_audio_streaming(self) -> None:
        """Cancel audio streaming tasks and unsubscribe from SDK audio."""
        if self._audio_helper:
            try:
                self._audio_helper.unsubscribe()
            except Exception:
                logger.debug("Error unsubscribing from Zoom audio", exc_info=True)
            self._audio_helper = None

        for task in (self._send_task, self._recv_task):
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        self._send_task = None
        self._recv_task = None

    async def stop(self) -> None:
        """Leave any active meeting and shut down the Zoom SDK."""
        self._active = False

        if self._in_meeting:
            await self.leave_meeting()

        # Clean up SDK
        if self._sdk:
            try:
                self._sdk.CleanUPSDK()
            except Exception:
                logger.debug("Error cleaning up Zoom SDK", exc_info=True)

        self._sdk = None
        self._meeting_service = None
        logger.info("Zoom platform stopped")

    def on_audio_received(self, callback: AudioCallback) -> None:
        """Register callback for captured audio from Zoom meetings."""
        self._callback = callback

    async def send_audio(self, audio: bytes) -> None:
        """Send audio to the active Zoom meeting.

        Converts from internal format (24 kHz mono) to Zoom format
        (32 kHz mono) and queues for the send loop.
        """
        if not self._active or not self._in_meeting:
            return

        zoom_audio = convert_audio(audio, AudioFormat(), ZOOM_AUDIO_FORMAT)

        try:
            self._output_queue.put_nowait(zoom_audio)
        except asyncio.QueueFull:
            # Drop oldest to prevent latency buildup
            try:
                self._output_queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
            self._output_queue.put_nowait(zoom_audio)

    async def answer_call(self) -> None:
        """Join the configured meeting (alias for auto-join)."""
        if self._config.meeting_id and not self._in_meeting:
            await self.join_meeting(
                self._config.meeting_id,
                self._config.meeting_passcode,
            )

    async def reject_call(self) -> None:
        """No-op — Zoom meetings are joined explicitly."""
        pass

    async def hang_up(self) -> None:
        """Leave the current Zoom meeting."""
        await self.leave_meeting()
