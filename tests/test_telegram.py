"""Tests for voicekit.platforms.telegram module.

Tests cover the TelegramPlatform adapter logic without requiring
actual Telegram credentials or py-tgcalls installation.
"""

import asyncio
import os
import tempfile
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pytest

from voicekit.config import TelegramPlatformConfig
from voicekit.core.audio import AudioFormat, convert_audio


class TestTelegramPlatformConfig:
    def test_default_config(self):
        cfg = TelegramPlatformConfig()
        assert cfg.enabled is False
        assert cfg.api_id == ""
        assert cfg.api_hash == ""
        assert cfg.session_name == "voicekit"
        assert cfg.auto_answer is True
        assert cfg.auto_join_group_calls is False
        assert cfg.allowed_users == []

    def test_custom_config(self):
        cfg = TelegramPlatformConfig(
            enabled=True,
            api_id="12345",
            api_hash="abc123",
            phone_number="+1234567890",
            session_name="test_session",
            auto_answer=False,
        )
        assert cfg.enabled is True
        assert cfg.api_id == "12345"
        assert cfg.session_name == "test_session"


class TestTelegramAudioFormat:
    """Test audio conversion between internal and Telegram formats."""

    def test_internal_to_telegram_conversion(self):
        """24kHz mono → 48kHz mono doubles the sample count."""
        from voicekit.platforms.telegram import TELEGRAM_AUDIO_FORMAT

        # 480 samples of 24kHz mono = 20ms
        internal_fmt = AudioFormat()  # 24kHz, mono
        samples = np.zeros(480, dtype=np.int16)
        internal_data = samples.tobytes()

        telegram_data = convert_audio(internal_data, internal_fmt, TELEGRAM_AUDIO_FORMAT)
        telegram_samples = np.frombuffer(telegram_data, dtype=np.int16)

        # 48kHz/24kHz = 2x, so 480 → 960 samples
        assert len(telegram_samples) == 960

    def test_telegram_to_internal_conversion(self):
        """48kHz mono → 24kHz mono halves the sample count."""
        from voicekit.platforms.telegram import TELEGRAM_AUDIO_FORMAT

        internal_fmt = AudioFormat()
        samples = np.zeros(960, dtype=np.int16)
        telegram_data = samples.tobytes()

        internal_data = convert_audio(telegram_data, TELEGRAM_AUDIO_FORMAT, internal_fmt)
        internal_samples = np.frombuffer(internal_data, dtype=np.int16)

        assert len(internal_samples) == 480

    def test_telegram_format_constants(self):
        from voicekit.platforms.telegram import TELEGRAM_AUDIO_FORMAT, TELEGRAM_FRAME_BYTES

        assert TELEGRAM_AUDIO_FORMAT.sample_rate == 48000
        assert TELEGRAM_AUDIO_FORMAT.channels == 1
        assert TELEGRAM_AUDIO_FORMAT.sample_width == 2
        assert TELEGRAM_FRAME_BYTES == 1920  # 960 samples * 2 bytes


class TestTelegramPlatformUnit:
    """Unit tests for TelegramPlatform without external dependencies."""

    def test_platform_name(self):
        """Import and check that TelegramPlatform can be imported."""
        from voicekit.platforms.telegram import TelegramPlatform

        cfg = TelegramPlatformConfig(api_id="123", api_hash="abc")
        platform = TelegramPlatform(cfg)
        assert platform.name == "telegram"
        assert platform.is_active is False

    def test_on_audio_received_stores_callback(self):
        from voicekit.platforms.telegram import TelegramPlatform

        cfg = TelegramPlatformConfig()
        platform = TelegramPlatform(cfg)
        callback = AsyncMock()
        platform.on_audio_received(callback)
        assert platform._callback is callback

    @pytest.mark.asyncio
    async def test_send_audio_noop_when_inactive(self):
        from voicekit.platforms.telegram import TelegramPlatform

        cfg = TelegramPlatformConfig()
        platform = TelegramPlatform(cfg)
        # Should not raise when platform is inactive
        await platform.send_audio(b"\x00" * 100)

    @pytest.mark.asyncio
    async def test_send_audio_queues_when_active(self):
        from voicekit.platforms.telegram import TelegramPlatform

        cfg = TelegramPlatformConfig()
        platform = TelegramPlatform(cfg)
        platform._active = True
        platform._active_chat_id = 12345

        await platform.send_audio(b"\x00" * 960)  # 480 samples at 24kHz → queued

        assert not platform._output_queue.empty()

    @pytest.mark.asyncio
    async def test_reject_call_clears_chat_id(self):
        from voicekit.platforms.telegram import TelegramPlatform

        cfg = TelegramPlatformConfig()
        platform = TelegramPlatform(cfg)
        platform._active_chat_id = 12345

        await platform.reject_call()
        assert platform._active_chat_id is None

    @pytest.mark.asyncio
    async def test_stop_cleans_up_state(self):
        from voicekit.platforms.telegram import TelegramPlatform

        cfg = TelegramPlatformConfig()
        platform = TelegramPlatform(cfg)
        platform._active = True

        # Create temp FIFOs to test cleanup
        fifo_dir = tempfile.mkdtemp(prefix="voicekit_test_")
        platform._fifo_dir = fifo_dir
        platform._output_fifo = os.path.join(fifo_dir, "output.pcm")
        platform._input_fifo = os.path.join(fifo_dir, "input.pcm")
        os.mkfifo(platform._output_fifo)
        os.mkfifo(platform._input_fifo)

        await platform.stop()

        assert platform._active is False
        assert platform._output_fifo is None
        assert platform._input_fifo is None
        assert not os.path.exists(fifo_dir)
