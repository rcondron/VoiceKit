"""Tests for voicekit.platforms.sip module.

Tests cover the SIP adapter's audio format conversion, RTP packet
handling, SDP parsing, configuration, and platform logic without
requiring an actual SIP server or the aiosip library installed.
"""

import asyncio
import audioop
import struct
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pytest

from voicekit.config import SipPlatformConfig
from voicekit.core.audio import AudioFormat, convert_audio
from voicekit.platforms.sip import (
    DTMF_DURATION,
    RTP_PT_L16_16K,
    RTP_PT_PCMU,
    SIP_AUDIO_FORMAT_L16,
    SIP_AUDIO_FORMAT_PCMU,
    SIP_FRAME_BYTES_PCM16,
    SIP_FRAME_BYTES_ULAW,
    SIP_FRAME_SAMPLES,
    SipPlatform,
    _RtpProtocol,
)


class TestSipPlatformConfig:
    def test_default_config(self):
        cfg = SipPlatformConfig()
        assert cfg.enabled is False
        assert cfg.server == ""
        assert cfg.username == ""
        assert cfg.password == ""
        assert cfg.port == 5060
        assert cfg.auto_answer is True
        assert cfg.allowed_numbers == []
        assert cfg.local_rtp_port_start == 10000
        assert cfg.register_expires == 3600

    def test_custom_config(self):
        cfg = SipPlatformConfig(
            enabled=True,
            server="sip.example.com",
            username="1001",
            password="secret",
            port=5061,
            auto_answer=False,
            allowed_numbers=["+15551234567", "+15559876543"],
            local_rtp_port_start=20000,
            register_expires=1800,
        )
        assert cfg.enabled is True
        assert cfg.server == "sip.example.com"
        assert cfg.username == "1001"
        assert cfg.password == "secret"
        assert cfg.port == 5061
        assert cfg.auto_answer is False
        assert len(cfg.allowed_numbers) == 2
        assert cfg.local_rtp_port_start == 20000
        assert cfg.register_expires == 1800


class TestSipAudioFormat:
    """Test audio conversion between internal and SIP formats."""

    def test_pcmu_format_constants(self):
        assert SIP_AUDIO_FORMAT_PCMU.sample_rate == 8000
        assert SIP_AUDIO_FORMAT_PCMU.channels == 1
        assert SIP_AUDIO_FORMAT_PCMU.sample_width == 2
        assert SIP_FRAME_SAMPLES == 160
        assert SIP_FRAME_BYTES_ULAW == 160
        assert SIP_FRAME_BYTES_PCM16 == 320

    def test_l16_format_constants(self):
        assert SIP_AUDIO_FORMAT_L16.sample_rate == 16000
        assert SIP_AUDIO_FORMAT_L16.channels == 1
        assert SIP_AUDIO_FORMAT_L16.sample_width == 2

    def test_internal_to_pcmu_conversion(self):
        """24kHz mono → 8kHz mono: 1/3 sample count."""
        internal_fmt = AudioFormat()
        # 480 samples at 24kHz = 20ms
        samples = np.zeros(480, dtype=np.int16)
        internal_data = samples.tobytes()

        pcmu_data = convert_audio(internal_data, internal_fmt, SIP_AUDIO_FORMAT_PCMU)
        pcmu_samples = np.frombuffer(pcmu_data, dtype=np.int16)

        # 8kHz/24kHz = 1/3, so 480 / 3 = 160 samples
        assert len(pcmu_samples) == 160

    def test_pcmu_to_internal_conversion(self):
        """8kHz mono → 24kHz mono: 3x sample count."""
        internal_fmt = AudioFormat()
        # 160 samples at 8kHz = 20ms
        samples = np.zeros(160, dtype=np.int16)
        pcmu_data = samples.tobytes()

        internal_data = convert_audio(pcmu_data, SIP_AUDIO_FORMAT_PCMU, internal_fmt)
        internal_samples = np.frombuffer(internal_data, dtype=np.int16)

        assert len(internal_samples) == 480

    def test_internal_to_l16_conversion(self):
        """24kHz mono → 16kHz mono: 2/3 sample count."""
        internal_fmt = AudioFormat()
        samples = np.zeros(480, dtype=np.int16)
        internal_data = samples.tobytes()

        l16_data = convert_audio(internal_data, internal_fmt, SIP_AUDIO_FORMAT_L16)
        l16_samples = np.frombuffer(l16_data, dtype=np.int16)

        # 16kHz/24kHz = 2/3, so 480 * 2/3 = 320 samples
        assert len(l16_samples) == 320

    def test_l16_to_internal_conversion(self):
        """16kHz mono → 24kHz mono: 3/2 sample count."""
        internal_fmt = AudioFormat()
        samples = np.zeros(320, dtype=np.int16)
        l16_data = samples.tobytes()

        internal_data = convert_audio(l16_data, SIP_AUDIO_FORMAT_L16, internal_fmt)
        internal_samples = np.frombuffer(internal_data, dtype=np.int16)

        assert len(internal_samples) == 480

    def test_rtp_payload_type_constants(self):
        assert RTP_PT_PCMU == 0
        assert RTP_PT_L16_16K == 96
        assert DTMF_DURATION == 1600


