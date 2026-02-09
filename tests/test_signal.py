"""Tests for voicekit.platforms.signal module.

Tests cover the Signal platform adapter logic without requiring
Signal Desktop, signal-cli, PulseAudio, or xdotool to be installed.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pytest

from voicekit.config import SignalPlatformConfig
from voicekit.core.audio import AudioFormat, convert_audio


class TestSignalPlatformConfig:
    def test_default_config(self):
        cfg = SignalPlatformConfig()
        assert cfg.enabled is False
        assert cfg.auto_answer is True
        assert cfg.allowed_contacts == []
        assert cfg.process_name == "Signal"
        assert cfg.pulse_sink_name == "voicekit_signal"
        assert cfg.signal_cli_path == "signal-cli"
        assert cfg.phone_number == ""
        assert cfg.config_dir == ""

    def test_custom_config(self):
        cfg = SignalPlatformConfig(
            enabled=True,
            auto_answer=False,
            allowed_contacts=["+1234567890"],
            process_name="signal-desktop",
            signal_cli_path="/opt/signal-cli/bin/signal-cli",
            phone_number="+9876543210",
            config_dir="/home/user/.local/share/signal-cli",
        )
        assert cfg.enabled is True
        assert cfg.auto_answer is False
        assert cfg.allowed_contacts == ["+1234567890"]
        assert cfg.signal_cli_path == "/opt/signal-cli/bin/signal-cli"
        assert cfg.phone_number == "+9876543210"
        assert cfg.config_dir == "/home/user/.local/share/signal-cli"


class TestSignalAudioFormat:
    """Test audio conversion between internal and Signal formats."""

    def test_internal_to_signal_conversion(self):
        """24kHz mono -> 48kHz mono doubles the sample count."""
        from voicekit.platforms.signal import SIGNAL_AUDIO_FORMAT

        internal_fmt = AudioFormat()  # 24kHz, mono
        samples = np.zeros(480, dtype=np.int16)
        internal_data = samples.tobytes()

        signal_data = convert_audio(internal_data, internal_fmt, SIGNAL_AUDIO_FORMAT)
        signal_samples = np.frombuffer(signal_data, dtype=np.int16)

        # 48kHz/24kHz = 2x, so 480 -> 960 samples
        assert len(signal_samples) == 960

    def test_signal_to_internal_conversion(self):
        """48kHz mono -> 24kHz mono halves the sample count."""
        from voicekit.platforms.signal import SIGNAL_AUDIO_FORMAT

        internal_fmt = AudioFormat()
        samples = np.zeros(960, dtype=np.int16)
        signal_data = samples.tobytes()

        internal_data = convert_audio(signal_data, SIGNAL_AUDIO_FORMAT, internal_fmt)
        internal_samples = np.frombuffer(internal_data, dtype=np.int16)

        assert len(internal_samples) == 480

    def test_signal_format_constants(self):
        from voicekit.platforms.signal import SIGNAL_AUDIO_FORMAT, SIGNAL_FRAME_BYTES

        assert SIGNAL_AUDIO_FORMAT.sample_rate == 48000
        assert SIGNAL_AUDIO_FORMAT.channels == 1
        assert SIGNAL_AUDIO_FORMAT.sample_width == 2
        assert SIGNAL_FRAME_BYTES == 1920  # 960 samples * 2 bytes


class TestSignalPlatformUnit:
    """Unit tests for SignalPlatform without external dependencies."""

    def test_platform_name(self):
        from voicekit.platforms.signal import SignalPlatform

        cfg = SignalPlatformConfig()
        platform = SignalPlatform(cfg)
        assert platform.name == "signal"
        assert platform.is_active is False

    def test_on_audio_received_stores_callback(self):
        from voicekit.platforms.signal import SignalPlatform

        cfg = SignalPlatformConfig()
        platform = SignalPlatform(cfg)
        callback = AsyncMock()
        platform.on_audio_received(callback)
        assert platform._callback is callback

    @pytest.mark.asyncio
    async def test_send_audio_noop_when_inactive(self):
        from voicekit.platforms.signal import SignalPlatform

        cfg = SignalPlatformConfig()
        platform = SignalPlatform(cfg)
        await platform.send_audio(b"\x00" * 100)

    @pytest.mark.asyncio
    async def test_send_audio_noop_when_not_in_call(self):
        from voicekit.platforms.signal import SignalPlatform

        cfg = SignalPlatformConfig()
        platform = SignalPlatform(cfg)
        platform._active = True
        platform._in_call = False
        await platform.send_audio(b"\x00" * 100)

    @pytest.mark.asyncio
    async def test_send_audio_writes_to_bridge_when_in_call(self):
        from voicekit.platforms.signal import SignalPlatform

        cfg = SignalPlatformConfig()
        platform = SignalPlatform(cfg)
        platform._active = True
        platform._in_call = True

        mock_bridge = MagicMock()
        mock_bridge.write_audio = AsyncMock()
        platform._bridge = mock_bridge

        await platform.send_audio(b"\x00" * 960)

        mock_bridge.write_audio.assert_called_once()
        written_data = mock_bridge.write_audio.call_args[0][0]
        assert len(written_data) == 1920  # 48kHz: 960 samples * 2 bytes

    @pytest.mark.asyncio
    async def test_on_captured_audio_converts_and_forwards(self):
        from voicekit.platforms.signal import SignalPlatform

        cfg = SignalPlatformConfig()
        platform = SignalPlatform(cfg)
        platform._in_call = True

        callback = AsyncMock()
        platform.on_audio_received(callback)

        # 48kHz mono audio
        signal_audio = b"\x00" * 1920
        await platform._on_captured_audio(signal_audio)

        callback.assert_called_once()
        internal_data = callback.call_args[0][0]
        assert len(internal_data) == 960

    @pytest.mark.asyncio
    async def test_on_captured_audio_noop_when_not_in_call(self):
        from voicekit.platforms.signal import SignalPlatform

        cfg = SignalPlatformConfig()
        platform = SignalPlatform(cfg)
        platform._in_call = False

        callback = AsyncMock()
        platform.on_audio_received(callback)

        await platform._on_captured_audio(b"\x00" * 1920)
        callback.assert_not_called()

    @pytest.mark.asyncio
    async def test_stop_cleans_up_state(self):
        from voicekit.platforms.signal import SignalPlatform

        cfg = SignalPlatformConfig()
        platform = SignalPlatform(cfg)
        platform._active = True
        platform._in_call = True
        platform._window_id = "12345"

        mock_bridge = MagicMock()
        mock_bridge.stop = AsyncMock()
        platform._bridge = mock_bridge

        await platform.stop()

        assert platform._active is False
        assert platform._in_call is False
        assert platform._window_id is None
        assert platform._bridge is None
        mock_bridge.stop.assert_called_once()

    def test_extract_contact_from_title(self):
        from voicekit.platforms.signal import SignalPlatform

        cfg = SignalPlatformConfig()
        platform = SignalPlatform(cfg)

        assert platform._extract_contact_from_title("Alice - Incoming call") == "Alice"
        assert platform._extract_contact_from_title("Bob — Signal call") == "Bob"
        assert platform._extract_contact_from_title("Signal") is None


class TestSignalCliIntegration:
    """Tests for signal-cli event handling logic."""

    @pytest.mark.asyncio
    async def test_handle_call_offer_event(self):
        """signal-cli call offer triggers auto-answer."""
        from voicekit.platforms.signal import SignalPlatform

        cfg = SignalPlatformConfig(auto_answer=True)
        platform = SignalPlatform(cfg)
        platform.answer_call = AsyncMock()

        msg = {
            "method": "receive",
            "params": {
                "envelope": {
                    "source": "+1234567890",
                    "callMessage": {
                        "offerMessage": {
                            "id": 123,
                            "type": "OFFER_AUDIO_CALL",
                        }
                    },
                }
            },
        }

        await platform._handle_signalcli_event(msg)
        platform.answer_call.assert_called_once()

    @pytest.mark.asyncio
    async def test_handle_call_offer_respects_allowlist(self):
        """Call from non-allowed contact is ignored."""
        from voicekit.platforms.signal import SignalPlatform

        cfg = SignalPlatformConfig(
            auto_answer=True,
            allowed_contacts=["+9999999999"],
        )
        platform = SignalPlatform(cfg)
        platform.answer_call = AsyncMock()

        msg = {
            "method": "receive",
            "params": {
                "envelope": {
                    "source": "+1234567890",
                    "callMessage": {
                        "offerMessage": {"id": 123},
                    },
                }
            },
        }

        await platform._handle_signalcli_event(msg)
        platform.answer_call.assert_not_called()

    @pytest.mark.asyncio
    async def test_handle_hangup_event(self):
        """Hangup event clears call state."""
        from voicekit.platforms.signal import SignalPlatform

        cfg = SignalPlatformConfig()
        platform = SignalPlatform(cfg)
        platform._in_call = True
        platform._window_id = "12345"

        msg = {
            "method": "receive",
            "params": {
                "envelope": {
                    "source": "+1234567890",
                    "callMessage": {
                        "hangupMessage": {"id": 123},
                    },
                }
            },
        }

        await platform._handle_signalcli_event(msg)
        assert platform._in_call is False
        assert platform._window_id is None

    @pytest.mark.asyncio
    async def test_handle_non_call_event(self):
        """Non-call events are ignored."""
        from voicekit.platforms.signal import SignalPlatform

        cfg = SignalPlatformConfig(auto_answer=True)
        platform = SignalPlatform(cfg)
        platform.answer_call = AsyncMock()

        msg = {
            "method": "receive",
            "params": {
                "envelope": {
                    "source": "+1234567890",
                    "dataMessage": {
                        "message": "Hello!",
                    },
                }
            },
        }

        await platform._handle_signalcli_event(msg)
        platform.answer_call.assert_not_called()

    @pytest.mark.asyncio
    async def test_answer_call_noop_without_xdotool(self):
        from voicekit.platforms.signal import SignalPlatform

        cfg = SignalPlatformConfig()
        platform = SignalPlatform(cfg)

        with patch("shutil.which", return_value=None):
            await platform.answer_call()
            assert platform._in_call is False

    @pytest.mark.asyncio
    async def test_hang_up_noop_when_not_in_call(self):
        from voicekit.platforms.signal import SignalPlatform

        cfg = SignalPlatformConfig()
        platform = SignalPlatform(cfg)
        platform._in_call = False

        await platform.hang_up()
        assert platform._in_call is False
