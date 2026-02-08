"""Tests for voicebridge.core.audio module."""

import numpy as np
import pytest

from voicebridge.core.audio import (
    INTERNAL_FORMAT,
    AudioBuffer,
    AudioFormat,
    convert_audio,
    mono_to_stereo,
    numpy_to_pcm,
    pcm_to_numpy,
    resample,
    stereo_to_mono,
)


class TestPcmConversion:
    def test_pcm_to_numpy_roundtrip(self):
        original = np.array([0, 100, -100, 32767, -32768], dtype=np.int16)
        pcm = numpy_to_pcm(original)
        result = pcm_to_numpy(pcm)
        np.testing.assert_array_equal(result, original)

    def test_pcm_to_numpy_empty(self):
        result = pcm_to_numpy(b"")
        assert len(result) == 0

    def test_pcm_to_numpy_rejects_non_16bit(self):
        fmt = AudioFormat(sample_width=4)
        with pytest.raises(ValueError, match="16-bit"):
            pcm_to_numpy(b"\x00" * 8, fmt)


class TestResample:
    def test_same_rate_noop(self):
        samples = np.array([1, 2, 3, 4, 5], dtype=np.int16)
        result = resample(samples, 24000, 24000)
        np.testing.assert_array_equal(result, samples)

    def test_upsample_doubles_length(self):
        samples = np.array([0, 1000, 0, -1000], dtype=np.int16)
        result = resample(samples, 24000, 48000)
        assert len(result) == 8

    def test_downsample_halves_length(self):
        samples = np.arange(100, dtype=np.int16)
        result = resample(samples, 48000, 24000)
        assert len(result) == 50

    def test_resample_preserves_dtype(self):
        samples = np.array([100, 200, 300], dtype=np.int16)
        result = resample(samples, 24000, 16000)
        assert result.dtype == np.int16


class TestChannelConversion:
    def test_stereo_to_mono(self):
        # L=100, R=200 -> mono=150
        stereo = np.array([100, 200, 300, 400], dtype=np.int16)
        mono = stereo_to_mono(stereo)
        np.testing.assert_array_equal(mono, np.array([150, 350], dtype=np.int16))

    def test_stereo_to_mono_rejects_odd_length(self):
        with pytest.raises(ValueError, match="even"):
            stereo_to_mono(np.array([1, 2, 3], dtype=np.int16))

    def test_mono_to_stereo(self):
        mono = np.array([100, 200], dtype=np.int16)
        stereo = mono_to_stereo(mono)
        np.testing.assert_array_equal(
            stereo, np.array([100, 100, 200, 200], dtype=np.int16)
        )

    def test_roundtrip_mono_stereo_mono(self):
        original = np.array([500, 1000, 1500], dtype=np.int16)
        stereo = mono_to_stereo(original)
        result = stereo_to_mono(stereo)
        np.testing.assert_array_equal(result, original)


class TestConvertAudio:
    def test_same_format_passthrough(self):
        data = b"\x00" * 100
        result = convert_audio(data, INTERNAL_FORMAT, INTERNAL_FORMAT)
        assert result == data

    def test_stereo_to_mono_conversion(self):
        stereo_fmt = AudioFormat(sample_rate=24000, channels=2)
        mono_fmt = AudioFormat(sample_rate=24000, channels=1)
        stereo_data = np.array([100, 200, 300, 400], dtype=np.int16).tobytes()
        result = convert_audio(stereo_data, stereo_fmt, mono_fmt)
        result_samples = np.frombuffer(result, dtype=np.int16)
        assert len(result_samples) == 2

    def test_rate_conversion(self):
        fmt_48k = AudioFormat(sample_rate=48000, channels=1)
        fmt_24k = AudioFormat(sample_rate=24000, channels=1)
        samples = np.zeros(480, dtype=np.int16)
        data = samples.tobytes()
        result = convert_audio(data, fmt_48k, fmt_24k)
        result_samples = np.frombuffer(result, dtype=np.int16)
        assert len(result_samples) == 240


class TestAudioBuffer:
    def test_write_returns_chunks_when_full(self):
        buf = AudioBuffer(chunk_size=4)  # 4 samples = 8 bytes
        chunks = buf.write(b"\x00" * 8)
        assert len(chunks) == 1
        assert len(chunks[0]) == 8

    def test_write_accumulates_small_writes(self):
        buf = AudioBuffer(chunk_size=4)
        assert buf.write(b"\x00" * 4) == []  # Only half a chunk
        chunks = buf.write(b"\x00" * 4)
        assert len(chunks) == 1

    def test_write_returns_multiple_chunks(self):
        buf = AudioBuffer(chunk_size=4)
        chunks = buf.write(b"\x00" * 24)  # 3 chunks worth
        assert len(chunks) == 3

    def test_flush_returns_padded_remainder(self):
        buf = AudioBuffer(chunk_size=4)
        buf.write(b"\x01\x00" * 2)  # 4 bytes = 2 samples (need 4)
        result = buf.flush()
        assert result is not None
        assert len(result) == 8  # Padded to chunk size
        assert result[:4] == b"\x01\x00\x01\x00"
        assert result[4:] == b"\x00\x00\x00\x00"

    def test_flush_empty_returns_none(self):
        buf = AudioBuffer(chunk_size=4)
        assert buf.flush() is None

    def test_clear(self):
        buf = AudioBuffer(chunk_size=4)
        buf.write(b"\x00" * 4)
        assert len(buf) == 4
        buf.clear()
        assert len(buf) == 0

    def test_len(self):
        buf = AudioBuffer(chunk_size=10)
        assert len(buf) == 0
        buf.write(b"\x00" * 5)
        assert len(buf) == 5


class TestAudioFormat:
    def test_bytes_per_second(self):
        fmt = AudioFormat(sample_rate=24000, channels=1, sample_width=2)
        assert fmt.bytes_per_second == 48000

    def test_bytes_per_second_stereo(self):
        fmt = AudioFormat(sample_rate=48000, channels=2, sample_width=2)
        assert fmt.bytes_per_second == 192000

    def test_frame_size(self):
        fmt = AudioFormat(channels=2, sample_width=2)
        assert fmt.frame_size == 4
