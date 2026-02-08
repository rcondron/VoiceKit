"""Tests for voicekit.core.pulse_bridge module.

Tests cover the PulseAudioBridge logic without requiring PulseAudio
to be installed.
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from voicekit.core.pulse_bridge import PulseAudioBridge, find_process_pulse_streams


class TestPulseAudioBridgeProperties:
    """Test bridge configuration and properties."""

    def test_sink_names(self):
        bridge = PulseAudioBridge(
            app_process_name="WhatsApp",
            sink_prefix="voicekit_whatsapp",
        )
        assert bridge.capture_sink == "voicekit_whatsapp_capture"
        assert bridge.inject_sink == "voicekit_whatsapp_inject"

    def test_audio_format(self):
        bridge = PulseAudioBridge(
            app_process_name="test",
            sink_prefix="test",
            sample_rate=48000,
            channels=2,
        )
        fmt = bridge.audio_format
        assert fmt.sample_rate == 48000
        assert fmt.channels == 2
        assert fmt.sample_width == 2

    def test_default_format(self):
        bridge = PulseAudioBridge(
            app_process_name="test",
            sink_prefix="test",
        )
        fmt = bridge.audio_format
        assert fmt.sample_rate == 48000
        assert fmt.channels == 1

    def test_matches_app(self):
        bridge = PulseAudioBridge(
            app_process_name="WhatsApp",
            sink_prefix="test",
        )
        assert bridge._matches_app("whatsapp") is True
        assert bridge._matches_app("WhatsApp") is True
        assert bridge._matches_app("/usr/bin/whatsapp") is True
        assert bridge._matches_app("firefox") is False
        assert bridge._matches_app("") is False


class TestPulseAudioBridgeSetup:
    """Test bridge setup and teardown."""

    @pytest.mark.asyncio
    async def test_setup_creates_two_modules(self):
        bridge = PulseAudioBridge(
            app_process_name="test",
            sink_prefix="vk_test",
        )

        call_count = 0

        async def mock_load_module(module, **kwargs):
            nonlocal call_count
            call_count += 1
            return 100 + call_count

        bridge._load_module = mock_load_module

        # Mock pactl --version check
        with patch("voicekit.core.pulse_bridge._run_pactl", new_callable=AsyncMock) as mock_pactl:
            mock_pactl.return_value = ("pactl 16.1", "", 0)
            await bridge.setup()

        assert bridge._capture_module_id == 101
        assert bridge._inject_module_id == 102

    @pytest.mark.asyncio
    async def test_setup_raises_if_pactl_missing(self):
        bridge = PulseAudioBridge(
            app_process_name="test",
            sink_prefix="vk_test",
        )

        with patch("voicekit.core.pulse_bridge._run_pactl", side_effect=FileNotFoundError):
            with pytest.raises(RuntimeError, match="pactl not found"):
                await bridge.setup()

    @pytest.mark.asyncio
    async def test_stop_unloads_modules(self):
        bridge = PulseAudioBridge(
            app_process_name="test",
            sink_prefix="vk_test",
        )
        bridge._capture_module_id = 101
        bridge._inject_module_id = 102
        bridge._active = True

        unloaded = []

        async def mock_unload(mod_id):
            unloaded.append(mod_id)

        bridge._unload_module = mock_unload

        await bridge.stop()

        assert 101 in unloaded
        assert 102 in unloaded
        assert bridge._capture_module_id is None
        assert bridge._inject_module_id is None

    @pytest.mark.asyncio
    async def test_stop_terminates_subprocesses(self):
        bridge = PulseAudioBridge(
            app_process_name="test",
            sink_prefix="vk_test",
        )
        bridge._active = True

        # Mock subprocesses
        mock_parec = MagicMock()
        mock_parec.returncode = None
        mock_parec.terminate = MagicMock()
        mock_parec.kill = MagicMock()
        mock_parec.wait = AsyncMock()
        bridge._parec_proc = mock_parec

        mock_pacat = MagicMock()
        mock_pacat.returncode = None
        mock_pacat.terminate = MagicMock()
        mock_pacat.kill = MagicMock()
        mock_pacat.wait = AsyncMock()
        bridge._pacat_proc = mock_pacat

        bridge._unload_module = AsyncMock()

        await bridge.stop()

        mock_parec.terminate.assert_called_once()
        mock_pacat.terminate.assert_called_once()
        assert bridge._parec_proc is None
        assert bridge._pacat_proc is None


class TestPulseAudioBridgeWriteAudio:
    """Test audio writing to the inject sink."""

    @pytest.mark.asyncio
    async def test_write_audio_to_pacat(self):
        bridge = PulseAudioBridge(
            app_process_name="test",
            sink_prefix="vk_test",
        )

        mock_stdin = MagicMock()
        mock_stdin.write = MagicMock()
        mock_stdin.drain = AsyncMock()

        mock_proc = MagicMock()
        mock_proc.stdin = mock_stdin
        bridge._pacat_proc = mock_proc

        audio = b"\x00" * 1920
        await bridge.write_audio(audio)

        mock_stdin.write.assert_called_once_with(audio)
        mock_stdin.drain.assert_called_once()

    @pytest.mark.asyncio
    async def test_write_audio_noop_when_no_process(self):
        bridge = PulseAudioBridge(
            app_process_name="test",
            sink_prefix="vk_test",
        )
        bridge._pacat_proc = None
        # Should not raise
        await bridge.write_audio(b"\x00" * 1920)

    @pytest.mark.asyncio
    async def test_write_audio_handles_broken_pipe(self):
        bridge = PulseAudioBridge(
            app_process_name="test",
            sink_prefix="vk_test",
        )

        mock_stdin = MagicMock()
        mock_stdin.write = MagicMock(side_effect=BrokenPipeError)
        mock_proc = MagicMock()
        mock_proc.stdin = mock_stdin
        bridge._pacat_proc = mock_proc

        # Should not raise
        await bridge.write_audio(b"\x00" * 1920)


class TestStreamRouting:
    """Test application stream routing logic."""

    @pytest.mark.asyncio
    async def test_route_sink_inputs(self):
        bridge = PulseAudioBridge(
            app_process_name="WhatsApp",
            sink_prefix="vk_wa",
        )

        streams = [
            {
                "index": 42,
                "sink": "default_output",
                "properties": {
                    "application.process.binary": "whatsapp-desktop",
                },
            },
            {
                "index": 43,
                "sink": "default_output",
                "properties": {
                    "application.process.binary": "firefox",
                },
            },
        ]

        bridge._list_streams = AsyncMock(return_value=streams)

        moved = []

        async def mock_pactl(*args):
            if args[0] == "move-sink-input":
                moved.append((args[1], args[2]))
            return ("", "", 0)

        with patch("voicekit.core.pulse_bridge._run_pactl", side_effect=mock_pactl):
            await bridge._route_sink_inputs()

        # Only WhatsApp stream should be moved
        assert len(moved) == 1
        assert moved[0] == ("42", "vk_wa_capture")

    @pytest.mark.asyncio
    async def test_route_source_outputs(self):
        bridge = PulseAudioBridge(
            app_process_name="signal",
            sink_prefix="vk_sig",
        )

        streams = [
            {
                "index": 10,
                "source": "default_input",
                "properties": {
                    "application.process.binary": "signal-desktop",
                },
            },
        ]

        bridge._list_streams = AsyncMock(return_value=streams)

        moved = []

        async def mock_pactl(*args):
            if args[0] == "move-source-output":
                moved.append((args[1], args[2]))
            return ("", "", 0)

        with patch("voicekit.core.pulse_bridge._run_pactl", side_effect=mock_pactl):
            await bridge._route_source_outputs()

        assert len(moved) == 1
        assert moved[0] == ("10", "vk_sig_inject.monitor")


class TestFindProcessStreams:
    """Tests for the find_process_pulse_streams utility function."""

    @pytest.mark.asyncio
    async def test_find_matching_streams(self):
        streams_json = json.dumps([
            {
                "index": 1,
                "properties": {"application.process.binary": "whatsapp"},
            },
            {
                "index": 2,
                "properties": {"application.process.binary": "firefox"},
            },
            {
                "index": 3,
                "properties": {"application.process.binary": "whatsapp-web"},
            },
        ])

        with patch("voicekit.core.pulse_bridge._run_pactl", new_callable=AsyncMock) as mock:
            mock.return_value = (streams_json, "", 0)
            result = await find_process_pulse_streams("whatsapp")

        assert result == [1, 3]

    @pytest.mark.asyncio
    async def test_find_no_matching_streams(self):
        streams_json = json.dumps([
            {
                "index": 1,
                "properties": {"application.process.binary": "firefox"},
            },
        ])

        with patch("voicekit.core.pulse_bridge._run_pactl", new_callable=AsyncMock) as mock:
            mock.return_value = (streams_json, "", 0)
            result = await find_process_pulse_streams("whatsapp")

        assert result == []

    @pytest.mark.asyncio
    async def test_find_streams_handles_pactl_error(self):
        with patch("voicekit.core.pulse_bridge._run_pactl", new_callable=AsyncMock) as mock:
            mock.return_value = ("", "error", 1)
            result = await find_process_pulse_streams("whatsapp")

        assert result == []

    @pytest.mark.asyncio
    async def test_find_streams_handles_missing_pactl(self):
        with patch("voicekit.core.pulse_bridge._run_pactl", side_effect=FileNotFoundError):
            result = await find_process_pulse_streams("whatsapp")

        assert result == []
