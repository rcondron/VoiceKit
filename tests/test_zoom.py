"""Tests for voicekit.platforms.zoom module.

Tests cover the Zoom adapter's audio format conversion, configuration,
and platform logic without requiring the Zoom Meeting SDK installed.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pytest

from voicekit.config import ZoomPlatformConfig
from voicekit.core.audio import AudioFormat, convert_audio
from voicekit.platforms.zoom import (
    ZOOM_AUDIO_FORMAT,
    ZOOM_FRAME_BYTES,
    ZoomPlatform,
)


class TestZoomPlatformConfig:
    def test_default_config(self):
        cfg = ZoomPlatformConfig()
        assert cfg.enabled is False
        assert cfg.client_id == ""
        assert cfg.client_secret == ""
        assert cfg.account_id == ""
        assert cfg.meeting_id == ""
        assert cfg.meeting_passcode == ""
        assert cfg.display_name == "VoiceKit AI"
        assert cfg.auto_join is False
        assert cfg.enable_sdk_log is False

    def test_custom_config(self):
        cfg = ZoomPlatformConfig(
            enabled=True,
            client_id="test-client-id",
            client_secret="test-secret",
            account_id="test-account",
            meeting_id="1234567890",
            meeting_passcode="abc123",
            display_name="My Bot",
            auto_join=True,
            enable_sdk_log=True,
        )
        assert cfg.enabled is True
        assert cfg.client_id == "test-client-id"
        assert cfg.client_secret == "test-secret"
        assert cfg.account_id == "test-account"
        assert cfg.meeting_id == "1234567890"
        assert cfg.meeting_passcode == "abc123"
        assert cfg.display_name == "My Bot"
        assert cfg.auto_join is True
        assert cfg.enable_sdk_log is True


class TestZoomAudioFormat:
    """Test audio conversion between internal and Zoom formats."""

    def test_zoom_format_constants(self):
        assert ZOOM_AUDIO_FORMAT.sample_rate == 32000
        assert ZOOM_AUDIO_FORMAT.channels == 1
        assert ZOOM_AUDIO_FORMAT.sample_width == 2
        assert ZOOM_FRAME_BYTES == 1280

    def test_internal_to_zoom_conversion(self):
        """24kHz mono → 32kHz mono: 4/3 sample count."""
        internal_fmt = AudioFormat()  # 24kHz, mono
        # 480 samples at 24kHz = 20ms
        samples = np.zeros(480, dtype=np.int16)
        internal_data = samples.tobytes()

        zoom_data = convert_audio(internal_data, internal_fmt, ZOOM_AUDIO_FORMAT)
        zoom_samples = np.frombuffer(zoom_data, dtype=np.int16)

        # 32kHz/24kHz = 4/3 ratio, so 480 * 4/3 = 640 samples
        assert len(zoom_samples) == 640

    def test_zoom_to_internal_conversion(self):
        """32kHz mono → 24kHz mono: 3/4 sample count."""
        internal_fmt = AudioFormat()
        # 640 samples at 32kHz = 20ms
        samples = np.zeros(640, dtype=np.int16)
        zoom_data = samples.tobytes()

        internal_data = convert_audio(zoom_data, ZOOM_AUDIO_FORMAT, internal_fmt)
        internal_samples = np.frombuffer(internal_data, dtype=np.int16)

        # 24kHz/32kHz = 3/4 ratio, so 640 * 3/4 = 480 samples
        assert len(internal_samples) == 480

    def test_roundtrip_preserves_silence(self):
        """Converting silence from internal→zoom→internal stays silent."""
        internal_fmt = AudioFormat()
        silence = np.zeros(480, dtype=np.int16).tobytes()

        zoom = convert_audio(silence, internal_fmt, ZOOM_AUDIO_FORMAT)
        back = convert_audio(zoom, ZOOM_AUDIO_FORMAT, internal_fmt)

        result = np.frombuffer(back, dtype=np.int16)
        assert np.all(result == 0)


class TestZoomPlatformUnit:
    """Unit tests for ZoomPlatform without connecting to Zoom."""

    def test_platform_name(self):
        cfg = ZoomPlatformConfig()
        platform = ZoomPlatform(cfg)
        assert platform.name == "zoom"
        assert platform.is_active is False

    def test_on_audio_received_stores_callback(self):
        cfg = ZoomPlatformConfig()
        platform = ZoomPlatform(cfg)
        callback = AsyncMock()
        platform.on_audio_received(callback)
        assert platform._callback is callback

    @pytest.mark.asyncio
    async def test_send_audio_noop_when_inactive(self):
        cfg = ZoomPlatformConfig()
        platform = ZoomPlatform(cfg)
        # Should not raise when platform is inactive
        await platform.send_audio(b"\x00" * 100)

    @pytest.mark.asyncio
    async def test_send_audio_noop_when_not_in_meeting(self):
        cfg = ZoomPlatformConfig()
        platform = ZoomPlatform(cfg)
        platform._active = True
        # Not in a meeting, should be no-op
        await platform.send_audio(b"\x00" * 960)
        assert platform._output_queue.empty()

    @pytest.mark.asyncio
    async def test_send_audio_queues_when_in_meeting(self):
        cfg = ZoomPlatformConfig()
        platform = ZoomPlatform(cfg)
        platform._active = True
        platform._in_meeting = True

        # 480 samples at 24kHz → converted to 32kHz and queued
        await platform.send_audio(b"\x00" * 960)

        assert not platform._output_queue.empty()

    @pytest.mark.asyncio
    async def test_send_audio_drops_oldest_when_queue_full(self):
        cfg = ZoomPlatformConfig()
        platform = ZoomPlatform(cfg)
        platform._active = True
        platform._in_meeting = True

        # Fill the queue
        for _ in range(200):
            platform._output_queue.put_nowait(b"\x00" * ZOOM_FRAME_BYTES)

        assert platform._output_queue.full()

        # Should drop oldest and add new
        await platform.send_audio(b"\x00" * 960)
        assert platform._output_queue.qsize() == 200

    @pytest.mark.asyncio
    async def test_reject_call_noop(self):
        cfg = ZoomPlatformConfig()
        platform = ZoomPlatform(cfg)
        await platform.reject_call()

    @pytest.mark.asyncio
    async def test_hang_up_when_not_in_meeting(self):
        cfg = ZoomPlatformConfig()
        platform = ZoomPlatform(cfg)
        # Should not raise
        await platform.hang_up()
        assert platform._in_meeting is False

    @pytest.mark.asyncio
    async def test_stop_cleans_up_state(self):
        cfg = ZoomPlatformConfig()
        platform = ZoomPlatform(cfg)
        platform._active = True

        await platform.stop()

        assert platform._active is False
        assert platform._sdk is None
        assert platform._meeting_service is None

    @pytest.mark.asyncio
    async def test_start_raises_without_sdk(self):
        cfg = ZoomPlatformConfig()
        platform = ZoomPlatform(cfg)

        with pytest.raises(RuntimeError, match="zoom-meeting-sdk is required"):
            await platform.start()

    @pytest.mark.asyncio
    async def test_join_meeting_warns_when_inactive(self):
        cfg = ZoomPlatformConfig()
        platform = ZoomPlatform(cfg)
        # Should not raise, just log warning
        await platform.join_meeting("1234567890")

    @pytest.mark.asyncio
    async def test_join_meeting_warns_when_already_in_meeting(self):
        cfg = ZoomPlatformConfig()
        platform = ZoomPlatform(cfg)
        platform._active = True
        platform._in_meeting = True
        platform._meeting_service = MagicMock()
        # Should not raise, just log warning
        await platform.join_meeting("1234567890")

    @pytest.mark.asyncio
    async def test_answer_call_joins_configured_meeting(self):
        cfg = ZoomPlatformConfig(meeting_id="9876543210", meeting_passcode="pass")
        platform = ZoomPlatform(cfg)
        platform._active = True
        platform._in_meeting = False
        platform._meeting_service = MagicMock()

        # Mock join_meeting to verify it's called
        platform.join_meeting = AsyncMock()
        await platform.answer_call()

        platform.join_meeting.assert_called_once_with("9876543210", "pass")

    @pytest.mark.asyncio
    async def test_answer_call_noop_without_meeting_id(self):
        cfg = ZoomPlatformConfig()
        platform = ZoomPlatform(cfg)
        platform._active = True
        # No meeting_id configured — should be a no-op
        platform.join_meeting = AsyncMock()
        await platform.answer_call()
        platform.join_meeting.assert_not_called()
