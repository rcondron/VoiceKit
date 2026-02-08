"""Discord voice channel platform adapter.

Connects VoiceKit to Discord voice channels using discord.py for the bot
framework and discord-ext-voice-recv for capturing user audio from voice
channels.

Supports:
- Joining voice channels via bot commands (!vk join / !vk leave)
- Auto-joining configured channels on startup
- Capturing all user audio from voice channels
- Playing AI responses back into voice channels
- Stage channels / video channels (camera off, audio only)

Audio format: Discord uses PCM 16-bit, 48 kHz, stereo.
The adapter converts to/from VoiceKit's internal format (24 kHz mono).

Requires: pip install voicekit[discord]
  - discord.py[voice] >= 2.3
  - discord-ext-voice-recv >= 0.4  (optional, for capturing user audio)
  - libopus and ffmpeg must be installed on the system
"""

from __future__ import annotations

import asyncio
import logging
import queue
from typing import Any

from voicekit.config import DiscordPlatformConfig
from voicekit.core.audio import AudioFormat, convert_audio
from voicekit.platforms.base import AudioCallback, PlatformAdapter

logger = logging.getLogger(__name__)

# Discord audio format: PCM s16le, 48 kHz, stereo
DISCORD_AUDIO_FORMAT = AudioFormat(sample_rate=48000, channels=2, sample_width=2)

# 20ms frame at 48 kHz stereo: 48000 * 2 bytes * 2 ch * 0.020 = 3840 bytes
DISCORD_FRAME_BYTES = 3840


class StreamingAudioSource:
    """Custom AudioSource that streams PCM from a thread-safe queue.

    At runtime, when discord.py is available, this is used as a
    discord.AudioSource. The ``read()`` method is called by discord.py
    in a dedicated audio thread every 20ms. We feed it 3840-byte frames
    (20ms of 48 kHz stereo 16-bit PCM) from a queue.
    """

    def __init__(self) -> None:
        self._buffer: queue.Queue[bytes] = queue.Queue(maxsize=250)
        self._finished = False

    def read(self) -> bytes:
        """Return next 3840-byte PCM frame.

        Called by discord.py in the audio playback thread.
        Returns silence when the buffer is empty.
        """
        try:
            return self._buffer.get(timeout=0.04)
        except queue.Empty:
            if self._finished:
                return b""
            return b"\x00" * DISCORD_FRAME_BYTES

    def is_opus(self) -> bool:
        return False

    def cleanup(self) -> None:
        self._finished = True

    def feed(self, data: bytes) -> None:
        """Push a 3840-byte PCM frame into the buffer.

        Thread-safe. Drops oldest frame if full to prevent latency buildup.
        """
        try:
            self._buffer.put_nowait(data)
        except queue.Full:
            try:
                self._buffer.get_nowait()
            except queue.Empty:
                pass
            try:
                self._buffer.put_nowait(data)
            except queue.Full:
                pass


