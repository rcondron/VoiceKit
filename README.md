# VoiceBridge

**Universal AI Voice Bridge** — a daemon that connects AI voice models to any voice/call platform.

```
┌─────────────────────────────────────────────────────────────┐
│                     Voice Bridge Daemon                      │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│  ┌─────────────┐    ┌──────────────┐    ┌───────────────┐  │
│  │  Platform   │───▶│ Audio Router │───▶│  AI Provider  │  │
│  │  Adapters   │◀───│   (Core)     │◀───│   Adapters    │  │
│  └─────────────┘    └──────────────┘    └───────────────┘  │
│                                                             │
│  Platforms:          Format:            Providers:          │
│  - Virtual Audio     - PCM 16-bit       - OpenAI Realtime  │
│  - Telegram          - 24kHz            - (future: others) │
│  - Discord           - Mono                                 │
│  - WhatsApp                                                 │
│  - Signal                                                   │
│  - Slack Huddles                                           │
│  - Zoom                                                     │
│  - Phone (SIP)                                             │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

## Features

- **Pluggable AI providers** — starting with OpenAI Realtime API, extensible to any voice AI
- **Multi-platform** — virtual audio devices, messaging apps, conferencing, phone calls
- **Real-time** — low-latency bidirectional audio streaming via async I/O
- **Cross-platform** — works on Windows, macOS, and Linux
- **Configurable** — YAML config with environment variable expansion
- **Extensible** — clean adapter pattern for adding new platforms and providers

## Quick Start

### Install

```bash
pip install voicebridge
```

Or from source:

```bash
git clone https://github.com/voicebridge/voicebridge.git
cd voicebridge
pip install -e ".[dev]"
```

### Configure

```bash
# Generate example config
voicebridge init

# Edit config.yaml with your settings
# At minimum, set your OpenAI API key:
export OPENAI_API_KEY="sk-..."
```

### Run

```bash
# Start the daemon
voicebridge start

# Start with a specific config file
voicebridge start --config ./my-config.yaml

# List available audio devices
voicebridge devices

# Test a platform
voicebridge test virtual_audio
```

## Configuration

VoiceBridge uses a YAML configuration file. Environment variables can be referenced as `${VAR_NAME}`.

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
    allowed_users: ["+1234567890"]

  discord:
    enabled: false
    bot_token: ${DISCORD_BOT_TOKEN}
```

See `config.example.yaml` for the full configuration reference.

## Virtual Audio Setup

The virtual audio adapter is the simplest way to get started. It bridges VoiceBridge to any application via a virtual audio cable.

### Windows
Install [VB-Cable](https://vb-audio.com/Cable/):
- Input device: `VB-Cable Output`
- Output device: `VB-Cable Input`

### macOS
Install [BlackHole](https://existential.audio/blackhole/):
- Input device: `BlackHole 2ch`
- Output device: `BlackHole 2ch`

### Linux
Create a PulseAudio virtual sink:
```bash
pactl load-module module-null-sink sink_name=VoiceBridge sink_properties=device.description=VoiceBridge
```

## Architecture

### Core Components

- **Audio Router** (`core/router.py`) — routes audio bidirectionally between platforms and providers, handling format conversion and buffering
- **Audio Utilities** (`core/audio.py`) — PCM format conversion, resampling, channel mixing, ring buffer
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

1. Create a new file in `voicebridge/platforms/`
2. Subclass `PlatformAdapter`
3. Implement the required methods
4. Add a config class in `config.py`
5. Register in `daemon.py`

### Adding a New Provider

1. Create a new file in `voicebridge/providers/`
2. Subclass `VoiceProvider`
3. Implement the required methods
4. Add to the provider factory in `daemon.py`

## Project Structure

```
voicebridge/
├── voicebridge/
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
│       ├── telegram.py        # Telegram calls (stub)
│       ├── discord.py         # Discord voice (stub)
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
ruff check voicebridge/

# Type check
mypy voicebridge/
```

## Roadmap

| Phase | Status | Platforms |
|-------|--------|-----------|
| **Phase 1** | ✅ Core + Virtual Audio | Audio router, OpenAI Realtime, virtual audio device |
| **Phase 2** | 🔲 Messaging | Telegram calls, Discord voice |
| **Phase 3** | 🔲 Desktop Apps | WhatsApp, Signal, Slack Huddles |
| **Phase 4** | 🔲 Professional | Zoom Bot SDK, SIP/phone, Microsoft Teams |

## License

MIT — see [LICENSE](LICENSE).
