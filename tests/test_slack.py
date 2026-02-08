"""Tests for voicekit.platforms.slack module.

Tests cover the Slack platform adapter logic without requiring
Slack credentials, slack-bolt, PulseAudio, or a running Slack Desktop.
"""

from unittest.mock import AsyncMock, MagicMock

import numpy as np
import pytest

from voicekit.config import SlackPlatformConfig
from voicekit.core.audio import AudioFormat, convert_audio


class TestSlackPlatformConfig:
    def test_default_config(self):
        cfg = SlackPlatformConfig()
        assert cfg.enabled is False
        assert cfg.bot_token == ""
        assert cfg.app_token == ""
        assert cfg.auto_join_channels == []
        assert cfg.command_prefix == "/voicekit"
        assert cfg.process_name == "slack"
        assert cfg.pulse_sink_name == "voicekit_slack"

    def test_custom_config(self):
        cfg = SlackPlatformConfig(
            enabled=True,
            bot_token="xoxb-test-token",
            app_token="xapp-test-token",
            auto_join_channels=["C123", "C456"],
            command_prefix="/ai",
            process_name="slack-desktop",
        )
        assert cfg.enabled is True
        assert cfg.bot_token == "xoxb-test-token"
        assert cfg.app_token == "xapp-test-token"
        assert cfg.auto_join_channels == ["C123", "C456"]
        assert cfg.command_prefix == "/ai"
        assert cfg.process_name == "slack-desktop"


class TestSlackAudioFormat:
    """Test audio conversion between internal and Slack formats."""

    def test_internal_to_slack_conversion(self):
        """24kHz mono -> 48kHz stereo: 4x sample count."""
        from voicekit.platforms.slack import SLACK_AUDIO_FORMAT

        internal_fmt = AudioFormat()  # 24kHz, mono
        samples = np.zeros(480, dtype=np.int16)
        internal_data = samples.tobytes()

        slack_data = convert_audio(internal_data, internal_fmt, SLACK_AUDIO_FORMAT)
        slack_samples = np.frombuffer(slack_data, dtype=np.int16)

        # 48kHz/24kHz = 2x rate, mono->stereo = 2x channels -> 4x total
        assert len(slack_samples) == 1920

    def test_slack_to_internal_conversion(self):
        """48kHz stereo -> 24kHz mono: 1/4 sample count."""
        from voicekit.platforms.slack import SLACK_AUDIO_FORMAT

        internal_fmt = AudioFormat()
        samples = np.zeros(1920, dtype=np.int16)
        slack_data = samples.tobytes()

        internal_data = convert_audio(slack_data, SLACK_AUDIO_FORMAT, internal_fmt)
        internal_samples = np.frombuffer(internal_data, dtype=np.int16)

        assert len(internal_samples) == 480

    def test_slack_format_constants(self):
        from voicekit.platforms.slack import SLACK_AUDIO_FORMAT, SLACK_FRAME_BYTES

        assert SLACK_AUDIO_FORMAT.sample_rate == 48000
        assert SLACK_AUDIO_FORMAT.channels == 2
        assert SLACK_AUDIO_FORMAT.sample_width == 2
        assert SLACK_FRAME_BYTES == 3840


