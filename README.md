# VoiceKit

**Universal AI Voice Bridge** — a daemon that connects AI voice models to any voice/call platform.

```
┌─────────────────────────────────────────────────────────────┐
│                      VoiceKit Daemon                        │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│  ┌─────────────┐     ┌─────────────┐     ┌──────────────┐   │
│  │  Platform   │───▶│ Audio Router │───▶│  AI Provider │   │
│  │  Adapters   │◀───│   (Core)     │◀───│   Adapters   │   │
│  └─────────────┘     └─────────────┘     └──────────────┘   │
│                                                             │
│  Platforms:          Format:            Providers:          │
│  - Virtual Audio     - PCM 16-bit       - OpenAI Realtime   │
│  - Telegram          - 24kHz internal   - (future: others)  │
│  - Discord           - Mono                                 │
│  - WhatsApp          (auto-converts                         │
│  - Signal             to 48kHz stereo                       │
│  - Slack Huddles      for platforms)                        │
│  - Zoom                                                     │
│  - Phone (SIP)                                              │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

## Features

- **Pluggable AI providers** — starting with OpenAI Realtime API, extensible to any voice AI
- **Multi-platform** — virtual audio devices, Telegram, Discord, and more
- **Real-time** — low-latency bidirectional audio streaming via async I/O
- **Auto format conversion** — internal 24 kHz mono PCM automatically converts to 48 kHz stereo for Telegram/Discord
- **Cross-platform** — works on Windows, macOS, and Linux
- **Configurable** — YAML config with environment variable expansion
- **Extensible** — clean adapter pattern for adding new platforms and providers

## Quick Start

### Install

```bash
pip install voicekit
```

With platform support:

```bash
# Telegram voice calls
pip install "voicekit[telegram]"

# Discord voice channels
pip install "voicekit[discord]"

# Everything
pip install "voicekit[all]"
```

Or from source:

```bash
git clone https://github.com/voicekit/voicekit.git
cd voicekit
pip install -e ".[dev]"
```

### System Dependencies

Some platforms require system libraries:

```bash
# Telegram (needs ffmpeg)
apt install ffmpeg      # Debian/Ubuntu
brew install ffmpeg     # macOS

# Discord (needs libopus + ffmpeg)
apt install libopus0 ffmpeg    # Debian/Ubuntu
brew install opus ffmpeg       # macOS
```

### Configure

```bash
# Generate example config
voicekit init

# Edit config.yaml with your settings
# At minimum, set your OpenAI API key:
export OPENAI_API_KEY="sk-..."
```

### Run

```bash
# Start the daemon
voicekit start

# Start with a specific config file
voicekit start --config ./my-config.yaml

# List available audio devices
voicekit devices

# Test a platform
voicekit test virtual_audio
```

## Platform Setup

### Virtual Audio (Phase 1)

The simplest way to test. Bridges VoiceKit to any application via a virtual audio cable.

| OS | Software | Input Device | Output Device |
|----|----------|-------------|---------------|
| Windows | [VB-Cable](https://vb-audio.com/Cable/) | `VB-Cable Output` | `VB-Cable Input` |
| macOS | [BlackHole](https://existential.audio/blackhole/) | `BlackHole 2ch` | `BlackHole 2ch` |
| Linux | PulseAudio | Virtual sink | Virtual sink |

Linux virtual sink:
```bash
pactl load-module module-null-sink sink_name=VoiceKit sink_properties=device.description=VoiceKit
```

### Telegram (Phase 2)

Joins Telegram group voice chats as a userbot. Audio only (camera off for video calls).

**Requirements:**
- `pip install voicekit[telegram]` (installs pyrogram + py-tgcalls)
- ffmpeg installed on the system
- Telegram API credentials from [my.telegram.org](https://my.telegram.org)

**How it works:**
- Pyrogram handles the MTProto connection (user account, not bot)
- py-tgcalls manages the WebRTC voice layer
- Audio streams bidirectionally via named pipes (FIFOs)
- Format: 48 kHz mono PCM (auto-converted from internal 24 kHz)

```yaml
platforms:
  telegram:
    enabled: true
    api_id: ${TELEGRAM_API_ID}
    api_hash: ${TELEGRAM_API_HASH}
    phone_number: "+1234567890"
    session_name: voicekit
    auto_answer: true
```

### Discord (Phase 2)

Joins Discord voice channels as a bot. Captures user audio and plays AI responses.

**Requirements:**
- `pip install voicekit[discord]` (installs discord.py[voice] + discord-ext-voice-recv)
- libopus and ffmpeg installed on the system
- Discord bot token from the [Developer Portal](https://discord.com/developers/applications)

**Bot commands** (prefix configurable, default `!vk`):
- `!vk join [channel]` — join your voice channel or a named channel
- `!vk leave` — leave the voice channel
- `!vk status` — show connection info

**How it works:**
- discord.py handles the bot connection and voice channel management
- Custom `StreamingAudioSource` feeds AI audio as 20ms PCM frames
- `discord-ext-voice-recv` captures user audio via `BasicSink` callback
- Format: 48 kHz stereo PCM (auto-converted from internal 24 kHz mono)

```yaml
platforms:
  discord:
    enabled: true
    bot_token: ${DISCORD_BOT_TOKEN}
    auto_join_channels: ["123456789"]  # Channel IDs
    command_prefix: "!vk"
