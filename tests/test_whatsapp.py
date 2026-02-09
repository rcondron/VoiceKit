"""Tests for voicekit.platforms.whatsapp module.

Tests cover the WhatsApp platform adapter logic without requiring
WhatsApp Desktop, PulseAudio, or xdotool to be installed.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pytest

from voicekit.config import WhatsAppPlatformConfig
from voicekit.core.audio import AudioFormat, convert_audio


class TestWhatsAppPlatformConfig:
    def test_default_config(self):
        cfg = WhatsAppPlatformConfig()
        assert cfg.enabled is False
        assert cfg.app_path == ""
        assert cfg.auto_answer is True
        assert cfg.allowed_contacts == []
        assert cfg.process_name == "WhatsApp"
        assert cfg.pulse_sink_name == "voicekit_whatsapp"

    def test_custom_config(self):
        cfg = WhatsAppPlatformConfig(
            enabled=True,
            auto_answer=False,
            allowed_contacts=["+1234567890", "Alice"],
            process_name="whatsapp-desktop",
            pulse_sink_name="custom_wa_sink",
        )
        assert cfg.enabled is True
        assert cfg.auto_answer is False
        assert cfg.allowed_contacts == ["+1234567890", "Alice"]
        assert cfg.process_name == "whatsapp-desktop"
        assert cfg.pulse_sink_name == "custom_wa_sink"


class TestWhatsAppAudioFormat:
    """Test audio conversion between internal and WhatsApp formats."""

    def test_internal_to_whatsapp_conversion(self):
        """24kHz mono -> 48kHz mono doubles the sample count."""
        from voicekit.platforms.whatsapp import WHATSAPP_AUDIO_FORMAT

        internal_fmt = AudioFormat()  # 24kHz, mono
        samples = np.zeros(480, dtype=np.int16)
        internal_data = samples.tobytes()

        wa_data = convert_audio(internal_data, internal_fmt, WHATSAPP_AUDIO_FORMAT)
        wa_samples = np.frombuffer(wa_data, dtype=np.int16)

        # 48kHz/24kHz = 2x, so 480 -> 960 samples
        assert len(wa_samples) == 960

    def test_whatsapp_to_internal_conversion(self):
        """48kHz mono -> 24kHz mono halves the sample count."""
        from voicekit.platforms.whatsapp import WHATSAPP_AUDIO_FORMAT

        internal_fmt = AudioFormat()
        samples = np.zeros(960, dtype=np.int16)
        wa_data = samples.tobytes()

        internal_data = convert_audio(wa_data, WHATSAPP_AUDIO_FORMAT, internal_fmt)
        internal_samples = np.frombuffer(internal_data, dtype=np.int16)

        assert len(internal_samples) == 480

    def test_whatsapp_format_constants(self):
        from voicekit.platforms.whatsapp import WHATSAPP_AUDIO_FORMAT, WHATSAPP_FRAME_BYTES

        assert WHATSAPP_AUDIO_FORMAT.sample_rate == 48000
        assert WHATSAPP_AUDIO_FORMAT.channels == 1
        assert WHATSAPP_AUDIO_FORMAT.sample_width == 2
        assert WHATSAPP_FRAME_BYTES == 1920  # 960 samples * 2 bytes


class TestWhatsAppPlatformUnit:
    """Unit tests for WhatsAppPlatform without external dependencies."""

    def test_platform_name(self):
        from voicekit.platforms.whatsapp import WhatsAppPlatform

        cfg = WhatsAppPlatformConfig()
        platform = WhatsAppPlatform(cfg)
        assert platform.name == "whatsapp"
        assert platform.is_active is False

    def test_on_audio_received_stores_callback(self):
        from voicekit.platforms.whatsapp import WhatsAppPlatform

        cfg = WhatsAppPlatformConfig()
        platform = WhatsAppPlatform(cfg)
        callback = AsyncMock()
        platform.on_audio_received(callback)
        assert platform._callback is callback

    @pytest.mark.asyncio
    async def test_send_audio_noop_when_inactive(self):
        from voicekit.platforms.whatsapp import WhatsAppPlatform

        cfg = WhatsAppPlatformConfig()
        platform = WhatsAppPlatform(cfg)
        # Should not raise when platform is inactive
        await platform.send_audio(b"\x00" * 100)

    @pytest.mark.asyncio
    async def test_send_audio_noop_when_not_in_call(self):
        from voicekit.platforms.whatsapp import WhatsAppPlatform

        cfg = WhatsAppPlatformConfig()
        platform = WhatsAppPlatform(cfg)
        platform._active = True
        platform._in_call = False
        # Should not raise when not in a call
        await platform.send_audio(b"\x00" * 100)

    @pytest.mark.asyncio
    async def test_send_audio_writes_to_bridge_when_in_call(self):
        from voicekit.platforms.whatsapp import WhatsAppPlatform

        cfg = WhatsAppPlatformConfig()
        platform = WhatsAppPlatform(cfg)
        platform._active = True
        platform._in_call = True

        mock_bridge = MagicMock()
        mock_bridge.write_audio = AsyncMock()
        platform._bridge = mock_bridge

        await platform.send_audio(b"\x00" * 960)  # 480 samples at 24kHz

        mock_bridge.write_audio.assert_called_once()
        # The audio should have been converted to 48kHz
        written_data = mock_bridge.write_audio.call_args[0][0]
        assert len(written_data) == 1920  # 960 samples * 2 bytes

    @pytest.mark.asyncio
    async def test_on_captured_audio_converts_and_forwards(self):
        from voicekit.platforms.whatsapp import WhatsAppPlatform

        cfg = WhatsAppPlatformConfig()
        platform = WhatsAppPlatform(cfg)
        platform._in_call = True

        callback = AsyncMock()
        platform.on_audio_received(callback)

        # Simulate captured audio at 48kHz mono
        wa_audio = b"\x00" * 1920  # 960 samples at 48kHz
        await platform._on_captured_audio(wa_audio)

        callback.assert_called_once()
        # Should be converted to 24kHz (half the samples)
        internal_data = callback.call_args[0][0]
        assert len(internal_data) == 960  # 480 samples * 2 bytes

    @pytest.mark.asyncio
    async def test_on_captured_audio_noop_when_no_callback(self):
        from voicekit.platforms.whatsapp import WhatsAppPlatform

        cfg = WhatsAppPlatformConfig()
        platform = WhatsAppPlatform(cfg)
        platform._in_call = True
        # No callback registered — should not raise
        await platform._on_captured_audio(b"\x00" * 1920)

    @pytest.mark.asyncio
    async def test_on_captured_audio_noop_when_not_in_call(self):
        from voicekit.platforms.whatsapp import WhatsAppPlatform

        cfg = WhatsAppPlatformConfig()
        platform = WhatsAppPlatform(cfg)
        platform._in_call = False

        callback = AsyncMock()
        platform.on_audio_received(callback)

        await platform._on_captured_audio(b"\x00" * 1920)
        callback.assert_not_called()

    @pytest.mark.asyncio
    async def test_stop_cleans_up_state(self):
        from voicekit.platforms.whatsapp import WhatsAppPlatform

        cfg = WhatsAppPlatformConfig()
        platform = WhatsAppPlatform(cfg)
        platform._active = True
        platform._in_call = True
        platform._window_id = "12345"

        # Mock bridge
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
        from voicekit.platforms.whatsapp import WhatsAppPlatform

        cfg = WhatsAppPlatformConfig()
        platform = WhatsAppPlatform(cfg)

        assert platform._extract_contact_from_title("Alice - Incoming voice call") == "Alice"
        assert platform._extract_contact_from_title("Bob — Incoming video call") == "Bob"
        assert platform._extract_contact_from_title("+1234567890: Ringing") == "+1234567890"
        assert platform._extract_contact_from_title("WhatsApp") is None

    @pytest.mark.asyncio
    async def test_answer_call_sets_in_call(self):
        """answer_call should set _in_call=True when xdotool succeeds."""
        from voicekit.platforms.whatsapp import WhatsAppPlatform

        cfg = WhatsAppPlatformConfig()
        platform = WhatsAppPlatform(cfg)

        # Mock xdotool and window finding
        with patch("shutil.which", return_value="/usr/bin/xdotool"):
            platform._find_whatsapp_window = AsyncMock(return_value="12345")
            platform._xdotool = AsyncMock(return_value=True)

            await platform.answer_call()

            assert platform._in_call is True
            assert platform._window_id == "12345"

    @pytest.mark.asyncio
    async def test_reject_call_clears_in_call(self):
        from voicekit.platforms.whatsapp import WhatsAppPlatform

        cfg = WhatsAppPlatformConfig()
        platform = WhatsAppPlatform(cfg)
        platform._in_call = True

        with patch("shutil.which", return_value="/usr/bin/xdotool"):
            platform._find_whatsapp_window = AsyncMock(return_value="12345")
            platform._xdotool = AsyncMock(return_value=True)

            await platform.reject_call()

            assert platform._in_call is False

    @pytest.mark.asyncio
    async def test_hang_up_noop_when_not_in_call(self):
        from voicekit.platforms.whatsapp import WhatsAppPlatform

        cfg = WhatsAppPlatformConfig()
        platform = WhatsAppPlatform(cfg)
        platform._in_call = False

        # Should be a no-op
        await platform.hang_up()
        assert platform._in_call is False

    @pytest.mark.asyncio
    async def test_answer_call_noop_without_xdotool(self):
        from voicekit.platforms.whatsapp import WhatsAppPlatform

        cfg = WhatsAppPlatformConfig()
        platform = WhatsAppPlatform(cfg)

        with patch("shutil.which", return_value=None):
            await platform.answer_call()
            assert platform._in_call is False
