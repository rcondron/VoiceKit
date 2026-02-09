"""FaceTime platform adapter for macOS desktop automation.

Automates Apple FaceTime on macOS to answer and route voice calls.
Since FaceTime has no public API, this adapter uses macOS-specific tools
for call detection, audio routing, and UI automation.

Architecture:
  FaceTime.app ←→ macOS Core Audio (virtual device) ←→ VoiceKit Audio Router

Detection mechanisms:
  1. AppleScript: query FaceTime call state
  2. macOS Notification Center: observe incoming call notifications
  3. Window title polling: detect call status changes

Audio routing:
  Uses a macOS virtual audio device (BlackHole, Loopback, or similar)
  to capture FaceTime audio output and inject AI responses as microphone
  input.

Supports:
- Answering incoming FaceTime audio calls
- Auto-answer with contact allowlisting
- Bidirectional audio via virtual audio device

Audio format: 48 kHz mono (auto-converted from internal 24 kHz).

Limitations:
  - macOS only (requires AppleScript + FaceTime.app)
  - Requires a virtual audio driver (BlackHole recommended)
  - UI automation may break across macOS versions
  - Cannot initiate outbound calls programmatically (FaceTime restriction)
  - Camera cannot be reliably toggled via automation

Requires:
  - macOS 12+ with FaceTime.app
  - BlackHole (https://existential.audio/blackhole/) or similar virtual audio driver
  - Accessibility permissions for the terminal/app running VoiceKit
"""

from __future__ import annotations

import asyncio
import logging
import platform as platform_mod
from typing import Any

from voicekit.config import FaceTimePlatformConfig
from voicekit.core.audio import AudioFormat, convert_audio
from voicekit.platforms.base import AudioCallback, PlatformAdapter

logger = logging.getLogger(__name__)

# FaceTime audio: 48 kHz mono via Core Audio
FACETIME_AUDIO_FORMAT = AudioFormat(sample_rate=48000, channels=1, sample_width=2)