class TestSlackPlatformUnit:
    """Unit tests for SlackPlatform without Slack API or PulseAudio."""

    def test_platform_name(self):
        from voicekit.platforms.slack import SlackPlatform

        cfg = SlackPlatformConfig()
        platform = SlackPlatform(cfg)
        assert platform.name == "slack"
        assert platform.is_active is False

    def test_on_audio_received_stores_callback(self):
        from voicekit.platforms.slack import SlackPlatform

        cfg = SlackPlatformConfig()
        platform = SlackPlatform(cfg)
        callback = AsyncMock()
        platform.on_audio_received(callback)
        assert platform._callback is callback

    @pytest.mark.asyncio
    async def test_send_audio_noop_when_inactive(self):
        from voicekit.platforms.slack import SlackPlatform

        cfg = SlackPlatformConfig()
        platform = SlackPlatform(cfg)
        await platform.send_audio(b"\x00" * 100)

    @pytest.mark.asyncio
    async def test_send_audio_noop_when_not_in_huddle(self):
        from voicekit.platforms.slack import SlackPlatform

        cfg = SlackPlatformConfig()
        platform = SlackPlatform(cfg)
        platform._active = True
        platform._in_huddle = False
        await platform.send_audio(b"\x00" * 100)

    @pytest.mark.asyncio
    async def test_send_audio_writes_to_bridge_when_in_huddle(self):
        from voicekit.platforms.slack import SlackPlatform

        cfg = SlackPlatformConfig()
        platform = SlackPlatform(cfg)
        platform._active = True
        platform._in_huddle = True

        mock_bridge = MagicMock()
        mock_bridge.write_audio = AsyncMock()
        platform._bridge = mock_bridge

        # 480 samples of 24kHz mono -> gets converted to 48kHz stereo
        await platform.send_audio(b"\x00" * 960)

        mock_bridge.write_audio.assert_called_once()
        written_data = mock_bridge.write_audio.call_args[0][0]
        # 480 * 2 (rate) * 2 (channels) = 1920 samples * 2 bytes = 3840 bytes
        assert len(written_data) == 3840

    @pytest.mark.asyncio
    async def test_on_captured_audio_converts_and_forwards(self):
        from voicekit.platforms.slack import SlackPlatform

        cfg = SlackPlatformConfig()
        platform = SlackPlatform(cfg)
        platform._in_huddle = True

        callback = AsyncMock()
        platform.on_audio_received(callback)

        # 48kHz stereo audio (3840 bytes = 1920 samples)
        slack_audio = b"\x00" * 3840
        await platform._on_captured_audio(slack_audio)

        callback.assert_called_once()
        internal_data = callback.call_args[0][0]
        # 1920 / 4 (stereo->mono + rate/2) = 480 samples * 2 bytes = 960 bytes
        assert len(internal_data) == 960

    @pytest.mark.asyncio
    async def test_on_captured_audio_noop_when_not_in_huddle(self):
        from voicekit.platforms.slack import SlackPlatform

        cfg = SlackPlatformConfig()
        platform = SlackPlatform(cfg)
        platform._in_huddle = False

        callback = AsyncMock()
        platform.on_audio_received(callback)

        await platform._on_captured_audio(b"\x00" * 3840)
        callback.assert_not_called()

    @pytest.mark.asyncio
    async def test_stop_cleans_up_state(self):
        from voicekit.platforms.slack import SlackPlatform

        cfg = SlackPlatformConfig()
        platform = SlackPlatform(cfg)
        platform._active = True
        platform._in_huddle = True
        platform._current_channel = "C123"

        mock_bridge = MagicMock()
        mock_bridge.stop = AsyncMock()
        platform._bridge = mock_bridge

        await platform.stop()

        assert platform._active is False
        assert platform._in_huddle is False
        assert platform._current_channel is None
        assert platform._bridge is None
        assert platform._bolt_app is None
        mock_bridge.stop.assert_called_once()


class TestSlackHuddleManagement:
    """Tests for huddle join/leave logic."""

    @pytest.mark.asyncio
    async def test_join_huddle_sets_state(self):
        from voicekit.platforms.slack import SlackPlatform

        cfg = SlackPlatformConfig()
        platform = SlackPlatform(cfg)

        mock_client = MagicMock()
        mock_client.chat_postMessage = AsyncMock()
        platform._web_client = mock_client

        mock_bridge = MagicMock()
        mock_bridge.route_app_streams = AsyncMock()
        platform._bridge = mock_bridge

        await platform._join_huddle("C123")

        assert platform._in_huddle is True
        assert platform._current_channel == "C123"
        mock_bridge.route_app_streams.assert_called_once()

    @pytest.mark.asyncio
    async def test_leave_huddle_clears_state(self):
        from voicekit.platforms.slack import SlackPlatform

        cfg = SlackPlatformConfig()
        platform = SlackPlatform(cfg)
        platform._in_huddle = True
        platform._current_channel = "C123"

        mock_client = MagicMock()
        mock_client.chat_postMessage = AsyncMock()
        platform._web_client = mock_client

        await platform._leave_huddle()

        assert platform._in_huddle is False
        assert platform._current_channel is None

    @pytest.mark.asyncio
    async def test_leave_huddle_noop_when_not_in_huddle(self):
        from voicekit.platforms.slack import SlackPlatform

        cfg = SlackPlatformConfig()
        platform = SlackPlatform(cfg)
        platform._in_huddle = False

        await platform._leave_huddle()
        assert platform._in_huddle is False

    @pytest.mark.asyncio
    async def test_join_huddle_leaves_current_first(self):
        from voicekit.platforms.slack import SlackPlatform

        cfg = SlackPlatformConfig()
        platform = SlackPlatform(cfg)
        platform._in_huddle = True
        platform._current_channel = "C123"

        mock_client = MagicMock()
        mock_client.chat_postMessage = AsyncMock()
        platform._web_client = mock_client

        mock_bridge = MagicMock()
        mock_bridge.route_app_streams = AsyncMock()
        platform._bridge = mock_bridge

        await platform._join_huddle("C456")

        # Should have left old and joined new
        assert platform._in_huddle is True
        assert platform._current_channel == "C456"

    @pytest.mark.asyncio
    async def test_answer_call_noop(self):
        from voicekit.platforms.slack import SlackPlatform

        cfg = SlackPlatformConfig()
        platform = SlackPlatform(cfg)
        await platform.answer_call()

    @pytest.mark.asyncio
    async def test_hang_up_leaves_huddle(self):
        from voicekit.platforms.slack import SlackPlatform

        cfg = SlackPlatformConfig()
        platform = SlackPlatform(cfg)
        platform._in_huddle = True
        platform._current_channel = "C123"

        mock_client = MagicMock()
        mock_client.chat_postMessage = AsyncMock()
        platform._web_client = mock_client

        await platform.hang_up()

        assert platform._in_huddle is False
        assert platform._current_channel is None
