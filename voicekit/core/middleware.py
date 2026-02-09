"""Audio middleware pipeline for VoiceKit.

Provides a composable pipeline of audio processors that can be inserted
between the platform and provider. Each middleware receives audio,
processes it, and passes it to the next stage.

Built-in middleware:
  - EchoCanceller: basic AEC using cross-correlation
  - NoiseGate: silence/noise suppression
  - AudioRecorder: save audio to WAV files
  - TranscriptLogger: capture and export transcripts
  - RateLimiter: limit audio throughput per connection

Usage:
  pipeline = AudioPipeline()
  pipeline.add(NoiseGate(threshold_db=-40))
  pipeline.add(EchoCanceller(tail_ms=150))
  processed = await pipeline.process(audio_chunk)
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import wave
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

import numpy as np

from voicekit.core.audio import INTERNAL_FORMAT, AudioFormat, pcm_to_numpy, numpy_to_pcm

logger = logging.getLogger(__name__)


class AudioMiddleware(ABC):
    """Base class for audio processing middleware."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Name of this middleware."""
        ...

    @abstractmethod
    async def process_inbound(self, audio: bytes) -> bytes:
        """Process audio from platform → provider (user speech).

        Args:
            audio: PCM 16-bit, 24 kHz, mono audio data.

        Returns:
            Processed audio bytes (same format).
        """
        ...

    @abstractmethod
    async def process_outbound(self, audio: bytes) -> bytes:
        """Process audio from provider → platform (AI speech).

        Args:
            audio: PCM 16-bit, 24 kHz, mono audio data.

        Returns:
            Processed audio bytes (same format).
        """
        ...

    async def start(self) -> None:
        """Initialize the middleware (called when pipeline starts)."""

    async def stop(self) -> None:
        """Clean up resources (called when pipeline stops)."""


class AudioPipeline:
    """Composable pipeline of AudioMiddleware processors."""

    def __init__(self) -> None:
        self._middleware: list[AudioMiddleware] = []

    def add(self, middleware: AudioMiddleware) -> AudioPipeline:
        """Add a middleware to the end of the pipeline."""
        self._middleware.append(middleware)
        return self

    async def start(self) -> None:
        for mw in self._middleware:
            await mw.start()

    async def stop(self) -> None:
        for mw in reversed(self._middleware):
            await mw.stop()

    async def process_inbound(self, audio: bytes) -> bytes:
        """Run audio through all middleware (platform → provider direction)."""
        for mw in self._middleware:
            audio = await mw.process_inbound(audio)
        return audio

    async def process_outbound(self, audio: bytes) -> bytes:
        """Run audio through all middleware (provider → platform direction)."""
        for mw in reversed(self._middleware):
            audio = await mw.process_outbound(audio)
        return audio


# ---------------------------------------------------------------------------
# Built-in middleware implementations
# ---------------------------------------------------------------------------


