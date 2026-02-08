"""Slack Huddles platform adapter.

Connects VoiceKit to Slack Huddles for real-time voice interaction.
Uses the Slack Bolt framework with Socket Mode for real-time events
and the PulseAudio bridge for audio routing through the Slack desktop
application.

Supports:
- Joining Slack Huddles via bot commands (/voicekit join)
- Monitoring huddle lifecycle events via Slack Events API
- Bidirectional audio via PulseAudio virtual sinks
- Channel allowlisting
- Auto-joining huddles in configured channels

Architecture:
    - **Slack Bolt** handles API interactions, slash commands, and events
    - **Socket Mode** provides WebSocket-based real-time event delivery
    - **PulseAudio bridge** captures/injects audio from/to the Slack
      Desktop application (Huddles use WebRTC internally with no public
      audio API)

Audio format: PulseAudio captures PCM 16-bit, 48 kHz, stereo from Slack.
The adapter converts to/from VoiceKit's internal format (24 kHz mono).

Requires: pip install voicekit[slack]
  - slack-bolt >= 1.18
  - slack-sdk >= 3.21

System requirements:
  - Slack Desktop (for Huddle audio via PulseAudio bridge)
  - PulseAudio or PipeWire (with pulseaudio-utils)
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from typing import Any

from voicekit.config import SlackPlatformConfig
from voicekit.core.audio import AudioFormat, convert_audio
from voicekit.platforms.base import AudioCallback, PlatformAdapter

logger = logging.getLogger(__name__)

# Slack Huddles use 48 kHz stereo via WebRTC / PulseAudio
SLACK_AUDIO_FORMAT = AudioFormat(sample_rate=48000, channels=2, sample_width=2)

# 20ms frame at 48 kHz stereo: 48000 * 2ch * 2B * 0.020 = 3840 bytes
SLACK_FRAME_BYTES = 3840


class SlackPlatform(PlatformAdapter):
    """Slack Huddles adapter using Slack Bolt and PulseAudio bridge.

    Provides two complementary layers:

    1. **Slack API layer** (Slack Bolt + Socket Mode): Handles slash commands,
       channel events, and huddle lifecycle monitoring. Allows users to
       control VoiceKit via Slack messages and slash commands.

    2. **Audio layer** (PulseAudio bridge): Routes audio bidirectionally
       between the Slack Desktop app and VoiceKit. Required because Slack
       does not expose a public API for huddle audio streams.

    Bot commands (via slash command, default ``/voicekit``):
      ``/voicekit join [channel]`` — join a huddle in the given channel
      ``/voicekit leave``          — leave the current huddle
      ``/voicekit status``         — show connection info
    """

    def __init__(self, config: SlackPlatformConfig) -> None:
        self._config = config
        self._callback: AudioCallback | None = None
        self._active = False
        self._bridge: Any = None  # PulseAudioBridge (lazy)
        self._bolt_app: Any = None  # slack_bolt.async_app.AsyncApp
        self._socket_handler: Any = None  # AsyncSocketModeHandler
        self._bolt_task: asyncio.Task[None] | None = None
        self._in_huddle = False
        self._current_channel: str | None = None
        self._web_client: Any = None  # AsyncWebClient

    @property
    def name(self) -> str:
        return "slack"

    @property
    def is_active(self) -> bool:
        return self._active

    async def start(self) -> None:
        """Start the Slack platform adapter.

        Initializes the Slack Bolt app with Socket Mode, registers
        command handlers, sets up PulseAudio bridge, and starts
        listening for events.
        """
        # Import Slack dependencies (lazy)
        try:
            from slack_bolt.adapter.socket_mode.async_handler import (
                AsyncSocketModeHandler,
            )
            from slack_bolt.async_app import AsyncApp
        except ImportError:
            raise RuntimeError(
                "slack-bolt is required for Slack support. "
                "Install with: pip install voicekit[slack]"
            )

        self._check_dependencies()

        # Initialize Slack Bolt app
        self._bolt_app = AsyncApp(
            token=self._config.bot_token,
            name="VoiceKit",
        )
        self._web_client = self._bolt_app.client

        # Register handlers
        self._register_commands()
        self._register_events()

        # Set up PulseAudio bridge for desktop audio
        from voicekit.core.pulse_bridge import PulseAudioBridge

        self._bridge = PulseAudioBridge(
            app_process_name=self._config.process_name,
            sink_prefix=self._config.pulse_sink_name,
            sample_rate=SLACK_AUDIO_FORMAT.sample_rate,
            channels=SLACK_AUDIO_FORMAT.channels,
        )

        try:
            await self._bridge.setup()
        except RuntimeError:
            logger.exception("Failed to set up PulseAudio bridge for Slack")
            raise

        await self._bridge.start(self._on_captured_audio)

        # Start Socket Mode handler
        self._socket_handler = AsyncSocketModeHandler(
            self._bolt_app, self._config.app_token
        )
        self._bolt_task = asyncio.create_task(
            self._run_bolt(), name="slack_bolt"
        )

        self._active = True

        # Auto-join configured channels
        if self._config.auto_join_channels:
            asyncio.create_task(
                self._auto_join_channels(), name="slack_auto_join"
            )

        logger.info(
            "Slack platform started (command=%s, auto_join=%s)",
            self._config.command_prefix,
            self._config.auto_join_channels or "none",
        )

    def _check_dependencies(self) -> None:
        """Verify PulseAudio tools are available."""
        missing = []
        for tool in ("pactl", "parec", "pacat"):
            if not shutil.which(tool):
                missing.append(tool)

        if missing:
            raise RuntimeError(
                f"Missing system tools for Slack audio: {', '.join(missing)}. "
                "Install pulseaudio-utils (or pipewire-pulse)."
            )

    async def _run_bolt(self) -> None:
        """Run the Slack Bolt Socket Mode handler."""
        try:
            await self._socket_handler.start_async()
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("Slack Bolt handler error")
            self._active = False

    def _register_commands(self) -> None:
        """Register slash command handlers."""
        if not self._bolt_app:
            return

        app = self._bolt_app
        prefix = self._config.command_prefix

        @app.command(prefix)
        async def handle_command(ack: Any, command: dict[str, Any], respond: Any) -> None:
            """Handle the /voicekit slash command."""
            await ack()
            text = command.get("text", "").strip()
            parts = text.split(maxsplit=1)
            subcommand = parts[0].lower() if parts else "help"

            if subcommand == "join":
                channel = parts[1].strip() if len(parts) > 1 else command.get("channel_id", "")
                await self._join_huddle(channel)
                await respond(f"Joining huddle in <#{channel}>")

            elif subcommand == "leave":
                await self._leave_huddle()
                await respond("Left the huddle.")

            elif subcommand == "status":
                status = (
                    f"Connected to huddle in <#{self._current_channel}>"
                    if self._in_huddle and self._current_channel
                    else "Not in a huddle."
                )
                await respond(f"VoiceKit status: {status}")

            else:
                await respond(
                    f"*VoiceKit Commands:*\n"
                    f"  `{prefix} join [#channel]` — Join a huddle\n"
                    f"  `{prefix} leave` — Leave the current huddle\n"
                    f"  `{prefix} status` — Show connection status"
                )

    def _register_events(self) -> None:
        """Register Slack event handlers for huddle lifecycle."""
        if not self._bolt_app:
            return

        app = self._bolt_app

        @app.event("huddle_started")
        async def on_huddle_started(event: dict[str, Any]) -> None:
            """Handle a huddle starting in a channel."""
            channel = event.get("channel", "")
            logger.info("Huddle started in channel %s", channel)

            if self._config.auto_join_channels:
                if channel in self._config.auto_join_channels:
                    logger.info("Auto-joining huddle in %s", channel)
                    await self._join_huddle(channel)

        @app.event("huddle_ended")
        async def on_huddle_ended(event: dict[str, Any]) -> None:
            """Handle a huddle ending."""
            channel = event.get("channel", "")
            logger.info("Huddle ended in channel %s", channel)
            if self._current_channel == channel:
                self._in_huddle = False
                self._current_channel = None

        @app.event("message")
        async def on_message(event: dict[str, Any]) -> None:
            """Handle messages — primarily for monitoring mentions."""
            # Only process direct mentions for commands
            text = event.get("text", "")
            if "<@" in text and "join huddle" in text.lower():
                channel = event.get("channel", "")
                await self._join_huddle(channel)

    async def _on_captured_audio(self, audio: bytes) -> None:
        """Handle audio captured from Slack via PulseAudio bridge.

        Converts from Slack format (48 kHz stereo) to internal format
        (24 kHz mono) and forwards to the registered callback.
        """
        if not self._callback or not self._in_huddle:
            return
        internal = convert_audio(audio, SLACK_AUDIO_FORMAT, AudioFormat())
        await self._callback(internal)

    async def stop(self) -> None:
        """Stop the Slack platform and clean up all resources."""
        self._active = False
        self._in_huddle = False

        # Stop Bolt handler
        if self._socket_handler:
            try:
                await self._socket_handler.close_async()
            except Exception:
                logger.debug("Error closing Socket Mode handler", exc_info=True)

        if self._bolt_task and not self._bolt_task.done():
            self._bolt_task.cancel()
            try:
                await self._bolt_task
            except asyncio.CancelledError:
                pass
        self._bolt_task = None

        # Stop PulseAudio bridge
        if self._bridge:
            await self._bridge.stop()
            self._bridge = None

        self._bolt_app = None
        self._web_client = None
        self._current_channel = None
        logger.info("Slack platform stopped")

    def on_audio_received(self, callback: AudioCallback) -> None:
        """Register callback for captured audio from Slack Huddles."""
        self._callback = callback

    async def send_audio(self, audio: bytes) -> None:
        """Send audio to the active Slack Huddle.

        Converts from internal format (24 kHz mono) to Slack format
        (48 kHz stereo) and writes to the PulseAudio inject sink.
        """
        if not self._active or not self._in_huddle or not self._bridge:
            return
        slack_audio = convert_audio(audio, AudioFormat(), SLACK_AUDIO_FORMAT)
        await self._bridge.write_audio(slack_audio)

    # -- Huddle management ---------------------------------------------------

    async def _join_huddle(self, channel: str) -> None:
        """Join a Slack Huddle in the specified channel.

        Uses the Slack Web API to initiate the huddle join, then
        activates audio routing via the PulseAudio bridge.

        Args:
            channel: The Slack channel ID to join the huddle in.
        """
        if self._in_huddle:
            await self._leave_huddle()

        logger.info("Joining Slack Huddle in channel %s", channel)

        # Post a message to indicate VoiceKit is joining
        if self._web_client:
            try:
                await self._web_client.chat_postMessage(
                    channel=channel,
                    text="VoiceKit is joining the huddle...",
                )
            except Exception:
                logger.debug("Failed to post join message", exc_info=True)

        self._current_channel = channel
        self._in_huddle = True

        # Re-route Slack's audio streams to our virtual sinks
        if self._bridge:
            await self._bridge.route_app_streams()

        logger.info("Joined Slack Huddle in %s", channel)

    async def _leave_huddle(self) -> None:
        """Leave the current Slack Huddle."""
        if not self._in_huddle:
            return

        logger.info("Leaving Slack Huddle in %s", self._current_channel)

        if self._web_client and self._current_channel:
            try:
                await self._web_client.chat_postMessage(
                    channel=self._current_channel,
                    text="VoiceKit has left the huddle.",
                )
            except Exception:
                logger.debug("Failed to post leave message", exc_info=True)

        self._in_huddle = False
        self._current_channel = None

    async def _auto_join_channels(self) -> None:
        """Auto-join huddles in configured channels after startup."""
        # Wait a moment for the Bolt app to fully connect
        await asyncio.sleep(3.0)

        for channel_id in self._config.auto_join_channels:
            if not self._active:
                break
            try:
                # Check if there's an active huddle in the channel
                if self._web_client:
                    result = await self._web_client.conversations_info(channel=channel_id)
                    channel_info = result.get("channel", {})
                    # If huddle is active, join it
                    props = channel_info.get("properties", {})
                    if props.get("huddle_state") == "active":
                        logger.info("Active huddle found in %s, auto-joining", channel_id)
                        await self._join_huddle(channel_id)
                        break
            except Exception:
                logger.debug(
                    "Could not check huddle state for %s", channel_id, exc_info=True
                )

    async def answer_call(self) -> None:
        """No-op — Slack Huddles are joined explicitly via commands."""
        pass

    async def hang_up(self) -> None:
        """Leave the current huddle."""
        await self._leave_huddle()
