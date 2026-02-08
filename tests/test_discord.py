"""Tests for voicekit.platforms.discord module.

Tests cover the Discord adapter's audio source, format conversion,
and platform logic without requiring an actual Discord bot token
or the discord.py library installed.
"""

import asyncio
import queue
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pytest

from voicekit.config import DiscordPlatformConfig
from voicekit.core.audio import AudioFormat, convert_audio
from voicekit.platforms.discord import (
    DISCORD_AUDIO_FORMAT,
    DISCORD_FRAME_BYTES,
    DiscordPlatform,
    StreamingAudioSource,
)


class TestDiscordPlatformConfig:
    def test_default_config(self):
        cfg = DiscordPlatformConfig()
        assert cfg.enabled is False
        assert cfg.bot_token == ""
        assert cfg.auto_join_channels == []
        assert cfg.command_prefix == "!vk"
        assert cfg.guild_ids == []
        assert cfg.listen_to_all_users is True

    def test_custom_config(self):
        cfg = DiscordPlatformConfig(
            enabled=True,
            bot_token="test-token",
            auto_join_channels=["123", "456"],
            command_prefix="!ai",
            guild_ids=[111, 222],
        )
        assert cfg.enabled is True
        assert cfg.bot_token == "test-token"
        assert cfg.auto_join_channels == ["123", "456"]
        assert cfg.command_prefix == "!ai"
        assert cfg.guild_ids == [111, 222]


class TestStreamingAudioSource:
    """Test the custom AudioSource used by the Discord adapter."""

    def test_read_returns_silence_when_empty(self):
        source = StreamingAudioSource()
        frame = source.read()
        assert len(frame) == DISCORD_FRAME_BYTES
        assert frame == b"\x00" * DISCORD_FRAME_BYTES

    def test_read_returns_fed_data(self):
        source = StreamingAudioSource()
        test_data = b"\x01\x02" * (DISCORD_FRAME_BYTES // 2)
        source.feed(test_data)

        frame = source.read()
        assert frame == test_data

    def test_feed_drops_oldest_when_full(self):
        source = StreamingAudioSource()
        for i in range(250):
            source.feed(b"\x00" * DISCORD_FRAME_BYTES)

        # Feed one more — should drop oldest and succeed
        new_data = b"\xff" * DISCORD_FRAME_BYTES
        source.feed(new_data)

        assert source._buffer.qsize() == 250

    def test_is_opus_returns_false(self):
        source = StreamingAudioSource()
        assert source.is_opus() is False

    def test_cleanup_sets_finished(self):
        source = StreamingAudioSource()
        source.cleanup()
        assert source._finished is True

    def test_read_returns_empty_after_cleanup(self):
        source = StreamingAudioSource()
        source.cleanup()
        frame = source.read()
        assert frame == b""

    def test_multiple_feeds_and_reads(self):
        source = StreamingAudioSource()
        frames = [bytes([i % 256]) * DISCORD_FRAME_BYTES for i in range(5)]

        for f in frames:
            source.feed(f)

        for f in frames:
            assert source.read() == f


class TestDiscordAudioFormat:
    """Test audio conversion between internal and Discord formats."""

    def test_internal_to_discord_conversion(self):
        """24kHz mono → 48kHz stereo: 4x sample count."""
        internal_fmt = AudioFormat()  # 24kHz, mono
        samples = np.zeros(480, dtype=np.int16)
        internal_data = samples.tobytes()

        discord_data = convert_audio(internal_data, internal_fmt, DISCORD_AUDIO_FORMAT)
        discord_samples = np.frombuffer(discord_data, dtype=np.int16)

        # 48kHz/24kHz = 2x rate, mono→stereo = 2x channels → 4x total
        assert len(discord_samples) == 1920

    def test_discord_to_internal_conversion(self):
        """48kHz stereo → 24kHz mono: 1/4 sample count."""
        internal_fmt = AudioFormat()
        samples = np.zeros(1920, dtype=np.int16)
        discord_data = samples.tobytes()

        internal_data = convert_audio(discord_data, DISCORD_AUDIO_FORMAT, internal_fmt)
        internal_samples = np.frombuffer(internal_data, dtype=np.int16)

        assert len(internal_samples) == 480

    def test_discord_format_constants(self):
        assert DISCORD_AUDIO_FORMAT.sample_rate == 48000
        assert DISCORD_AUDIO_FORMAT.channels == 2
        assert DISCORD_AUDIO_FORMAT.sample_width == 2
        assert DISCORD_FRAME_BYTES == 3840


class TestDiscordPlatformUnit:
    """Unit tests for DiscordPlatform without connecting to Discord."""

    def test_platform_name(self):
        cfg = DiscordPlatformConfig()
        platform = DiscordPlatform(cfg)
        assert platform.name == "discord"
        assert platform.is_active is False

    def test_on_audio_received_stores_callback(self):
        cfg = DiscordPlatformConfig()
        platform = DiscordPlatform(cfg)
        callback = AsyncMock()
        platform.on_audio_received(callback)
        assert platform._callback is callback

    @pytest.mark.asyncio
    async def test_send_audio_noop_when_inactive(self):
        cfg = DiscordPlatformConfig()
        platform = DiscordPlatform(cfg)
        await platform.send_audio(b"\x00" * 100)

    @pytest.mark.asyncio
    async def test_send_audio_feeds_sources_when_active(self):
        cfg = DiscordPlatformConfig()
        platform = DiscordPlatform(cfg)
        platform._active = True

        source = StreamingAudioSource()
        platform._audio_sources[1] = source

        # 480 samples of 24kHz mono (20ms) → gets converted to 48kHz stereo
        internal_data = b"\x00" * 960
        await platform.send_audio(internal_data)

        assert not source._buffer.empty()

    @pytest.mark.asyncio
    async def test_send_audio_multi_guild(self):
        """Audio is sent to all guilds' audio sources."""
        cfg = DiscordPlatformConfig()
        platform = DiscordPlatform(cfg)
        platform._active = True

        source1 = StreamingAudioSource()
        source2 = StreamingAudioSource()
        platform._audio_sources[1] = source1
        platform._audio_sources[2] = source2

        await platform.send_audio(b"\x00" * 960)

        assert not source1._buffer.empty()
        assert not source2._buffer.empty()

    @pytest.mark.asyncio
    async def test_answer_call_noop(self):
        cfg = DiscordPlatformConfig()
        platform = DiscordPlatform(cfg)
        await platform.answer_call()

    @pytest.mark.asyncio
    async def test_hang_up_when_no_connections(self):
        cfg = DiscordPlatformConfig()
        platform = DiscordPlatform(cfg)
        await platform.hang_up()
        assert len(platform._voice_clients) == 0