class EchoCanceller(AudioMiddleware):
    """Basic echo cancellation using normalized cross-correlation.

    Detects when the AI's output audio is being picked up by the
    platform's microphone and subtracts it. Uses a simple delay-and-
    subtract approach with adaptive gain.

    This is a lightweight AEC suitable for virtual audio setups.
    For hardware mic+speaker scenarios, consider integrating a
    proper AEC library (speexdsp, WebRTC AEC3).

    Args:
        tail_ms: Echo tail length in milliseconds (how far back to
                 search for the echo). Default 150ms.
        sample_rate: Audio sample rate. Default 24000 (internal format).
    """

    def __init__(self, tail_ms: int = 150, sample_rate: int = 24000) -> None:
        self._tail_samples = int(sample_rate * tail_ms / 1000)
        self._reference_buffer = np.zeros(self._tail_samples * 2, dtype=np.float64)
        self._adaptation_rate = 0.01

    @property
    def name(self) -> str:
        return "echo_canceller"

    async def process_inbound(self, audio: bytes) -> bytes:
        """Remove echo from inbound (user) audio."""
        samples = pcm_to_numpy(audio).astype(np.float64)

        if np.max(np.abs(self._reference_buffer)) < 100:
            # No reference signal — nothing to cancel
            return audio

        # Find best correlation offset
        ref_len = min(len(samples), self._tail_samples)
        ref_segment = self._reference_buffer[-ref_len:]

        if len(ref_segment) < 64 or len(samples) < 64:
            return audio

        # Normalized cross-correlation to find echo delay
        correlation = np.correlate(samples[:ref_len], ref_segment, mode="full")
        if np.max(np.abs(correlation)) > 0:
            delay = int(np.argmax(np.abs(correlation)) - ref_len + 1)
        else:
            return audio

        # Compute echo estimate and subtract
        corr_peak = np.max(np.abs(correlation))
        ref_power = np.sqrt(np.sum(ref_segment**2))
        sig_power = np.sqrt(np.sum(samples[:ref_len] ** 2))

        if ref_power > 0 and sig_power > 0:
            echo_ratio = corr_peak / (ref_power * sig_power + 1e-10)
        else:
            echo_ratio = 0.0

        # Only cancel if strong echo detected (correlation > 0.3)
        if echo_ratio > 0.3 and abs(delay) < self._tail_samples:
            gain = echo_ratio * self._adaptation_rate * 10
            gain = min(gain, 0.95)  # Never fully subtract

            # Shift reference to align with detected delay
            if delay >= 0 and delay < len(self._reference_buffer):
                echo_estimate = self._reference_buffer[-(len(samples) + delay):-(delay or None)]
                if len(echo_estimate) == len(samples):
                    samples = samples - gain * echo_estimate

        result = np.clip(samples, -32768, 32767).astype(np.int16)
        return numpy_to_pcm(result)

    async def process_outbound(self, audio: bytes) -> bytes:
        """Store outbound (AI) audio as echo reference."""
        samples = pcm_to_numpy(audio).astype(np.float64)

        # Append to reference buffer (sliding window)
        self._reference_buffer = np.concatenate([
            self._reference_buffer[len(samples):],
            samples,
        ])

        return audio


class NoiseGate(AudioMiddleware):
    """Simple noise gate that silences audio below a threshold.

    Args:
        threshold_db: Gate threshold in dB below full scale.
                      Default -40 dB (very quiet signals are gated).
        attack_ms: Time to open the gate (ms). Default 5.
        release_ms: Time to close the gate (ms). Default 50.
    """

    def __init__(
        self, threshold_db: float = -40, attack_ms: int = 5, release_ms: int = 50
    ) -> None:
        self._threshold = 10 ** (threshold_db / 20) * 32768
        self._attack_samples = int(24000 * attack_ms / 1000)
        self._release_samples = int(24000 * release_ms / 1000)
        self._gate_open = False
        self._hold_counter = 0

    @property
    def name(self) -> str:
        return "noise_gate"

    async def process_inbound(self, audio: bytes) -> bytes:
        """Apply noise gate to inbound (user) audio."""
        samples = pcm_to_numpy(audio)
        rms = float(np.sqrt(np.mean(samples.astype(np.float64) ** 2)))

        if rms > self._threshold:
            self._gate_open = True
            self._hold_counter = self._release_samples
        elif self._hold_counter > 0:
            self._hold_counter -= len(samples)
        else:
            self._gate_open = False

        if not self._gate_open:
            return b"\x00" * len(audio)

        return audio

    async def process_outbound(self, audio: bytes) -> bytes:
        return audio  # Don't gate AI output


class AudioRecorder(AudioMiddleware):
    """Records audio streams to WAV files for debugging or logging.

    Creates separate files for inbound (user) and outbound (AI) audio.

    Args:
        output_dir: Directory to save recordings.
        prefix: Filename prefix.
        record_inbound: Whether to record user audio.
        record_outbound: Whether to record AI audio.
    """

    def __init__(
        self,
        output_dir: str = "recordings",
        prefix: str = "voicekit",
        record_inbound: bool = True,
        record_outbound: bool = True,
    ) -> None:
        self._output_dir = Path(output_dir)
        self._prefix = prefix
        self._record_inbound = record_inbound
        self._record_outbound = record_outbound
        self._inbound_wav: wave.Wave_write | None = None
        self._outbound_wav: wave.Wave_write | None = None

    @property
    def name(self) -> str:
        return "audio_recorder"

    async def start(self) -> None:
        self._output_dir.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y%m%d_%H%M%S")

        if self._record_inbound:
            path = self._output_dir / f"{self._prefix}_inbound_{ts}.wav"
            self._inbound_wav = wave.open(str(path), "wb")
            self._inbound_wav.setnchannels(1)
            self._inbound_wav.setsampwidth(2)
            self._inbound_wav.setframerate(24000)
            logger.info("Recording inbound audio to %s", path)

        if self._record_outbound:
            path = self._output_dir / f"{self._prefix}_outbound_{ts}.wav"
            self._outbound_wav = wave.open(str(path), "wb")
            self._outbound_wav.setnchannels(1)
            self._outbound_wav.setsampwidth(2)
            self._outbound_wav.setframerate(24000)
            logger.info("Recording outbound audio to %s", path)

    async def stop(self) -> None:
        for wav in (self._inbound_wav, self._outbound_wav):
            if wav:
                wav.close()

    async def process_inbound(self, audio: bytes) -> bytes:
        if self._inbound_wav:
            self._inbound_wav.writeframes(audio)
        return audio

    async def process_outbound(self, audio: bytes) -> bytes:
        if self._outbound_wav:
            self._outbound_wav.writeframes(audio)
        return audio


