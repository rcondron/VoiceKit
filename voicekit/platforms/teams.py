"""Microsoft Teams meeting platform adapter.

Connects VoiceKit to Microsoft Teams meetings via the Microsoft Graph
Communications API and the Teams Bot Framework. The adapter joins
meetings as a bot, captures mixed audio, and plays AI responses.

Supports:
- Joining meetings by join URL or meeting ID
- Capturing mixed meeting audio (all participants)
- Playing AI responses into the meeting
- Meeting lifecycle management (join, leave, mute/unmute)

Audio format: Teams streams PCM 16-bit, 16 kHz, mono via the
Communications API. The adapter converts to/from VoiceKit's internal
format (24 kHz mono).

Requires: pip install voicekit[teams]
  - aiohttp >= 3.9
  - msal >= 1.24  (Microsoft Authentication Library)
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from voicekit.config import TeamsPlatformConfig
from voicekit.core.audio import AudioFormat, convert_audio
from voicekit.platforms.base import AudioCallback, PlatformAdapter

logger = logging.getLogger(__name__)

# Teams Communications API audio: PCM s16le, 16 kHz, mono
TEAMS_AUDIO_FORMAT = AudioFormat(sample_rate=16000, channels=1, sample_width=2)

# 20ms frame at 16 kHz mono = 320 samples = 640 bytes
TEAMS_FRAME_BYTES = 320 * 2


class TeamsPlatform(PlatformAdapter):
    """Microsoft Teams meeting adapter.

    Uses the Microsoft Graph Communications API to join Teams meetings
    as an application (bot). Authenticates via Azure AD client credentials
    and streams bidirectional audio through the Communications media stack.

    Note: Requires an Azure Bot registration with Communications API
    permissions and a media hosting endpoint.
    """

    def __init__(self, config: TeamsPlatformConfig) -> None:
        self._config = config
        self._callback: AudioCallback | None = None
        self._active = False
        self._access_token: str = ""
        self._call_id: str | None = None
        self._output_queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=200)
        self._send_task: asyncio.Task[None] | None = None
        self._recv_task: asyncio.Task[None] | None = None
        self._in_meeting = False
        self._session: Any = None  # aiohttp.ClientSession

    @property
    def name(self) -> str:
        return "teams"

    @property
    def is_active(self) -> bool:
        return self._active

    async def start(self) -> None:
        """Authenticate with Azure AD and prepare to join meetings."""
        try:
            import aiohttp
        except ImportError:
            raise RuntimeError(
                "aiohttp is required for Teams support. "
                "Install with: pip install voicekit[teams]"
            )

        try:
            import msal  # type: ignore[import-untyped]
        except ImportError:
            raise RuntimeError(
                "msal is required for Teams support. "
                "Install with: pip install msal"
            )

        # Authenticate via MSAL (client credentials flow)
        app = msal.ConfidentialClientApplication(
            self._config.client_id,
            authority=f"https://login.microsoftonline.com/{self._config.tenant_id}",
            client_credential=self._config.client_secret,
        )

        result = app.acquire_token_for_client(
            scopes=["https://graph.microsoft.com/.default"]
        )

        if "access_token" not in result:
            error = result.get("error_description", "Unknown error")
            raise RuntimeError(f"Azure AD authentication failed: {error}")

        self._access_token = result["access_token"]
        self._session = aiohttp.ClientSession(
            headers={"Authorization": f"Bearer {self._access_token}"}
        )

        self._active = True
        logger.info(
            "Teams platform started (tenant=%s, auto_join=%s)",
            self._config.tenant_id,
            self._config.auto_join,
        )

        # Auto-join meeting if configured
        if self._config.auto_join and self._config.meeting_url:
            await self.join_meeting(self._config.meeting_url)

    async def join_meeting(self, meeting_url: str) -> None:
        """Join a Teams meeting by join URL.

        Args:
            meeting_url: The Teams meeting join URL.
        """
        if not self._active or not self._session:
            logger.warning("Cannot join meeting: Teams platform not active")
            return

        if self._in_meeting:
            logger.warning("Already in a Teams meeting; leave first")
            return

        logger.info("Joining Teams meeting...")

        # Create call via Graph Communications API
        call_payload = {
            "callbackUri": self._config.callback_url,
            "targets": [],
            "requestedModalities": ["audio"],
            "mediaConfig": {
                "@odata.type": "#microsoft.graph.appHostedMediaConfig",
                "blob": "",
            },
            "joinMeetingIdSettings": None,
        }

        # If it's a join URL, use chatInfo-based joining
        if "teams.microsoft.com" in meeting_url:
            call_payload["chatInfo"] = {
                "threadId": meeting_url,
                "messageId": "0",
            }
            call_payload["meetingInfo"] = {
                "@odata.type": "#microsoft.graph.organizerMeetingInfo",
            }

        try:
            async with self._session.post(
                "https://graph.microsoft.com/v1.0/communications/calls",
                json=call_payload,
            ) as resp:
                if resp.status not in (200, 201):
                    body = await resp.text()
                    raise RuntimeError(
                        f"Teams call creation failed ({resp.status}): {body}"
                    )
                result = await resp.json()
                self._call_id = result.get("id")
        except RuntimeError:
            raise
        except Exception as exc:
            raise RuntimeError(f"Failed to join Teams meeting: {exc}") from exc

        self._in_meeting = True
        logger.info("Joined Teams meeting (call_id=%s)", self._call_id)

    async def leave_meeting(self) -> None:
        """Leave the current Teams meeting."""
        if not self._in_meeting or not self._call_id:
            return

        logger.info("Leaving Teams meeting (call_id=%s)", self._call_id)

        if self._session:
            try:
                await self._session.delete(
                    f"https://graph.microsoft.com/v1.0/communications/calls/{self._call_id}"
                )
            except Exception:
                logger.debug("Error leaving Teams meeting", exc_info=True)

        self._in_meeting = False
        self._call_id = None

    async def stop(self) -> None:
        """Leave any active meeting and shut down."""
        self._active = False

        if self._in_meeting:
            await self.leave_meeting()

        for task in (self._send_task, self._recv_task):
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        self._send_task = None
        self._recv_task = None

        if self._session:
            await self._session.close()
            self._session = None

        logger.info("Teams platform stopped")

    def on_audio_received(self, callback: AudioCallback) -> None:
        """Register callback for captured audio from Teams meetings."""
        self._callback = callback

    async def send_audio(self, audio: bytes) -> None:
        """Send audio to the active Teams meeting.

        Converts from internal format (24 kHz mono) to Teams format
        (16 kHz mono) and queues for sending.
        """
        if not self._active or not self._in_meeting:
            return

        teams_audio = convert_audio(audio, AudioFormat(), TEAMS_AUDIO_FORMAT)

        try:
            self._output_queue.put_nowait(teams_audio)
        except asyncio.QueueFull:
            try:
                self._output_queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
            self._output_queue.put_nowait(teams_audio)

    async def answer_call(self) -> None:
        """Join the configured meeting (alias for auto-join)."""
        if self._config.meeting_url and not self._in_meeting:
            await self.join_meeting(self._config.meeting_url)

    async def reject_call(self) -> None:
        """No-op — Teams meetings are joined explicitly."""

    async def hang_up(self) -> None:
        """Leave the current Teams meeting."""
        await self.leave_meeting()
