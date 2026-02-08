"""WhatsApp Desktop automation platform adapter.

Automates WhatsApp Desktop to answer voice calls and route audio.
Uses PulseAudio/PipeWire virtual devices for per-app audio capture
and playback, and desktop notification monitoring for incoming call
detection.

Supports:
- Detecting incoming WhatsApp voice/video calls via desktop notifications
- Auto-answering calls (via xdotool keyboard simulation)
- Bidirectional audio routing via PulseAudio virtual sinks
- Contact allowlisting
- Call rejection and hang-up

Audio format: PulseAudio captures PCM 16-bit, 48 kHz, mono.
The adapter converts to/from VoiceKit's internal format (24 kHz mono).

Requires: pip install voicekit[desktop]
System requirements:
  - WhatsApp Desktop (Linux: snap, Flatpak, or .deb)
  - PulseAudio or PipeWire (with pulseaudio-utils)
  - xdotool (for keyboard/window simulation)
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from typing import Any

from voicekit.config import WhatsAppPlatformConfig
from voicekit.core.audio import AudioFormat, convert_audio
from voicekit.platforms.base import AudioCallback, PlatformAdapter

logger = logging.getLogger(__name__)

# WhatsApp Desktop voice calls use 48 kHz mono (via PulseAudio)
WHATSAPP_AUDIO_FORMAT = AudioFormat(sample_rate=48000, channels=1, sample_width=2)

# 20ms frame at 48 kHz mono = 960 samples = 1920 bytes
WHATSAPP_FRAME_BYTES = 960 * 2

# Window title patterns indicating an incoming call
_CALL_TITLE_PATTERNS = ("incoming voice call", "incoming video call", "ringing")

# xdotool key to answer a WhatsApp call (Enter typically answers)
_ANSWER_KEY = "Return"
_REJECT_KEY = "Escape"


class WhatsAppPlatform(PlatformAdapter):
    """WhatsApp Desktop adapter using PulseAudio bridge and xdotool.

    Routes audio bidirectionally between WhatsApp Desktop's voice calls
    and VoiceKit's AI provider by:

    1. Creating PulseAudio virtual sinks (capture + inject).
    2. Moving WhatsApp's audio streams to the virtual sinks.
    3. Reading captured call audio and forwarding to the AI.
    4. Playing AI responses into WhatsApp's microphone input.

    Incoming calls are detected by polling window properties, and
    auto-answered via ``xdotool`` keyboard simulation if enabled.
    """

    def __init__(self, config: WhatsAppPlatformConfig) -> None:
        self._config = config
        self._callback: AudioCallback | None = None
        self._active = False
        self._bridge: Any = None  # PulseAudioBridge (lazy)
        self._call_monitor_task: asyncio.Task[None] | None = None
        self._in_call = False
        self._window_id: str | None = None

    @property
    def name(self) -> str:
        return "whatsapp"

    @property
    def is_active(self) -> bool:
        return self._active

    async def start(self) -> None:
        """Start the WhatsApp platform adapter.

        Sets up PulseAudio virtual sinks, starts audio capture/playback,
        and begins monitoring for incoming calls.
        """
        # Verify system dependencies
        self._check_dependencies()

        from voicekit.core.pulse_bridge import PulseAudioBridge

        self._bridge = PulseAudioBridge(
            app_process_name=self._config.process_name,
            sink_prefix=self._config.pulse_sink_name,
            sample_rate=WHATSAPP_AUDIO_FORMAT.sample_rate,
            channels=WHATSAPP_AUDIO_FORMAT.channels,
        )

        # Create virtual PulseAudio sinks
        try:
            await self._bridge.setup()
        except RuntimeError:
            logger.exception("Failed to set up PulseAudio bridge for WhatsApp")
            raise

        # Start audio capture/playback
        await self._bridge.start(self._on_captured_audio)

        # Start call detection polling
        self._call_monitor_task = asyncio.create_task(
            self._call_monitor_loop(), name="whatsapp_call_monitor"
        )

        self._active = True
        logger.info(
            "WhatsApp platform started (process=%s, auto_answer=%s, allowed=%s)",
            self._config.process_name,
            self._config.auto_answer,
            self._config.allowed_contacts or "all",
        )

    def _check_dependencies(self) -> None:
        """Verify required system tools are available."""
        missing = []
        for tool in ("pactl", "parec", "pacat"):
            if not shutil.which(tool):
                missing.append(tool)

        if missing:
            raise RuntimeError(
                f"Missing system tools for WhatsApp audio: {', '.join(missing)}. "
                "Install pulseaudio-utils (or pipewire-pulse)."
            )

        if not shutil.which("xdotool"):
            logger.warning(
                "xdotool not found — auto-answer and call control will be disabled. "
                "Install with: sudo apt install xdotool"
            )

    async def _on_captured_audio(self, audio: bytes) -> None:
        """Handle audio captured from WhatsApp via PulseAudio bridge.

        Converts from WhatsApp format (48 kHz mono) to internal format
        (24 kHz mono) and forwards to the registered callback.
        """
        if not self._callback or not self._in_call:
            return
        internal = convert_audio(audio, WHATSAPP_AUDIO_FORMAT, AudioFormat())
        await self._callback(internal)

    async def stop(self) -> None:
        """Stop the WhatsApp platform and clean up all resources."""
        self._active = False
        self._in_call = False

        # Stop call monitor
        if self._call_monitor_task and not self._call_monitor_task.done():
            self._call_monitor_task.cancel()
            try:
                await self._call_monitor_task
            except asyncio.CancelledError:
                pass
        self._call_monitor_task = None

        # Stop PulseAudio bridge
        if self._bridge:
            await self._bridge.stop()
            self._bridge = None

        self._window_id = None
        logger.info("WhatsApp platform stopped")

    def on_audio_received(self, callback: AudioCallback) -> None:
        """Register callback for captured audio from WhatsApp calls."""
        self._callback = callback

    async def send_audio(self, audio: bytes) -> None:
        """Send audio to the active WhatsApp call.

        Converts from internal format (24 kHz mono) to WhatsApp format
        (48 kHz mono) and writes to the PulseAudio inject sink.
        """
        if not self._active or not self._in_call or not self._bridge:
            return
        whatsapp_audio = convert_audio(audio, AudioFormat(), WHATSAPP_AUDIO_FORMAT)
        await self._bridge.write_audio(whatsapp_audio)

    async def answer_call(self) -> None:
        """Answer an incoming WhatsApp call via xdotool."""
        if not shutil.which("xdotool"):
            logger.warning("Cannot answer call: xdotool not installed")
            return

        wid = await self._find_whatsapp_window()
        if not wid:
            logger.warning("Cannot answer call: WhatsApp window not found")
            return

        logger.info("Answering WhatsApp call (window=%s)", wid)
        await self._xdotool("windowactivate", "--sync", wid)
        await asyncio.sleep(0.3)
        await self._xdotool("key", "--window", wid, _ANSWER_KEY)
        self._in_call = True
        self._window_id = wid

    async def reject_call(self) -> None:
        """Reject an incoming WhatsApp call via xdotool."""
        if not shutil.which("xdotool"):
            logger.warning("Cannot reject call: xdotool not installed")
            return

        wid = await self._find_whatsapp_window()
        if not wid:
            return

        logger.info("Rejecting WhatsApp call (window=%s)", wid)
        await self._xdotool("windowactivate", "--sync", wid)
        await asyncio.sleep(0.3)
        await self._xdotool("key", "--window", wid, _REJECT_KEY)
        self._in_call = False

    async def hang_up(self) -> None:
        """End the current WhatsApp call."""
        if not self._in_call:
            return

        if not shutil.which("xdotool"):
            logger.warning("Cannot hang up: xdotool not installed")
            return

        wid = self._window_id or await self._find_whatsapp_window()
        if wid:
            logger.info("Hanging up WhatsApp call (window=%s)", wid)
            await self._xdotool("windowactivate", "--sync", wid)
            await asyncio.sleep(0.3)
            # Escape typically ends/closes the call UI
            await self._xdotool("key", "--window", wid, _REJECT_KEY)

        self._in_call = False
        self._window_id = None

    # -- Call detection ------------------------------------------------------

    async def _call_monitor_loop(self) -> None:
        """Poll for incoming WhatsApp calls by checking window state."""
        try:
            while self._active:
                if not self._in_call:
                    await self._check_for_incoming_call()
                await asyncio.sleep(1.0)
        except asyncio.CancelledError:
            pass

    async def _check_for_incoming_call(self) -> None:
        """Check if WhatsApp has an incoming call via window title/urgency."""
        wid = await self._find_whatsapp_window()
        if not wid:
            return

        # Check window title for call indicators
        title = await self._get_window_title(wid)
        if not title:
            return

        title_lower = title.lower()
        is_call = any(pattern in title_lower for pattern in _CALL_TITLE_PATTERNS)

        if not is_call:
            # Also check the urgency hint (WM_HINTS)
            is_call = await self._check_window_urgency(wid)

        if is_call:
            logger.info("Incoming WhatsApp call detected (title='%s')", title)

            # Check contact allowlist
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
        """Try to extract the caller name/number from the window title.

        WhatsApp Desktop typically shows "ContactName - Incoming call" or
        similar patterns.
        """
        for separator in (" - ", " — ", ": "):
            if separator in title:
                parts = title.split(separator)
                # The contact name is usually the first part
                candidate = parts[0].strip()
                if candidate and not any(
                    p in candidate.lower() for p in _CALL_TITLE_PATTERNS
                ):
                    return candidate
        return None

    # -- Window helpers ------------------------------------------------------

    async def _find_whatsapp_window(self) -> str | None:
        """Find the WhatsApp Desktop window ID using xdotool."""
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
                # Return the first matching window
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
        """Check if a window has the urgency/demands-attention hint set."""
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