```

## Configuration

VoiceKit uses a YAML configuration file. Environment variables can be referenced as `${VAR_NAME}`.

```yaml
daemon:
  log_level: info

provider:
  type: openai_realtime
  api_key: ${OPENAI_API_KEY}
  voice: alloy
  instructions: |
    You are a helpful voice assistant.
  turn_detection:
    threshold: 0.5
    silence_duration_ms: 500

platforms:
  virtual_audio:
    enabled: true
    input_device: "VB-Cable Output"
    output_device: "VB-Cable Input"

  telegram:
    enabled: false
    api_id: ${TELEGRAM_API_ID}
    api_hash: ${TELEGRAM_API_HASH}
    auto_answer: true

  discord:
    enabled: false
    bot_token: ${DISCORD_BOT_TOKEN}
    command_prefix: "!vk"
```

See `config.example.yaml` for the full configuration reference.

## Architecture

### Audio Format Flow

```
Platform (48kHz stereo)  ←→  Audio Router  ←→  Internal (24kHz mono)  ←→  AI Provider
       Telegram                 auto-converts                              OpenAI Realtime
       Discord                  at boundaries
```

### Core Components

- **Audio Router** (`core/router.py`) — routes audio bidirectionally between platforms and providers with automatic format conversion
- **Audio Utilities** (`core/audio.py`) — PCM format conversion, resampling (linear interpolation), channel mixing, ring buffer
- **Event Bus** (`core/events.py`) — decoupled async event system for lifecycle and status events

### Provider Interface

```python
class VoiceProvider(ABC):
    async def connect(self) -> None: ...
    async def send_audio(self, audio: bytes) -> None: ...
    async def receive_audio(self) -> AsyncIterator[bytes]: ...
    async def disconnect(self) -> None: ...
```

### Platform Interface

```python
class PlatformAdapter(ABC):
    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    def on_audio_received(self, callback) -> None: ...
    async def send_audio(self, audio: bytes) -> None: ...
    async def answer_call(self) -> None: ...
```

### Adding a New Platform

1. Create a new file in `voicekit/platforms/`
2. Subclass `PlatformAdapter`
3. Implement the required methods
4. Add a config class in `config.py`
5. Register in `daemon.py`

### Adding a New Provider

1. Create a new file in `voicekit/providers/`
2. Subclass `VoiceProvider`
3. Implement the required methods
4. Add to the provider factory in `daemon.py`

## Project Structure

```
voicekit/
├── voicekit/
│   ├── __init__.py
│   ├── cli.py                 # CLI entry point (Typer)
│   ├── daemon.py              # Main daemon loop
│   ├── config.py              # Configuration schema (Pydantic)
│   ├── core/
│   │   ├── audio.py           # Audio buffer, format conversion
│   │   ├── router.py          # Routes audio between platform ↔ AI
│   │   └── events.py          # Event system
│   ├── providers/
│   │   ├── base.py            # Abstract provider class
│   │   └── openai_realtime.py # OpenAI Realtime API implementation
│   └── platforms/
│       ├── base.py            # Abstract platform class
│       ├── virtual_audio.py   # Virtual audio device adapter
│       ├── telegram.py        # Telegram voice (py-tgcalls + Pyrogram)
│       ├── discord.py         # Discord voice (discord.py + voice-recv)
│       ├── zoom.py            # Zoom meetings (stub)
│       ├── whatsapp.py        # WhatsApp Desktop (stub)
│       ├── signal.py          # Signal Desktop (stub)
│       ├── slack.py           # Slack Huddles (stub)
│       └── sip.py             # SIP/phone calls (stub)
├── tests/
├── config.example.yaml
├── pyproject.toml
├── README.md
└── LICENSE (MIT)
```

## Development

```bash
# Install in development mode with all extras
pip install -e ".[dev]"

# Run tests
pytest

# Lint
ruff check voicekit/

# Type check
mypy voicekit/
```

## Roadmap

| Phase | Status | Platforms |
|-------|--------|-----------|
| **Phase 1** | ✅ Done | Audio router, OpenAI Realtime, virtual audio device |
| **Phase 2** | ✅ Done | Telegram calls (py-tgcalls), Discord voice (discord.py) |
| **Phase 3** | Planned | WhatsApp, Signal, Slack Huddles |
| **Phase 4** | Planned | Zoom Bot SDK, SIP/phone, Microsoft Teams |

## License

MIT — see [LICENSE](LICENSE).
