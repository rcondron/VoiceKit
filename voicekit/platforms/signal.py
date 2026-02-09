"""Signal Desktop automation platform adapter.

Automates Signal Desktop to answer voice calls and route audio.
Supports two complementary mechanisms:

1. **signal-cli** (optional): If installed and configured, uses signal-cli's
   D-Bus interface for call detection and management. Provides the most
   reliable call control.

2. **Desktop automation**: Uses PulseAudio virtual sinks for audio routing
   and xdotool for window interaction (answering/rejecting calls).

Supports:
- Detecting incoming Signal calls via window monitoring or signal-cli
- Auto-answering calls
- Bidirectional audio via PulseAudio virtual sinks
- Contact allowlisting
- Call rejection

Audio format: PulseAudio captures PCM 16-bit, 48 kHz, mono.
The adapter converts to/from VoiceKit's internal format (24 kHz mono).

System requirements:
  - Signal Desktop
  - PulseAudio or PipeWire (with pulseaudio-utils)
  - xdotool (for keyboard/window simulation)
  - signal-cli (optional, for enhanced call detection)
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
from typing import Any

from voicekit.config import SignalPlatformConfig
from voicekit.core.audio import AudioFormat, convert_audio
from voicekit.platforms.base import AudioCallback, PlatformAdapter

logger = logging.getLogger(__name__)

# Signal Desktop voice calls use 48 kHz mono via PulseAudio
SIGNAL_AUDIO_FORMAT = AudioFormat(sample_rate=48000, channels=1, sample_width=2)

# 20ms frame at 48 kHz mono = 960 samples = 1920 bytes
SIGNAL_FRAME_BYTES = 960 * 2

# Window title patterns that indicate an incoming call
_CALL_TITLE_PATTERNS = (
    "incoming call",
    "incoming video call",
    "signal call",
    "ringing",
)

_ANSWER_KEY = "Return"
_REJECT_KEY = "Escape"


class SignalPlatform(PlatformAdapter):
    """Signal Desktop adapter using PulseAudio bridge and signal-cli.

    Routes audio bidirectionally between Signal Desktop's voice calls
    and VoiceKit's AI provider by:

    1. Creating PulseAudio virtual sinks (capture + inject).
    2. Moving Signal's audio streams to the virtual sinks.
    3. Reading captured call audio and forwarding to the AI.
    4. Playing AI responses into Signal's microphone input.

    Call detection uses a combination of signal-cli D-Bus events
    (when available) and window title/urgency polling as fallback.
    """

    def __init__(self, config: SignalPlatformConfig) -> None:
        self._config = config
        self._callback: AudioCallback | None = None
        self._active = False
        self._bridge: Any = None  # PulseAudioBridge (lazy)
        self._call_monitor_task: asyncio.Task[None] | None = None
        self._signalcli_monitor_task: asyncio.Task[None] | None = None
        self._in_call = False
        self._window_id: str | None = None
        self._has_signal_cli = False
        self._signalcli_proc: asyncio.subprocess.Process | None = None

    @property
    def name(self) -> str:
        return "signal"

    @property
    def is_active(self) -> bool:
        return self._active

    async def start(self) -> None:
        """Start the Signal platform adapter.

        Sets up PulseAudio virtual sinks, starts audio routing, and
        begins monitoring for incoming calls via signal-cli and/or
        window polling.
        """
        self._check_dependencies()

        from voicekit.core.pulse_bridge import PulseAudioBridge

        self._bridge = PulseAudioBridge(
            app_process_name=self._config.process_name,
            sink_prefix=self._config.pulse_sink_name,
            sample_rate=SIGNAL_AUDIO_FORMAT.sample_rate,
            channels=SIGNAL_AUDIO_FORMAT.channels,
        )

        # Create virtual PulseAudio sinks
        try:
            await self._bridge.setup()
        except RuntimeError:
            logger.exception("Failed to set up PulseAudio bridge for Signal")
            raise

        # Start audio capture/playback
        await self._bridge.start(self._on_captured_audio)

        # Try to start signal-cli monitoring
        if self._config.signal_cli_path and shutil.which(self._config.signal_cli_path):
            self._has_signal_cli = True
            self._signalcli_monitor_task = asyncio.create_task(
                self._signalcli_monitor_loop(), name="signal_cli_monitor"
            )
            logger.info("signal-cli available — using enhanced call detection")
        else:
            logger.info("signal-cli not found — using window-polling call detection")

        # Start window-based call detection (works alongside signal-cli)
        self._call_monitor_task = asyncio.create_task(
            self._call_monitor_loop(), name="signal_call_monitor"
        )

        self._active = True
        logger.info(
            "Signal platform started (process=%s, auto_answer=%s, signal_cli=%s)",
            self._config.process_name,
            self._config.auto_answer,
            self._has_signal_cli,
        )

    def _check_dependencies(self) -> None:
        """Verify required system tools are available."""
        missing = []
        for tool in ("pactl", "parec", "pacat"):
            if not shutil.which(tool):
                missing.append(tool)

        if missing:
            raise RuntimeError(
                f"Missing system tools for Signal audio: {', '.join(missing)}. "
                "Install pulseaudio-utils (or pipewire-pulse)."
            )

        if not shutil.which("xdotool"):
            logger.warning(
                "xdotool not found — auto-answer/call control via window will be "
                "disabled. Install with: sudo apt install xdotool"
            )

    async def _on_captured_audio(self, audio: bytes) -> None:
        """Handle audio captured from Signal via PulseAudio bridge."""
        if not self._callback or not self._in_call:
            return
        internal = convert_audio(audio, SIGNAL_AUDIO_FORMAT, AudioFormat())
        await self._callback(internal)

    async def stop(self) -> None:
        """Stop the Signal platform and clean up all resources."""
        self._active = False
        self._in_call = False

        # Stop monitor tasks
        for task in (self._call_monitor_task, self._signalcli_monitor_task):
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        self._call_monitor_task = None
        self._signalcli_monitor_task = None

        # Stop signal-cli subprocess
        if self._signalcli_proc and self._signalcli_proc.returncode is None:
            self._signalcli_proc.terminate()
            try:
                await asyncio.wait_for(self._signalcli_proc.wait(), timeout=3.0)
            except TimeoutError:
                self._signalcli_proc.kill()
                await self._signalcli_proc.wait()
        self._signalcli_proc = None

        # Stop PulseAudio bridge
        if self._bridge:
            await self._bridge.stop()
            self._bridge = None

        self._window_id = None
        logger.info("Signal platform stopped")

    def on_audio_received(self, callback: AudioCallback) -> None:
        """Register callback for captured audio from Signal calls."""
        self._callback = callback

    async def send_audio(self, audio: bytes) -> None:
        """Send audio to the active Signal call.

        Converts from internal format (24 kHz mono) to Signal format
        (48 kHz mono) and writes to the PulseAudio inject sink.
        """
        if not self._active or not self._in_call or not self._bridge:
            return
        signal_audio = convert_audio(audio, AudioFormat(), SIGNAL_AUDIO_FORMAT)
        await self._bridge.write_audio(signal_audio)

    async def answer_call(self) -> None:
        """Answer an incoming Signal call."""
        # Try signal-cli first if available
        if self._has_signal_cli and self._config.phone_number:
            logger.info("Answering Signal call via signal-cli")
            # signal-cli doesn't have a direct answer-call command,
            # so fall through to xdotool
            pass

        if not shutil.which("xdotool"):
            logger.warning("Cannot answer call: xdotool not installed")
            return

        wid = await self._find_signal_window()
        if not wid:
            logger.warning("Cannot answer call: Signal window not found")
            return

        logger.info("Answering Signal call (window=%s)", wid)
        await self._xdotool("windowactivate", "--sync", wid)
        await asyncio.sleep(0.3)
        await self._xdotool("key", "--window", wid, _ANSWER_KEY)
        self._in_call = True
        self._window_id = wid

    async def reject_call(self) -> None:
        """Reject an incoming Signal call."""
        if not shutil.which("xdotool"):
            logger.warning("Cannot reject call: xdotool not installed")
            return

        wid = await self._find_signal_window()
        if not wid:
            return

        logger.info("Rejecting Signal call (window=%s)", wid)
        await self._xdotool("windowactivate", "--sync", wid)
        await asyncio.sleep(0.3)
        await self._xdotool("key", "--window", wid, _REJECT_KEY)
        self._in_call = False

    async def hang_up(self) -> None:
        """End the current Signal call."""
        if not self._in_call:
            return

        if not shutil.which("xdotool"):
            logger.warning("Cannot hang up: xdotool not installed")
            return

        wid = self._window_id or await self._find_signal_window()
        if wid:
            logger.info("Hanging up Signal call (window=%s)", wid)
            await self._xdotool("windowactivate", "--sync", wid)
            await asyncio.sleep(0.3)
            await self._xdotool("key", "--window", wid, _REJECT_KEY)

        self._in_call = False
        self._window_id = None

    # -- signal-cli integration ----------------------------------------------

    async def _signalcli_monitor_loop(self) -> None:
        """Monitor signal-cli JSON output for incoming call events.

        Runs signal-cli in ``jsonRpc`` mode and watches for call-related
        messages on stdout.
        """
        if not self._config.signal_cli_path or not self._config.phone_number:
            return

        try:
            args = [
                self._config.signal_cli_path,
                "--output=json",
                "-a", self._config.phone_number,
            ]
            if self._config.config_dir:
                args.extend(["--config", self._config.config_dir])
            args.append("jsonRpc")

            self._signalcli_proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )

            if not self._signalcli_proc.stdout:
                return

            while self._active:
                line = await self._signalcli_proc.stdout.readline()
                if not line:
                    break
                try:
                    msg = json.loads(line.decode())
                    await self._handle_signalcli_event(msg)
                except (json.JSONDecodeError, KeyError):
                    continue

        except asyncio.CancelledError:
            pass
        except FileNotFoundError:
            logger.warning("signal-cli not found at: %s", self._config.signal_cli_path)
        except Exception:
            if self._active:
                logger.exception("signal-cli monitor error")

    async def _handle_signalcli_event(self, msg: dict[str, Any]) -> None:
        """Handle a parsed signal-cli JSON-RPC event."""
        method = msg.get("method", "")

        if method == "receive":
            params = msg.get("params", {})
            envelope = params.get("envelope", {})

            # Check for call offer message
            call_msg = envelope.get("callMessage")
            if call_msg and call_msg.get("offerMessage"):
                source = envelope.get("source", "unknown")
                logger.info("Incoming Signal call from %s (via signal-cli)", source)

                # Check allowlist
                if self._config.allowed_contacts:
                    if source not in self._config.allowed_contacts:
                        logger.info(
                            "Ignoring call from '%s' (not in allowed_contacts)",
                            source,
                        )
                        return

                if self._config.auto_answer:
                    await self.answer_call()

            # Check for call hangup
            if call_msg and call_msg.get("hangupMessage"):
                logger.info("Signal call ended (via signal-cli)")
                self._in_call = False
                self._window_id = None

    # -- Window-based call detection -----------------------------------------

    async def _call_monitor_loop(self) -> None:
        """Poll for incoming Signal calls by checking window state."""
        try:
            while self._active:
                if not self._in_call:
                    await self._check_for_incoming_call()
                await asyncio.sleep(1.0)
        except asyncio.CancelledError:
            pass

    async def _check_for_incoming_call(self) -> None:
        """Check if Signal Desktop has an incoming call."""
        wid = await self._find_signal_window()
        if not wid:
            return

        title = await self._get_window_title(wid)
        if not title:
            return

        title_lower = title.lower()
        is_call = any(pattern in title_lower for pattern in _CALL_TITLE_PATTERNS)

        if not is_call:
            is_call = await self._check_window_urgency(wid)

        if is_call:
            logger.info("Incoming Signal call detected (title='%s')", title)

            if self._config.allowed_contacts:
                contact = self._extract_contact_from_title(title)
                if contact and contact not in self._config.allowed_contacts:
                    logger.info(
                        "Ignoring call from '%s' (not in allowed_contacts)", contact
                    )
                    return

            if self._config.auto_answer:
                await self.answer_call()

    def _extract_contact_from_title(self, title: str) -> str | None:
        """Extract caller info from Signal Desktop window title."""
        for separator in (" - ", " — ", ": "):
            if separator in title:
                parts = title.split(separator)
                candidate = parts[0].strip()
                if candidate and not any(
                    p in candidate.lower() for p in _CALL_TITLE_PATTERNS
                ):
                    return candidate
        return None

    # -- Window helpers ------------------------------------------------------

    async def _find_signal_window(self) -> str | None:
        """Find the Signal Desktop window ID using xdotool."""
        if not shutil.which("xdotool"):
            return None
        try:
            proc = await asyncio.create_subprocess_exec(
                "xdotool", "search", "--name", self._config.process_name,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await proc.communicate()
            if proc.returncode == 0 and stdout.strip():
                return stdout.decode().strip().split("\n")[0]
        except FileNotFoundError:
            pass
        return None

    async def _get_window_title(self, window_id: str) -> str | None:
        """Get the title of a window by its X11 ID."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "xdotool", "getwindowname", window_id,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await proc.communicate()
            if proc.returncode == 0:
                return stdout.decode().strip()
        except FileNotFoundError:
            pass
        return None

    async def _check_window_urgency(self, window_id: str) -> bool:
        """Check if a window has the urgency/demands-attention hint."""
        if not shutil.which("xprop"):
            return False
        try:
            proc = await asyncio.create_subprocess_exec(
                "xprop", "-id", window_id, "_NET_WM_STATE",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await proc.communicate()
            if proc.returncode == 0:
                return b"DEMANDS_ATTENTION" in stdout
        except FileNotFoundError:
            pass
        return False

    async def _xdotool(self, *args: str) -> bool:
        """Run an xdotool command. Returns True on success."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "xdotool", *args,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.wait()
            return proc.returncode == 0
        except FileNotFoundError:
            return False