class DiscordPlatform(PlatformAdapter):
    """Discord voice channel adapter.

    Uses discord.py with the commands extension for the bot framework,
    and discord-ext-voice-recv for capturing user audio. AI responses
    are played back via a custom StreamingAudioSource.

    Bot commands (prefix configurable, default ``!vk``):
      ``!vk join [channel]`` — join your current or named voice channel
      ``!vk leave``          — leave the voice channel
      ``!vk status``         — show connection info
    """

    def __init__(self, config: DiscordPlatformConfig) -> None:
        self._config = config
        self._callback: AudioCallback | None = None
        self._active = False
        self._bot: Any = None  # commands.Bot
        self._audio_sources: dict[int, StreamingAudioSource] = {}  # guild_id → source
        self._voice_clients: dict[int, Any] = {}  # guild_id → VoiceClient
        self._loop: asyncio.AbstractEventLoop | None = None
        self._bot_task: asyncio.Task[None] | None = None
        self._has_voice_recv = False

    @property
    def name(self) -> str:
        return "discord"

    @property
    def is_active(self) -> bool:
        return self._active

    async def start(self) -> None:
        """Start the Discord bot and prepare for voice connections."""
        try:
            import discord
            from discord.ext import commands
        except ImportError:
            raise RuntimeError(
                "discord.py[voice] is required for Discord support. "
                "Install with: pip install voicekit[discord]"
            )

        # Check for voice receive support (optional)
        try:
            from discord.ext import voice_recv  # noqa: F401

            self._has_voice_recv = True
            logger.info("discord-ext-voice-recv available — user audio capture enabled")
        except ImportError:
            logger.warning(
                "discord-ext-voice-recv not installed. "
                "Voice capture (user → AI) will be disabled. "
                "Install with: pip install discord-ext-voice-recv"
            )

        self._loop = asyncio.get_running_loop()

        # Configure intents
        intents = discord.Intents.default()
        intents.message_content = True
        intents.voice_states = True
        intents.guilds = True

        # Create bot with command prefix (e.g. "!vk join" → prefix is "!vk ")
        self._bot = commands.Bot(
            command_prefix=self._config.command_prefix + " ",
            intents=intents,
        )

        self._register_commands()
        self._register_events()

        # Start bot in background
        self._bot_task = asyncio.create_task(self._run_bot(), name="discord_bot")
        self._active = True

        logger.info(
            "Discord platform starting (prefix='%s', auto_join=%s)",
            self._config.command_prefix,
            self._config.auto_join_channels,
        )

    async def _run_bot(self) -> None:
        """Run the Discord bot (blocks until closed)."""
        import discord as _discord

        if not self._bot:
            return
        try:
            await self._bot.start(self._config.bot_token)
        except _discord.LoginFailure:
            logger.error("Discord login failed — check your bot token")
            self._active = False
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("Discord bot error")
            self._active = False

    def _register_events(self) -> None:
        """Register Discord lifecycle events."""
        import discord as _discord

        if not self._bot:
            return

        bot = self._bot

        @bot.event
        async def on_ready() -> None:
            assert bot.user is not None
            logger.info("Discord bot ready: %s (id=%s)", bot.user, bot.user.id)

            # Auto-join configured channels
            for ch_id_str in self._config.auto_join_channels:
                try:
                    ch = bot.get_channel(int(ch_id_str))
                    if isinstance(ch, (_discord.VoiceChannel, _discord.StageChannel)):
                        await self._join_channel(ch)
                    else:
                        logger.warning(
                            "Auto-join: channel %s not found or not a voice channel", ch_id_str
                        )
                except Exception as exc:
                    logger.error("Auto-join failed for %s: %s", ch_id_str, exc)

    def _register_commands(self) -> None:
        """Register bot slash/prefix commands for voice management."""
        import discord as _discord
        from discord.ext import commands as _commands

        if not self._bot:
            return

        bot = self._bot

        @bot.command(name="join")
        async def cmd_join(ctx: _commands.Context, *, channel_name: str | None = None) -> None:
            """Join a voice channel."""
            if channel_name:
                channel = (
                    _discord.utils.get(ctx.guild.voice_channels, name=channel_name)
                    if ctx.guild
                    else None
                )
                if not channel:
                    await ctx.send(f"Voice channel '{channel_name}' not found.")
                    return
            else:
                author = ctx.author
                voice_state = getattr(author, "voice", None)
                if voice_state and voice_state.channel:
                    channel = voice_state.channel
                else:
                    await ctx.send("Join a voice channel first, or specify a channel name.")
                    return

            await self._join_channel(channel)
            await ctx.send(f"Joined **{channel.name}**")

        @bot.command(name="leave")
        async def cmd_leave(ctx: _commands.Context) -> None:
            """Leave the current voice channel."""
            if ctx.guild and ctx.guild.id in self._voice_clients:
                vc = self._voice_clients.pop(ctx.guild.id)
                self._audio_sources.pop(ctx.guild.id, None)
                if vc.is_playing():
                    vc.stop()
                await vc.disconnect()
                await ctx.send("Left voice channel.")
            else:
                await ctx.send("Not in a voice channel.")

        @bot.command(name="status")
        async def cmd_status(ctx: _commands.Context) -> None:
            """Show voice connection status."""
            if ctx.guild and ctx.guild.id in self._voice_clients:
                vc = self._voice_clients[ctx.guild.id]
                ch_name = vc.channel.name if vc.channel else "unknown"
                await ctx.send(
                    f"Connected to **{ch_name}** "
                    f"(playing={'yes' if vc.is_playing() else 'no'}, "
                    f"capture={'yes' if self._has_voice_recv else 'no'})"
                )
            else:
                await ctx.send("Not connected to any voice channel.")

    async def _join_channel(self, channel: Any) -> None:
        """Join a voice/stage channel and set up bidirectional audio."""
        guild_id = channel.guild.id

        # Disconnect existing connection in this guild
        if guild_id in self._voice_clients:
            old_vc = self._voice_clients.pop(guild_id)
            self._audio_sources.pop(guild_id, None)
            if old_vc.is_playing():
                old_vc.stop()
            await old_vc.disconnect()

        # Connect — use VoiceRecvClient if capture is available
        if self._has_voice_recv:
            from discord.ext import voice_recv

            vc = await channel.connect(cls=voice_recv.VoiceRecvClient)

            # Set up audio capture callback
            def _on_audio(user: Any, data: Any) -> None:
                if not self._callback or not self._loop or not self._active:
                    return
                pcm = getattr(data, "pcm", None)
                if not pcm:
                    return
                # Convert from Discord (48 kHz stereo) to internal (24 kHz mono)
                internal = convert_audio(pcm, DISCORD_AUDIO_FORMAT, AudioFormat())
                asyncio.run_coroutine_threadsafe(self._callback(internal), self._loop)

            vc.listen(voice_recv.BasicSink(_on_audio))  # type: ignore[attr-defined]
        else:
            vc = await channel.connect()

        self._voice_clients[guild_id] = vc

        # Start playing AI audio through a StreamingAudioSource
        source = StreamingAudioSource()
        self._audio_sources[guild_id] = source
        vc.play(source)

        logger.info(
            "Joined Discord channel: %s (guild=%s, capture=%s)",
            channel.name,
            channel.guild.name,
            self._has_voice_recv,
        )

    async def stop(self) -> None:
        """Disconnect from all voice channels and shut down the bot."""
        self._active = False

        # Disconnect from all voice channels
        for guild_id, vc in list(self._voice_clients.items()):
            try:
                if vc.is_playing():
                    vc.stop()
                await vc.disconnect()
            except Exception:
                logger.debug("Error disconnecting from guild %s", guild_id, exc_info=True)
        self._voice_clients.clear()

        # Clean up audio sources
        for source in self._audio_sources.values():
            source.cleanup()
        self._audio_sources.clear()

        # Shut down the bot
        if self._bot:
            try:
                await self._bot.close()
            except Exception:
                logger.debug("Error closing Discord bot", exc_info=True)

        if self._bot_task and not self._bot_task.done():
            self._bot_task.cancel()
            try:
                await self._bot_task
            except asyncio.CancelledError:
                pass

        self._bot = None
        logger.info("Discord platform stopped")

    def on_audio_received(self, callback: AudioCallback) -> None:
        """Register callback for captured audio from Discord voice channels."""
        self._callback = callback

    async def send_audio(self, audio: bytes) -> None:
        """Send audio to all active Discord voice channels.

        Converts from internal format (24 kHz mono) to Discord format
        (48 kHz stereo) and feeds 20ms frames to the audio sources.
        """
        if not self._active or not self._audio_sources:
            return

        # Convert internal → Discord format
        discord_audio = convert_audio(audio, AudioFormat(), DISCORD_AUDIO_FORMAT)

        # Feed as 3840-byte frames (20ms at 48 kHz stereo)
        offset = 0
        while offset < len(discord_audio):
            frame = discord_audio[offset : offset + DISCORD_FRAME_BYTES]
            if len(frame) < DISCORD_FRAME_BYTES:
                frame += b"\x00" * (DISCORD_FRAME_BYTES - len(frame))
            for source in self._audio_sources.values():
                source.feed(frame)
            offset += DISCORD_FRAME_BYTES

    async def answer_call(self) -> None:
        """No-op — Discord channels are joined explicitly via commands."""
        pass

    async def hang_up(self) -> None:
        """Leave all voice channels."""
        for guild_id, vc in list(self._voice_clients.items()):
            try:
                if vc.is_playing():
                    vc.stop()
                await vc.disconnect()
            except Exception:
                logger.debug("Error disconnecting from guild %s", guild_id, exc_info=True)
        self._voice_clients.clear()
        for source in self._audio_sources.values():
            source.cleanup()
        self._audio_sources.clear()