class TestSipSdpParsing:
    """Test SDP parsing for RTP address extraction."""

    def test_parse_rtp_from_sdp_basic(self):
        cfg = SipPlatformConfig()
        platform = SipPlatform(cfg)

        sdp = (
            "v=0\r\n"
            "o=- 0 0 IN IP4 192.168.1.100\r\n"
            "s=Session\r\n"
            "c=IN IP4 192.168.1.100\r\n"
            "t=0 0\r\n"
            "m=audio 12000 RTP/AVP 0\r\n"
            "a=rtpmap:0 PCMU/8000\r\n"
        )

        ip, port = platform._parse_rtp_from_sdp(sdp)
        assert ip == "192.168.1.100"
        assert port == 12000

    def test_parse_rtp_from_sdp_no_connection(self):
        cfg = SipPlatformConfig()
        platform = SipPlatform(cfg)

        sdp = "v=0\r\nm=audio 5000 RTP/AVP 0\r\n"
        ip, port = platform._parse_rtp_from_sdp(sdp)
        assert ip == "0.0.0.0"
        assert port == 5000

    def test_parse_rtp_from_sdp_empty(self):
        cfg = SipPlatformConfig()
        platform = SipPlatform(cfg)

        ip, port = platform._parse_rtp_from_sdp("")
        assert ip == "0.0.0.0"
        assert port == 0


class TestSipNumberExtraction:
    """Test phone number extraction from SIP URIs."""

    def test_extract_simple_sip_uri(self):
        cfg = SipPlatformConfig()
        platform = SipPlatform(cfg)

        assert platform._extract_number("sip:+15551234567@server.com") == "+15551234567"

    def test_extract_with_display_name(self):
        cfg = SipPlatformConfig()
        platform = SipPlatform(cfg)

        assert platform._extract_number('"John" <sip:1001@pbx.local>') == "1001"

    def test_extract_plain_number(self):
        cfg = SipPlatformConfig()
        platform = SipPlatform(cfg)

        assert platform._extract_number("+15551234567@server.com") == "+15551234567"

    def test_extract_extension(self):
        cfg = SipPlatformConfig()
        platform = SipPlatform(cfg)

        assert platform._extract_number("sip:200@192.168.1.1") == "200"


class TestSipByteOrderConversion:
    """Test PCM byte order conversion (network ↔ host)."""

    def test_ntohs_pcm(self):
        """Network (big-endian) → host (little-endian)."""
        # 2 samples in big-endian: 0x0100 and 0x0200
        big_endian = struct.pack("!2h", 256, 512)
        little_endian = SipPlatform._ntohs_pcm(big_endian)
        samples = struct.unpack("<2h", little_endian)
        assert samples == (256, 512)

    def test_htons_pcm(self):
        """Host (little-endian) → network (big-endian)."""
        little_endian = struct.pack("<2h", 256, 512)
        big_endian = SipPlatform._htons_pcm(little_endian)
        samples = struct.unpack("!2h", big_endian)
        assert samples == (256, 512)

    def test_roundtrip_byte_order(self):
        """ntohs → htons should be identity."""
        original = struct.pack("!4h", 100, -200, 300, -400)
        converted = SipPlatform._ntohs_pcm(original)
        restored = SipPlatform._htons_pcm(converted)
        assert restored == original