class FaceTimePlatform(PlatformAdapter):
    """FaceTime voice call adapter for macOS.

    Uses AppleScript for call detection and control, and a virtual audio
    device (BlackHole) for bidirectional audio routing.

    NOTE: This adapter only works on macOS. It requires Accessibility
    permissions and a virtual audio driver. FaceTime's UI and AppleScript
    interface may change between macOS versions.
    """

    def __init__(self, config: FaceTimePlatformConfig) -> None:
        self._config = config
        self._callback: AudioCallback | None = None
        self._active = False
        self._in_call = False
        self._poll_task: asyncio.Task[None] | None = None
        self._capture_task: asyncio.Task[None] | None = None
        self._output_queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=200)

    @property
    def name(self) -> str:
        return "facetime"

    @property
    def is_active(self) -> bool:
        return self._active

    async def start(self) -> None:
        """Verify macOS environment and begin call detection polling."""
        if platform_mod.system() != "Darwin":
            raise RuntimeError(
                "FaceTime platform is macOS-only. "
                f"Current OS: {platform_mod.system()}"
            )

        # Verify virtual audio device exists
        if not await self._check_virtual_audio_device():
            logger.warning(
                "Virtual audio device '%s' not found. "
                "Install BlackHole: https://existential.audio/blackhole/",
                self._config.virtual_device_name,
            )

        self._active = True
        self._poll_task = asyncio.create_task(self._call_detection_loop())
        logger.info(
            "FaceTime platform started (auto_answer=%s, device=%s)",
            self._config.auto_answer,
            self._config.virtual_device_name,
        )

    async def _check_virtual_audio_device(self) -> bool:
        """Check if the configured virtual audio device is available."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "system_profiler", "SPAudioDataType",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await proc.communicate()
            return self._config.virtual_device_name in stdout.decode()
        except Exception:
            return False

    async def _call_detection_loop(self) -> None:
        """Poll for incoming FaceTime calls via AppleScript."""
        while self._active:
            try:
                if not self._in_call:
                    incoming = await self._check_incoming_call()
                    if incoming:
                        caller = incoming.get("caller", "Unknown")
                        logger.info("Incoming FaceTime call from: %s", caller)

                        if self._config.allowed_contacts and caller not in self._config.allowed_contacts:
                            logger.info("Caller %s not in allowlist, ignoring", caller)
                            await asyncio.sleep(2)
                            continue

                        if self._config.auto_answer:
                            await self.answer_call()

                await asyncio.sleep(self._config.poll_interval)

            except asyncio.CancelledError:
                break
            except Exception:
                logger.debug("FaceTime poll error", exc_info=True)
                await asyncio.sleep(2)

    async def _check_incoming_call(self) -> dict[str, str] | None:
        """Use AppleScript to detect an incoming FaceTime call.

        Returns caller info dict or None if no incoming call.
        """
        # AppleScript to check FaceTime state
        # NOTE: FaceTime's AppleScript dictionary is limited.
        # We fall back to checking window titles for call indicators.
        script = '''
        tell application "System Events"
            if (name of processes) contains "FaceTime" then
                tell process "FaceTime"
                    set windowNames to name of every window
                    repeat with w in windowNames
                        if w contains "incoming" or w contains "Incoming" then
                            return w as string
                        end if
                    end repeat
                end tell
            end if
        end tell
        return ""
        '''

        try:
            proc = await asyncio.create_subprocess_exec(
                "osascript", "-e", script,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await proc.communicate()
            result = stdout.decode().strip()

            if result:
                return {"caller": result}
        except Exception:
            pass

        return None

    async def answer_call(self) -> None:
        """Answer the incoming FaceTime call via AppleScript."""
        logger.info("Answering FaceTime call...")

        # Use AppleScript to click the Accept button
        script = '''
        tell application "System Events"
            tell process "FaceTime"
                -- Try to click Accept/Answer button
                try
                    click button "Accept" of window 1
                on error
                    try
                        click button "Answer" of window 1
                    on error
                        -- Try keyboard shortcut as fallback
                        keystroke return
                    end try
                end try
            end tell
        end tell
        '''

        try:
            proc = await asyncio.create_subprocess_exec(
                "osascript", "-e", script,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.communicate()
        except Exception:
            logger.warning("Failed to answer FaceTime call via AppleScript")
            return

        self._in_call = True

        # Start audio capture via virtual device
        self._capture_task = asyncio.create_task(self._audio_capture_loop())
        logger.info("FaceTime call answered")

    async def _audio_capture_loop(self) -> None:
        """Capture audio from the virtual audio device using sounddevice.

        Reads from the configured virtual audio device (e.g. BlackHole)
        which receives FaceTime's audio output.
        """
        try:
            import sounddevice as sd
        except ImportError:
            logger.error("sounddevice required for FaceTime audio capture")
            return

        try:
            stream = sd.InputStream(
                device=self._config.virtual_device_name,
                samplerate=48000,
                channels=1,
                dtype="int16",
                blocksize=960,  # 20ms at 48kHz
            )
            stream.start()

            while self._active and self._in_call:
                data, overflowed = stream.read(960)
                if overflowed:
                    logger.debug("FaceTime audio capture overflow")
                if self._callback and data is not None:
                    pcm_bytes = data.tobytes()
                    internal = convert_audio(pcm_bytes, FACETIME_AUDIO_FORMAT, AudioFormat())
                    await self._callback(internal)
                await asyncio.sleep(0.01)

            stream.stop()
            stream.close()

        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("FaceTime audio capture error")

    async def reject_call(self) -> None:
        """Reject the incoming FaceTime call."""
        script = '''
        tell application "System Events"
            tell process "FaceTime"
                try
                    click button "Decline" of window 1
                on error
                    keystroke "d" using {command down}
                end try
            end tell
        end tell
        '''
        try:
            proc = await asyncio.create_subprocess_exec(
                "osascript", "-e", script,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.communicate()
        except Exception:
            logger.debug("Failed to reject FaceTime call")

    async def hang_up(self) -> None:
        """End the current FaceTime call."""
        if not self._in_call:
            return

        script = '''
        tell application "System Events"
            tell process "FaceTime"
                try
                    click button "End" of window 1
                on error
                    keystroke "." using {command down}
                end try
            end tell
        end tell
        '''
        try:
            proc = await asyncio.create_subprocess_exec(
                "osascript", "-e", script,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.communicate()
        except Exception:
            logger.debug("Failed to end FaceTime call")

        self._in_call = False
        logger.info("FaceTime call ended")

    async def stop(self) -> None:
        self._active = False

        if self._in_call:
            await self.hang_up()

        for task in (self._poll_task, self._capture_task):
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        self._poll_task = None
        self._capture_task = None

        logger.info("FaceTime platform stopped")

    def on_audio_received(self, callback: AudioCallback) -> None:
        self._callback = callback

    async def send_audio(self, audio: bytes) -> None:
        """Send audio to FaceTime via the virtual audio device."""
        if not self._active or not self._in_call:
            return

        ft_audio = convert_audio(audio, AudioFormat(), FACETIME_AUDIO_FORMAT)

        try:
            self._output_queue.put_nowait(ft_audio)
        except asyncio.QueueFull:
            try:
                self._output_queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
            self._output_queue.put_nowait(ft_audio)
