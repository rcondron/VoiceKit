"""PulseAudio bridge for per-application audio routing.

Creates virtual PulseAudio sinks to capture audio output from desktop
applications and inject audio input, enabling bidirectional audio
routing without modifying the target application.

Architecture::

    Capture (App -> VoiceKit):
        App playback -> [capture null-sink] -> .monitor -> parec -> callback

    Inject (VoiceKit -> App):
        pacat -> [inject null-sink] -> .monitor (virtual source) -> App mic

System requirements:
    - PulseAudio or PipeWire (with PulseAudio compatibility layer)
    - pactl, parec, pacat CLI tools
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from voicekit.core.audio import AudioFormat
from voicekit.platforms.base import AudioCallback

logger = logging.getLogger(__name__)

# Desktop apps typically use 48 kHz mono for voice calls
DESKTOP_AUDIO_FORMAT = AudioFormat(sample_rate=48000, channels=1, sample_width=2)

# 20ms frame at 48 kHz mono = 960 samples = 1920 bytes
DESKTOP_FRAME_BYTES = 960 * 2


async def _run_pactl(*args: str) -> tuple[str, str, int]:
    """Run a pactl command and return (stdout, stderr, returncode)."""
    proc = await asyncio.create_subprocess_exec(
        "pactl", *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    return stdout.decode().strip(), stderr.decode().strip(), proc.returncode or 0


class PulseAudioBridge:
    """Per-application audio routing via PulseAudio virtual devices.

    Creates two null-sinks:

    * **capture sink** — the target app's playback streams are moved here
      so ``parec`` can read from its ``.monitor`` source.
    * **inject sink** — ``pacat`` writes AI audio here; the app's
      recording stream is moved to the sink's ``.monitor`` source so it
      reads our audio instead of the real microphone.

    Args:
        app_process_name: Process name used to find the app's PulseAudio
            streams (matched against ``application.process.binary``).
        sink_prefix: Prefix for the virtual sink names.  Actual sinks
            will be ``<prefix>_capture`` and ``<prefix>_inject``.
        sample_rate: PCM sample rate for the virtual devices.
        channels: Number of audio channels (1=mono, 2=stereo).
    """

    def __init__(
        self,
        app_process_name: str,
        sink_prefix: str,
        sample_rate: int = 48000,
        channels: int = 1,
    ) -> None:
        self._app_name = app_process_name
        self._sink_prefix = sink_prefix
        self._sample_rate = sample_rate
        self._channels = channels

        self._capture_module_id: int | None = None
        self._inject_module_id: int | None = None
        self._parec_proc: asyncio.subprocess.Process | None = None
        self._pacat_proc: asyncio.subprocess.Process | None = None
        self._read_task: asyncio.Task[None] | None = None
        self._route_task: asyncio.Task[None] | None = None
        self._audio_callback: AudioCallback | None = None
        self._active = False

    # -- Public properties ---------------------------------------------------

    @property
    def capture_sink(self) -> str:
        """Name of the null-sink that captures the app's playback audio."""
        return f"{self._sink_prefix}_capture"

    @property
    def inject_sink(self) -> str:
        """Name of the null-sink that injects audio into the app's mic."""
        return f"{self._sink_prefix}_inject"

    @property
    def audio_format(self) -> AudioFormat:
        return AudioFormat(
            sample_rate=self._sample_rate,
            channels=self._channels,
            sample_width=2,
        )

    # -- Lifecycle -----------------------------------------------------------

    async def setup(self) -> None:
        """Create virtual PulseAudio sinks.

        Raises:
            RuntimeError: If ``pactl`` is not available or module loading fails.
        """
        # Verify pactl is available
        try:
            _, _, rc = await _run_pactl("--version")
        except FileNotFoundError:
            raise RuntimeError(
                "pactl not found. Install PulseAudio or PipeWire "
                "(with pulseaudio-utils) for desktop app audio routing."
            )
        if rc != 0:
            raise RuntimeError("pactl is not functional — check PulseAudio/PipeWire status.")

        # Create capture null-sink
        self._capture_module_id = await self._load_module(
            "module-null-sink",
            sink_name=self.capture_sink,
            rate=str(self._sample_rate),
            channels=str(self._channels),
        )
        logger.debug(
            "Created capture sink: %s (module %s)", self.capture_sink, self._capture_module_id
        )

        # Create inject null-sink
        self._inject_module_id = await self._load_module(
            "module-null-sink",
            sink_name=self.inject_sink,
            rate=str(self._sample_rate),
            channels=str(self._channels),
        )
        logger.debug(
            "Created inject sink: %s (module %s)", self.inject_sink, self._inject_module_id
        )

    async def start(self, callback: AudioCallback) -> None:
        """Start audio capture and playback subprocesses.

        Args:
            callback: Async function called with captured PCM audio
                bytes (in ``audio_format``).
        """
        self._audio_callback = callback
        self._active = True

        # Start parec — reads captured audio from the capture sink's monitor
        self._parec_proc = await asyncio.create_subprocess_exec(
            "parec",
            f"--device={self.capture_sink}.monitor",
            "--format=s16le",
            f"--rate={self._sample_rate}",
            f"--channels={self._channels}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )

        # Start pacat — writes AI audio to the inject sink
        self._pacat_proc = await asyncio.create_subprocess_exec(
            "pacat",
            "--playback",
            f"--device={self.inject_sink}",
            "--format=s16le",
            f"--rate={self._sample_rate}",
            f"--channels={self._channels}",
            stdin=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )

        # Background task: read captured audio and invoke callback
        self._read_task = asyncio.create_task(
            self._capture_loop(), name=f"{self._sink_prefix}_capture"
        )

        # Background task: periodically route new app streams to our sinks
        self._route_task = asyncio.create_task(
            self._stream_routing_loop(), name=f"{self._sink_prefix}_route"
        )

        logger.info(
            "PulseAudio bridge started for '%s' (capture=%s, inject=%s)",
            self._app_name, self.capture_sink, self.inject_sink,
        )

    async def stop(self) -> None:
        """Stop subprocesses and tear down virtual sinks."""
        self._active = False

        # Cancel background tasks
        for task in (self._read_task, self._route_task):
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        self._read_task = None
        self._route_task = None

        # Terminate subprocesses
        for proc in (self._parec_proc, self._pacat_proc):
            if proc and proc.returncode is None:
                proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=3.0)
                except TimeoutError:
                    proc.kill()
                    await proc.wait()
        self._parec_proc = None
        self._pacat_proc = None

        # Unload PulseAudio modules (removes virtual sinks)
        for mod_id in (self._capture_module_id, self._inject_module_id):
            if mod_id is not None:
                await self._unload_module(mod_id)
        self._capture_module_id = None
        self._inject_module_id = None

        logger.info("PulseAudio bridge stopped for '%s'", self._app_name)

    # -- Audio I/O -----------------------------------------------------------

    async def write_audio(self, audio: bytes) -> None:
        """Write PCM audio to the inject sink (played to the app's mic).

        Args:
            audio: Raw PCM bytes in ``audio_format``.
        """
        if not self._pacat_proc or not self._pacat_proc.stdin:
            return
        try:
            self._pacat_proc.stdin.write(audio)
            await self._pacat_proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError, OSError):
            logger.debug("PulseAudio inject pipe broken for '%s'", self._app_name)

    async def _capture_loop(self) -> None:
        """Read audio from parec stdout and forward to callback."""
        if not self._parec_proc or not self._parec_proc.stdout:
            return

        # 20ms frame size
        frame_bytes = (self._sample_rate * self._channels * 2) // 50

        try:
            while self._active:
                data = await self._parec_proc.stdout.read(frame_bytes)
                if not data:
                    break
                if self._audio_callback:
                    await self._audio_callback(data)
        except asyncio.CancelledError:
            pass
        except Exception:
            if self._active:
                logger.exception("Error in PulseAudio capture loop for '%s'", self._app_name)

    # -- Stream routing ------------------------------------------------------

    async def route_app_streams(self) -> None:
        """Move the target application's audio streams to our virtual sinks.

        Finds PulseAudio sink-inputs (playback) and source-outputs (recording)
        belonging to the target app and moves them to the capture/inject sinks.
        """
        await self._route_sink_inputs()
        await self._route_source_outputs()

    async def _stream_routing_loop(self) -> None:
        """Periodically re-route app streams (apps may create new ones)."""
        try:
            while self._active:
                try:
                    await self.route_app_streams()
                except Exception:
                    logger.debug("Stream routing error", exc_info=True)
                await asyncio.sleep(2.0)
        except asyncio.CancelledError:
            pass

    async def _route_sink_inputs(self) -> None:
        """Move the app's playback streams to the capture sink."""
        streams = await self._list_streams("sink-inputs")
        for stream in streams:
            props = stream.get("properties", {})
            binary = props.get("application.process.binary", "")
            if self._matches_app(binary) and stream.get("sink") != self.capture_sink:
                idx = stream.get("index")
                if idx is not None:
                    _, err, rc = await _run_pactl(
                        "move-sink-input", str(idx), self.capture_sink
                    )
                    if rc == 0:
                        logger.debug("Moved sink-input %s to %s", idx, self.capture_sink)
                    else:
                        logger.debug("Failed to move sink-input %s: %s", idx, err)

    async def _route_source_outputs(self) -> None:
        """Move the app's recording streams to the inject sink's monitor."""
        streams = await self._list_streams("source-outputs")
        monitor_source = f"{self.inject_sink}.monitor"
        for stream in streams:
            props = stream.get("properties", {})
            binary = props.get("application.process.binary", "")
            if self._matches_app(binary) and stream.get("source") != monitor_source:
                idx = stream.get("index")
                if idx is not None:
                    _, err, rc = await _run_pactl(
                        "move-source-output", str(idx), monitor_source
                    )
                    if rc == 0:
                        logger.debug("Moved source-output %s to %s", idx, monitor_source)
                    else:
                        logger.debug("Failed to move source-output %s: %s", idx, err)

    def _matches_app(self, binary_name: str) -> bool:
        """Check if a PulseAudio stream's binary name matches our target app."""
        return self._app_name.lower() in binary_name.lower()

    # -- PulseAudio helpers --------------------------------------------------

    async def _list_streams(self, stream_type: str) -> list[dict[str, Any]]:
        """List PulseAudio streams as parsed JSON.

        Args:
            stream_type: Either ``"sink-inputs"`` or ``"source-outputs"``.

        Returns:
            List of stream info dicts. Empty list on error.
        """
        try:
            stdout, _, rc = await _run_pactl("-f", "json", "list", stream_type)
            if rc != 0 or not stdout:
                return []
            return json.loads(stdout)
        except (json.JSONDecodeError, FileNotFoundError):
            return []

    async def _load_module(self, module: str, **kwargs: str) -> int:
        """Load a PulseAudio module with the given arguments.

        Returns:
            The loaded module ID.

        Raises:
            RuntimeError: If the module fails to load.
        """
        args = [f"{k}={v}" for k, v in kwargs.items()]
        stdout, stderr, rc = await _run_pactl("load-module", module, *args)
        if rc != 0:
            raise RuntimeError(
                f"Failed to load PulseAudio module {module}: {stderr}"
            )
        return int(stdout)

    async def _unload_module(self, module_id: int) -> None:
        """Unload a PulseAudio module by ID."""
        _, stderr, rc = await _run_pactl("unload-module", str(module_id))
        if rc != 0:
            logger.debug("Failed to unload module %s: %s", module_id, stderr)


async def find_process_pulse_streams(
    process_name: str,
    stream_type: str = "sink-inputs",
) -> list[int]:
    """Find PulseAudio stream indices for a given process name.

    Args:
        process_name: Substring matched against ``application.process.binary``.
        stream_type: ``"sink-inputs"`` or ``"source-outputs"``.

    Returns:
        List of stream index integers.
    """
    try:
        stdout, _, rc = await _run_pactl("-f", "json", "list", stream_type)
        if rc != 0 or not stdout:
            return []
        streams = json.loads(stdout)
    except (json.JSONDecodeError, FileNotFoundError):
        return []

    indices: list[int] = []
    for stream in streams:
        props = stream.get("properties", {})
        binary = props.get("application.process.binary", "")
        if process_name.lower() in binary.lower():
            idx = stream.get("index")
            if idx is not None:
                indices.append(int(idx))
    return indices
