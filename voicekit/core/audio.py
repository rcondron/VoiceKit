"""Audio buffer management and format conversion utilities.

VoiceKit standardizes on PCM 16-bit signed, 24 kHz, mono as its internal
audio format. Platform adapters and AI providers convert to/from this format
at their boundaries.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from enum import Enum

import numpy as np
from numpy.typing import NDArray


class SampleRate(int, Enum):
    """Common sample rates encountered across platforms."""

    RATE_8K = 8000
    RATE_16K = 16000
    RATE_24K = 24000
    RATE_44K = 44100
    RATE_48K = 48000


# Internal format constants
INTERNAL_SAMPLE_RATE = SampleRate.RATE_24K
INTERNAL_CHANNELS = 1
INTERNAL_SAMPLE_WIDTH = 2  # 16-bit = 2 bytes
INTERNAL_DTYPE = np.int16


@dataclass(frozen=True)
class AudioFormat:
    """Describes a PCM audio format."""

    sample_rate: int = INTERNAL_SAMPLE_RATE
    channels: int = INTERNAL_CHANNELS
    sample_width: int = INTERNAL_SAMPLE_WIDTH

    @property
    def bytes_per_second(self) -> int:
        return self.sample_rate * self.channels * self.sample_width

    @property
    def frame_size(self) -> int:
        """Bytes per sample frame (all channels)."""
        return self.channels * self.sample_width


INTERNAL_FORMAT = AudioFormat()


def pcm_to_numpy(data: bytes, fmt: AudioFormat | None = None) -> NDArray[np.int16]:
    """Convert raw PCM bytes to a numpy int16 array.

    Args:
        data: Raw PCM bytes (16-bit signed little-endian).
        fmt: The audio format. Defaults to internal format.

    Returns:
        Numpy array of int16 samples. If stereo, interleaved.
    """
    fmt = fmt or INTERNAL_FORMAT
    if fmt.sample_width != 2:
        raise ValueError(f"Only 16-bit PCM supported, got {fmt.sample_width * 8}-bit")
    return np.frombuffer(data, dtype=np.int16).copy()


def numpy_to_pcm(samples: NDArray[np.int16]) -> bytes:
    """Convert a numpy int16 array back to raw PCM bytes."""
    return samples.astype(np.int16).tobytes()


def resample(
    samples: NDArray[np.int16],
    from_rate: int,
    to_rate: int,
) -> NDArray[np.int16]:
    """Resample audio using linear interpolation.

    This is a simple resampler suitable for voice. For production use with
    high-fidelity requirements, consider libsamplerate via samplerate package.

    Args:
        samples: Input samples as int16.
        from_rate: Source sample rate in Hz.
        to_rate: Target sample rate in Hz.

    Returns:
        Resampled int16 array.
    """
    if from_rate == to_rate:
        return samples

    ratio = to_rate / from_rate
    output_length = int(len(samples) * ratio)

    # Work in float for interpolation precision
    float_samples = samples.astype(np.float64)
    indices = np.linspace(0, len(float_samples) - 1, output_length)
    resampled = np.interp(indices, np.arange(len(float_samples)), float_samples)

    return np.clip(resampled, -32768, 32767).astype(np.int16)


def stereo_to_mono(samples: NDArray[np.int16]) -> NDArray[np.int16]:
    """Mix stereo interleaved samples down to mono by averaging channels."""
    if len(samples) % 2 != 0:
        raise ValueError("Stereo buffer must have even number of samples")
    left = samples[0::2].astype(np.int32)
    right = samples[1::2].astype(np.int32)
    mono = ((left + right) // 2).astype(np.int16)
    return mono


def mono_to_stereo(samples: NDArray[np.int16]) -> NDArray[np.int16]:
    """Duplicate mono samples to interleaved stereo."""
    stereo = np.empty(len(samples) * 2, dtype=np.int16)
    stereo[0::2] = samples
    stereo[1::2] = samples
    return stereo


def convert_audio(
    data: bytes,
    from_format: AudioFormat,
    to_format: AudioFormat,
) -> bytes:
    """Convert audio data between formats.

    Handles sample rate conversion and channel count changes.

    Args:
        data: Raw PCM bytes in the source format.
        from_format: Source audio format.
        to_format: Target audio format.

    Returns:
        Raw PCM bytes in the target format.
    """
    if from_format == to_format:
        return data

    samples = pcm_to_numpy(data, from_format)

    # Channel conversion first (before resampling, as it changes sample count)
    if from_format.channels == 2 and to_format.channels == 1:
        samples = stereo_to_mono(samples)
    elif from_format.channels == 1 and to_format.channels == 2:
        samples = mono_to_stereo(samples)

    # Sample rate conversion
    if from_format.sample_rate != to_format.sample_rate:
        samples = resample(samples, from_format.sample_rate, to_format.sample_rate)

    return numpy_to_pcm(samples)


class AudioBuffer:
    """Thread-safe ring buffer for audio chunks.

    Accumulates incoming PCM data and yields it in fixed-size chunks
    suitable for the downstream consumer.
    """

    def __init__(self, chunk_size: int = 4800) -> None:
        """Initialize the audio buffer.

        Args:
            chunk_size: Number of *samples* per output chunk.
                        Default 4800 = 200ms at 24kHz mono.
        """
        self._chunk_size = chunk_size
        self._buffer = bytearray()

    @property
    def chunk_bytes(self) -> int:
        """Output chunk size in bytes."""
        return self._chunk_size * INTERNAL_SAMPLE_WIDTH

    def write(self, data: bytes) -> list[bytes]:
        """Append data and return any complete chunks.

        Args:
            data: Raw PCM bytes in internal format.

        Returns:
            List of complete chunks (may be empty).
        """
        self._buffer.extend(data)

        chunks: list[bytes] = []
        while len(self._buffer) >= self.chunk_bytes:
            chunk = bytes(self._buffer[: self.chunk_bytes])
            del self._buffer[: self.chunk_bytes]
            chunks.append(chunk)

        return chunks

    def flush(self) -> bytes | None:
        """Return any remaining data in the buffer, zero-padded to chunk size.

        Returns:
            Padded chunk or None if the buffer is empty.
        """
        if not self._buffer:
            return None

        data = bytes(self._buffer)
        self._buffer.clear()

        # Zero-pad to full chunk
        if len(data) < self.chunk_bytes:
            data += b"\x00" * (self.chunk_bytes - len(data))

        return data

    def clear(self) -> None:
        """Discard all buffered data."""
        self._buffer.clear()

    def __len__(self) -> int:
        """Number of bytes currently buffered."""
        return len(self._buffer)