class TranscriptLogger(AudioMiddleware):
    """Captures transcript events and exports them to JSON/text files.

    Listens for transcript data flowing through the pipeline and
    accumulates it. Transcripts are flushed to disk periodically
    and on stop.

    Args:
        output_dir: Directory for transcript files.
        format: Output format ("json" or "text").
    """

    def __init__(self, output_dir: str = "transcripts", fmt: str = "json") -> None:
        self._output_dir = Path(output_dir)
        self._format = fmt
        self._entries: list[dict[str, Any]] = []
        self._session_start: float = 0.0

    @property
    def name(self) -> str:
        return "transcript_logger"

    async def start(self) -> None:
        self._output_dir.mkdir(parents=True, exist_ok=True)
        self._session_start = time.time()
        self._entries = []
        logger.info("Transcript logging enabled → %s", self._output_dir)

    async def stop(self) -> None:
        await self._flush()

    async def _flush(self) -> None:
        if not self._entries:
            return

        ts = time.strftime("%Y%m%d_%H%M%S")
        if self._format == "json":
            path = self._output_dir / f"transcript_{ts}.json"
            path.write_text(json.dumps(self._entries, indent=2))
        else:
            path = self._output_dir / f"transcript_{ts}.txt"
            lines = []
            for entry in self._entries:
                t = entry.get("time", "")
                role = entry.get("role", "")
                text = entry.get("text", "")
                lines.append(f"[{t}] {role}: {text}")
            path.write_text("\n".join(lines))

        logger.info("Transcript saved to %s (%d entries)", path, len(self._entries))

    def add_entry(self, role: str, text: str) -> None:
        """Add a transcript entry (called externally by provider hooks)."""
        elapsed = time.time() - self._session_start
        self._entries.append({
            "time": f"{elapsed:.1f}s",
            "role": role,
            "text": text,
            "timestamp": time.time(),
        })

    async def process_inbound(self, audio: bytes) -> bytes:
        return audio  # Transcript capture happens via add_entry()

    async def process_outbound(self, audio: bytes) -> bytes:
        return audio


class RateLimiter(AudioMiddleware):
    """Token-bucket rate limiter for audio throughput.

    Limits the rate of audio flowing through the pipeline, useful
    for protecting public-facing endpoints (WebRTC, SIP) from abuse.

    Args:
        max_bytes_per_second: Maximum audio bytes per second per connection.
                              Default 96000 (2 seconds of 24kHz 16-bit mono
                              per second — generous for real-time).
        burst_bytes: Maximum burst size. Default 48000 (1 second buffer).
    """

    def __init__(
        self, max_bytes_per_second: int = 96000, burst_bytes: int = 48000
    ) -> None:
        self._rate = max_bytes_per_second
        self._burst = burst_bytes
        self._tokens = float(burst_bytes)
        self._last_time = time.monotonic()
        self._dropped_bytes = 0

    @property
    def name(self) -> str:
        return "rate_limiter"

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last_time
        self._last_time = now
        self._tokens = min(self._burst, self._tokens + elapsed * self._rate)

    async def process_inbound(self, audio: bytes) -> bytes:
        self._refill()
        if self._tokens >= len(audio):
            self._tokens -= len(audio)
            return audio
        else:
            self._dropped_bytes += len(audio)
            if self._dropped_bytes % 48000 == 0:
                logger.warning(
                    "Rate limiter: dropped %d bytes total",
                    self._dropped_bytes,
                )
            return b"\x00" * len(audio)  # Replace with silence

    async def process_outbound(self, audio: bytes) -> bytes:
        return audio  # Don't rate-limit outbound AI audio
