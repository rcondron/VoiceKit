"""Google Meet platform adapter via browser automation.

Joins Google Meet calls by automating a Chromium browser instance using
Playwright. Since Google Meet has no public bot SDK, this adapter launches
a headless (or headed) browser, navigates to the meeting URL, and captures
audio via virtual audio devices.

Architecture:
  Playwright (Chromium) ←→ PulseAudio virtual sinks ←→ VoiceKit Audio Router

The browser renders the Meet page and routes its audio output through a
PulseAudio null-sink (capture side), while VoiceKit injects AI responses
into the browser's microphone via another null-sink (playback side).

Supports:
- Joining meetings by URL
- Auto-dismiss "join now" / "ask to join" dialogs
- Audio capture via PulseAudio per-application routing
- Audio injection via virtual microphone sink
- Automatic muting of the bot's camera

Audio format: 48 kHz mono via PulseAudio (auto-converted from internal 24 kHz).

Requires:
  pip install voicekit[meet]
    - playwright >= 1.40
  System: PulseAudio (or PipeWire with pulseaudio-utils), xdotool
  Setup: python -m playwright install chromium
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from voicekit.config import GoogleMeetPlatformConfig
from voicekit.core.audio import AudioFormat, convert_audio
from voicekit.platforms.base import AudioCallback, PlatformAdapter

logger = logging.getLogger(__name__)

# Meet audio via PulseAudio: 48 kHz, mono
MEET_AUDIO_FORMAT = AudioFormat(sample_rate=48000, channels=1, sample_width=2)


class GoogleMeetPlatform(PlatformAdapter):
    """Google Meet adapter using Playwright browser automation.

    Launches a Chromium instance, joins a meeting URL, and routes audio
    bidirectionally through PulseAudio virtual devices.

    NOTE: This approach is inherently fragile — Meet's DOM structure may
    change without notice. CSS selectors are based on observed patterns
    and may need updating when Meet updates its UI. For production use,
    consider Google's Companion Mode API when it becomes available.
    """

    def __init__(self, config: GoogleMeetPlatformConfig) -> None:
        self._config = config
        self._callback: AudioCallback | None = None
        self._active = False
        self._browser: Any = None
        self._page: Any = None
        self._pulse_bridge: Any = None  # PulseAudioBridge
        self._capture_task: asyncio.Task[None] | None = None

    @property
    def name(self) -> str:
        return "google_meet"

    @property
    def is_active(self) -> bool:
        return self._active

    async def start(self) -> None:
        """Launch browser and prepare PulseAudio routing."""
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            raise RuntimeError(
                "playwright is required for Google Meet support. "
                "Install with: pip install playwright && python -m playwright install chromium"
            )

        from voicekit.core.pulse_bridge import PulseAudioBridge

        # Set up PulseAudio virtual sinks for this browser instance
        self._pulse_bridge = PulseAudioBridge(
            app_name="chromium",
            sink_name=self._config.pulse_sink_name,
        )
        await self._pulse_bridge.setup()

        # Launch browser with fake audio device pointing to our virtual sink
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=self._config.headless,
            args=[
                "--use-fake-ui-for-media-stream",  # Auto-allow mic/camera
                "--use-fake-device-for-media-stream",  # Use virtual devices
                "--disable-features=WebRtcHideLocalIpsWithMdns",
                "--no-sandbox",
                f"--alsa-output-device=pulse",
            ],
        )

        context = await self._browser.new_context(
            permissions=["microphone", "camera"],
        )
        self._page = await context.new_page()

        self._active = True
        logger.info(
            "Google Meet platform started (headless=%s, sink=%s)",
            self._config.headless,
            self._config.pulse_sink_name,
        )

        # Auto-join if meeting URL is configured
        if self._config.meeting_url:
            await self.join_meeting(self._config.meeting_url)

    async def join_meeting(self, meeting_url: str) -> None:
        """Navigate to a Google Meet URL and join the call.

        Handles the pre-join screen by clicking through permission dialogs,
        disabling camera, and clicking "Join now" or "Ask to join".

        Args:
            meeting_url: Full Google Meet URL (https://meet.google.com/xxx-xxxx-xxx).
        """
        if not self._page:
            logger.warning("Cannot join meeting: browser not started")
            return

        logger.info("Joining Google Meet: %s", meeting_url)
        await self._page.goto(meeting_url)

        # Wait for the page to load the pre-join screen
        await self._page.wait_for_timeout(3000)

        # Try to disable camera (click camera toggle button)
        # NOTE: These selectors are based on observed Meet UI and may break.
        try:
            camera_btn = self._page.locator('[data-is-muted][aria-label*="camera" i]')
            if await camera_btn.count() > 0:
                await camera_btn.first.click()
                logger.debug("Camera disabled")
        except Exception:
            logger.debug("Could not find camera toggle (may already be off)")

        # Try to disable microphone (we inject audio via PulseAudio, not the real mic)
        # Actually, we want the mic ON so the browser captures our virtual mic input
        # But we mute the real mic and route through PulseAudio instead.

        # Click "Join now" or "Ask to join" button
        await self._page.wait_for_timeout(2000)
        try:
            # Try multiple known button selectors
            for selector in [
                'button:has-text("Join now")',
                'button:has-text("Ask to join")',
                'button:has-text("Join")',
                '[jsname="Qx7uuf"]',  # Known jsname for join button
            ]:
                btn = self._page.locator(selector)
                if await btn.count() > 0:
                    await btn.first.click()
                    logger.info("Clicked join button")
                    break
            else:
                logger.warning("Could not find join button — may need manual join")
        except Exception:
            logger.warning("Error clicking join button", exc_info=True)

        # Wait for meeting to load
        await self._page.wait_for_timeout(3000)

        # Start audio capture from PulseAudio
        if self._pulse_bridge:
            self._capture_task = asyncio.create_task(self._audio_capture_loop())

        logger.info("Joined Google Meet meeting")

    async def _audio_capture_loop(self) -> None:
        """Continuously capture audio from the browser via PulseAudio."""
        if not self._pulse_bridge or not self._callback:
            return

        try:
            async for chunk in self._pulse_bridge.capture_audio():
                if not self._active:
                    break
                # Convert from Meet format to internal
                internal = convert_audio(chunk, MEET_AUDIO_FORMAT, AudioFormat())
                await self._callback(internal)
        except asyncio.CancelledError:
            pass
        except Exception:
            if self._active:
                logger.exception("Audio capture error in Google Meet")

    async def leave_meeting(self) -> None:
        """Leave the current Google Meet call."""
        if not self._page:
            return

        logger.info("Leaving Google Meet")
        try:
            # Click the hang-up button
            hangup = self._page.locator('[aria-label*="Leave call" i], [aria-label*="leave" i]')
            if await hangup.count() > 0:
                await hangup.first.click()
        except Exception:
            logger.debug("Could not click leave button")

    async def stop(self) -> None:
        """Close browser and clean up PulseAudio."""
        self._active = False

        if self._capture_task and not self._capture_task.done():
            self._capture_task.cancel()
            try:
                await self._capture_task
            except asyncio.CancelledError:
                pass

        if self._browser:
            try:
                await self._browser.close()
            except Exception:
                pass
            self._browser = None

        if hasattr(self, "_playwright") and self._playwright:
            await self._playwright.stop()

        if self._pulse_bridge:
            await self._pulse_bridge.cleanup()
            self._pulse_bridge = None

        logger.info("Google Meet platform stopped")

    def on_audio_received(self, callback: AudioCallback) -> None:
        self._callback = callback

    async def send_audio(self, audio: bytes) -> None:
        """Inject audio into the browser's microphone via PulseAudio."""
        if not self._active or not self._pulse_bridge:
            return

        meet_audio = convert_audio(audio, AudioFormat(), MEET_AUDIO_FORMAT)
        await self._pulse_bridge.inject_audio(meet_audio)

    async def answer_call(self) -> None:
        if self._config.meeting_url:
            await self.join_meeting(self._config.meeting_url)

    async def hang_up(self) -> None:
        await self.leave_meeting()
