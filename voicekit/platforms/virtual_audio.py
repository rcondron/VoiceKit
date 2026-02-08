"""Virtual audio device platform adapter.

Connects VoiceKit to virtual audio devices (e.g., VB-Cable on Windows,
BlackHole on macOS, PulseAudio virtual sinks on Linux). This is the
simplest adapter and the recommended starting point for testing.

Audio is captured from the virtual device's output and played back to
the virtual device's input, creating a bidirectional audio bridge that
any application can use.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import numpy as np

from voicekit.config import VirtualAudioPlatformConfig
from voicekit.core.audio import INTERNAL_SAMPLE_RATE, AudioBuffer
from voicekit.platforms.base import AudioCallback, PlatformAdapter

logger = logging.getLogger(__name__)


class VirtualAudioPlatform(PlatformAdapter):
    """Platform adapter for virtual audio devices.

    Uses the ``sounddevice`` library for cross-platform audio I/O.
    Captures audio from a configured input device and plays audio
    to a configured output device.
    """

    def __init__(self, config: VirtualAudioPlatformConfig) -> None:
        self._config = config
        self._callback: AudioCallback | None = None
        self._active = False
        self._input_stream: Any = None
        self._output_stream: Any = None
        self._output_buffer = AudioBuffer(chunk_size=config.chunk_size)
        self._output_queue: asyncio.Queue[bytes] = asyncio.Queue()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._playback_task: asyncio.Task[None] | None = None

    @property
    def name(self) -> str:
        return "virtual_audio"

    @property
    def is_active(self) -> bool:
        return self._active

    async def start(self) -> None:
        """Start capturing and playing audio via virtual audio devices."""
        try:
            import sounddevice as sd
        except ImportError:
            raise RuntimeError(
                "sounddevice is required for virtual audio. "
                "Install it with: pip install sounddevice"
            )

        self._loop = asyncio.get_running_loop()

        sample_rate = self._config.sample_rate or INTERNAL_SAMPLE_RATE
        channels = self._config.channels or 1
        block_size = self._config.chunk_size

        # Resolve device indices
        input_device = self._config.input_device or None
        output_device = self._config.output_device or None

        if input_device:
            logger.info("Virtual audio input device: %s", input_device)
        if output_device:
            logger.info("Virtual audio output device: %s", output_device)

        # Input stream — captures audio from virtual device
        self._input_stream = sd.InputStream(
            device=input_device,  # type: ignore[arg-type]
            samplerate=sample_rate,
            channels=channels,
            dtype="int16",
            blocksize=block_size,
            callback=self._input_callback,
        )

        # Output stream — plays audio to virtual device
        self._output_stream = sd.OutputStream(
            device=output_device,  # type: ignore[arg-type]
            samplerate=sample_rate,
            channels=channels,
            dtype="int16",
            blocksize=block_size,
            callback=self._output_callback,
        )

        self._input_stream.start()
        self._output_stream.start()
        self._active = True

        logger.info(
            "Virtual audio started (rate=%d, channels=%d, chunk=%d)",
            sample_rate,
            channels,
            block_size,
        )

    def _input_callback(
        self,
        indata: np.ndarray,
        frames: int,
        time_info: Any,
        status: Any,
    ) -> None:
        """sounddevice input callback — runs in audio thread."""
        if status:
            logger.warning("Input audio status: %s", status)

        if not self._callback or not self._loop:
            return

        # Convert numpy array to PCM bytes
        pcm_bytes = indata.flatten().astype(np.int16).tobytes()

        # Schedule the async callback on the event loop
        asyncio.run_coroutine_threadsafe(self._callback(pcm_bytes), self._loop)

    def _output_callback(
        self,
        outdata: np.ndarray,
        frames: int,
        time_info: Any,
        status: Any,
    ) -> None:
        """sounddevice output callback — runs in audio thread."""
        if status:
            logger.warning("Output audio status: %s", status)

        try:
            data = self._output_queue.get_nowait()
            samples = np.frombuffer(data, dtype=np.int16)

            # Ensure correct shape
            expected_samples = frames * (self._config.channels or 1)
            if len(samples) < expected_samples:
                padded = np.zeros(expected_samples, dtype=np.int16)
                padded[: len(samples)] = samples
                samples = padded
            elif len(samples) > expected_samples:
                samples = samples[:expected_samples]

            outdata[:] = samples.reshape(outdata.shape)

        except asyncio.QueueEmpty:
            outdata.fill(0)

    async def stop(self) -> None:
        """Stop audio streams and clean up."""
        self._active = False

        if self._playback_task and not self._playback_task.done():
            self._playback_task.cancel()
            try:
                await self._playback_task
            except asyncio.CancelledError:
                pass

        if self._input_stream:
            self._input_stream.stop()
            self._input_stream.close()
            self._input_stream = None

        if self._output_stream:
            self._output_stream.stop()
            self._output_stream.close()
            self._output_stream = None

        logger.info("Virtual audio stopped")

    def on_audio_received(self, callback: AudioCallback) -> None:
        """Register callback for captured audio."""
        self._callback = callback

    async def send_audio(self, audio: bytes) -> None:
        """Send audio to the virtual audio output device."""
        if not self._active:
            return

        # Buffer and enqueue complete chunks
        chunks = self._output_buffer.write(audio)
        for chunk in chunks:
            try:
                self._output_queue.put_nowait(chunk)
            except asyncio.QueueFull:
                # Drop oldest if queue is full to prevent unbounded growth
                try:
                    self._output_queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                self._output_queue.put_nowait(chunk)

    @staticmethod
    def list_devices() -> list[dict[str, Any]]:
        """List available audio devices.

        Returns:
            List of device info dictionaries.
        """
        try:
            import sounddevice as sd

            devices = sd.query_devices()
            result = []
            if isinstance(devices, list):
                for i, dev in enumerate(devices):
                    if isinstance(dev, dict):
                        result.append(
                            {
                                "index": i,
                                "name": dev.get("name", ""),
                                "max_input_channels": dev.get("max_input_channels", 0),
                                "max_output_channels": dev.get("max_output_channels", 0),
                                "default_samplerate": dev.get("default_samplerate", 0),
                            }
                        )
            return result
        except ImportError:
            return []
        except Exception as exc:
            logger.error("Failed to list audio devices: %s", exc)
            return []