class TestSipPlatformUnit:
    """Unit tests for SipPlatform without connecting to a SIP server."""

    def test_platform_name(self):
        cfg = SipPlatformConfig()
        platform = SipPlatform(cfg)
        assert platform.name == "sip"
        assert platform.is_active is False

    def test_on_audio_received_stores_callback(self):
        cfg = SipPlatformConfig()
        platform = SipPlatform(cfg)
        callback = AsyncMock()
        platform.on_audio_received(callback)
        assert platform._callback is callback

    def test_audio_format_property_narrowband(self):
        cfg = SipPlatformConfig()
        platform = SipPlatform(cfg)
        platform._use_wideband = False
        assert platform._audio_format == SIP_AUDIO_FORMAT_PCMU

    def test_audio_format_property_wideband(self):
        cfg = SipPlatformConfig()
        platform = SipPlatform(cfg)
        platform._use_wideband = True
        assert platform._audio_format == SIP_AUDIO_FORMAT_L16

    @pytest.mark.asyncio
    async def test_send_audio_noop_when_inactive(self):
        cfg = SipPlatformConfig()
        platform = SipPlatform(cfg)
        await platform.send_audio(b"\x00" * 100)

    @pytest.mark.asyncio
    async def test_send_audio_noop_when_not_in_call(self):
        cfg = SipPlatformConfig()
        platform = SipPlatform(cfg)
        platform._active = True
        await platform.send_audio(b"\x00" * 960)
        assert platform._output_queue.empty()

    @pytest.mark.asyncio
    async def test_send_audio_queues_when_in_call(self):
        cfg = SipPlatformConfig()
        platform = SipPlatform(cfg)
        platform._active = True
        platform._in_call = True

        await platform.send_audio(b"\x00" * 960)
        assert not platform._output_queue.empty()

    @pytest.mark.asyncio
    async def test_send_audio_drops_oldest_when_queue_full(self):
        cfg = SipPlatformConfig()
        platform = SipPlatform(cfg)
        platform._active = True
        platform._in_call = True

        for _ in range(200):
            platform._output_queue.put_nowait(b"\x00" * SIP_FRAME_BYTES_PCM16)

        assert platform._output_queue.full()

        await platform.send_audio(b"\x00" * 960)
        assert platform._output_queue.qsize() == 200

    @pytest.mark.asyncio
    async def test_hang_up_when_not_in_call(self):
        cfg = SipPlatformConfig()
        platform = SipPlatform(cfg)
        await platform.hang_up()
        assert platform._in_call is False

    @pytest.mark.asyncio
    async def test_reject_call_clears_dialog(self):
        cfg = SipPlatformConfig()
        platform = SipPlatform(cfg)
        mock_dialog = MagicMock()
        mock_dialog._pending_request = MagicMock()
        platform._active_dialog = mock_dialog

        await platform.reject_call()
        assert platform._active_dialog is None

    @pytest.mark.asyncio
    async def test_stop_cleans_up_state(self):
        cfg = SipPlatformConfig()
        platform = SipPlatform(cfg)
        platform._active = True

        await platform.stop()

        assert platform._active is False
        assert platform._sip_app is None
        assert platform._transport is None

    @pytest.mark.asyncio
    async def test_start_raises_without_aiosip(self):
        cfg = SipPlatformConfig()
        platform = SipPlatform(cfg)

        with pytest.raises(RuntimeError, match="aiosip is required"):
            await platform.start()

    @pytest.mark.asyncio
    async def test_make_call_warns_when_inactive(self):
        cfg = SipPlatformConfig()
        platform = SipPlatform(cfg)
        # Should not raise
        await platform.make_call("+15551234567")

    @pytest.mark.asyncio
    async def test_make_call_warns_when_already_in_call(self):
        cfg = SipPlatformConfig()
        platform = SipPlatform(cfg)
        platform._active = True
        platform._in_call = True
        platform._transport = MagicMock()
        # Should not raise
        await platform.make_call("+15551234567")


class TestRtpProtocol:
    """Test the minimal RTP datagram protocol."""

    def test_datagram_received_calls_callback(self):
        callback = MagicMock()
        protocol = _RtpProtocol(callback)

        data = b"\x80\x00\x00\x01\x00\x00\x00\xa0\x00\x00\x00\x01" + b"\x00" * 160
        addr = ("192.168.1.1", 12000)

        protocol.datagram_received(data, addr)
        callback.assert_called_once_with(data, addr)

    def test_error_received_does_not_raise(self):
        callback = MagicMock()
        protocol = _RtpProtocol(callback)
        # Should not raise
        protocol.error_received(OSError("test error"))


class TestRtpPacketHandling:
    """Test RTP receive-side packet parsing and audio decoding."""

    def test_on_rtp_received_ignores_short_packets(self):
        cfg = SipPlatformConfig()
        platform = SipPlatform(cfg)
        platform._active = True
        platform._loop = asyncio.new_event_loop()
        platform._callback = AsyncMock()

        # Packet too short for RTP header (< 12 bytes)
        platform._on_rtp_received(b"\x80\x00\x00", ("1.2.3.4", 5000))
        # Callback should not have been called
        platform._callback.assert_not_called()
        platform._loop.close()

    def test_on_rtp_received_ignores_unsupported_payload_type(self):
        cfg = SipPlatformConfig()
        platform = SipPlatform(cfg)
        platform._active = True
        platform._loop = asyncio.new_event_loop()
        platform._callback = AsyncMock()

        # Build RTP header with unsupported PT=99
        rtp_header = struct.pack("!BBHII", 0x80, 99, 1, 160, 12345)
        payload = b"\x00" * 160

        platform._on_rtp_received(rtp_header + payload, ("1.2.3.4", 5000))
        platform._callback.assert_not_called()
        platform._loop.close()

    def test_on_rtp_received_does_nothing_without_callback(self):
        cfg = SipPlatformConfig()
        platform = SipPlatform(cfg)
        platform._active = True
        platform._loop = asyncio.new_event_loop()
        # No callback set

        rtp_header = struct.pack("!BBHII", 0x80, RTP_PT_PCMU, 1, 160, 12345)
        payload = b"\x7f" * 160  # u-law silence

        # Should not raise
        platform._on_rtp_received(rtp_header + payload, ("1.2.3.4", 5000))
        platform._loop.close()
