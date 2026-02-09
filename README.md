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
│  - Telegram          - 24kHz internal   - Google Gemini     │
│  - Discord           - Mono             - ElevenLabs        │
│  - WhatsApp          (auto-converts     - Deepgram          │
│  - Signal             at boundaries)    - Anthropic Claude   │
│  - Slack Huddles                        - Failover wrapper   │
│  - Zoom                                                     │
│  - Phone (SIP)       Middleware:                             │
│  - Microsoft Teams   - Echo cancel      Persistence:        │
│  - WebRTC Browser    - Noise gate       - Conversation      │
│  - Google Meet       - Recording          history (JSON)    │
│  - FaceTime          - Transcripts                          │
│                      - Rate limiting                        │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

## Features

- **Pluggable AI providers** — OpenAI Realtime, Google Gemini, ElevenLabs, Deepgram, Anthropic Claude (text-bridge), with multi-provider failover
- **12 platforms** — virtual audio, Telegram, Discord, WhatsApp, Signal, Slack, Zoom, SIP/phone, Teams, WebRTC, Google Meet, FaceTime
- **Audio middleware** — composable pipeline with echo cancellation, noise gate, recording, transcripts, rate limiting
- **Conversation persistence** — JSON file-based history with TTL and max-message limits
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

### WhatsApp Desktop (Phase 3)

Automates WhatsApp Desktop to answer voice calls. Uses PulseAudio virtual devices for audio routing and xdotool for call interaction.

**Requirements:**
- WhatsApp Desktop (Linux: snap, Flatpak, or .deb)
- PulseAudio or PipeWire (with `pulseaudio-utils`)
- `xdotool` for keyboard/window automation

**How it works:**
- PulseAudio virtual sinks capture and inject audio per-application
- xdotool monitors window titles for incoming call detection
- Auto-answers calls and routes audio bidirectionally
- Format: 48 kHz mono PCM (auto-converted from internal 24 kHz)

```yaml
platforms:
  whatsapp:
    enabled: true
    auto_answer: true
    allowed_contacts: ["Alice", "+15551234567"]
    process_name: "WhatsApp"
```

### Signal Desktop (Phase 3)

Automates Signal Desktop voice calls with dual detection: signal-cli (if installed) and window polling fallback.

**Requirements:**
- Signal Desktop
- PulseAudio or PipeWire (with `pulseaudio-utils`)
- `xdotool` for window automation
- `signal-cli` (optional, for enhanced call detection via D-Bus)

```yaml
platforms:
  signal:
    enabled: true
    auto_answer: true
    allowed_contacts: ["+15551234567"]
    signal_cli_path: "signal-cli"  # optional
    phone_number: "+15559876543"
```

### Slack Huddles (Phase 3)

Joins Slack Huddles via bot commands. Uses Slack Bolt with Socket Mode for API interactions and PulseAudio for audio routing.

**Requirements:**
- `pip install voicekit[slack]` (installs slack-bolt + slack-sdk)
- Slack Desktop (for huddle audio via PulseAudio bridge)
- PulseAudio or PipeWire

**Bot commands** (slash command, default `/voicekit`):
- `/voicekit join [#channel]` — join a huddle
- `/voicekit leave` — leave the current huddle
- `/voicekit status` — show connection info

```yaml
platforms:
  slack:
    enabled: true
    bot_token: ${SLACK_BOT_TOKEN}
    app_token: ${SLACK_APP_TOKEN}
    auto_join_channels: ["C0123456789"]
    command_prefix: "/voicekit"
```

### Zoom Meetings (Phase 4)

Joins Zoom meetings as a headless bot via the Zoom Meeting SDK.

**Requirements:**
- `pip install voicekit[zoom]` (installs zoom-meeting-sdk + aiohttp)
- Zoom Server-to-Server OAuth app from [marketplace.zoom.us](https://marketplace.zoom.us/)

```yaml
platforms:
  zoom:
    enabled: true
    client_id: ${ZOOM_CLIENT_ID}
    client_secret: ${ZOOM_CLIENT_SECRET}
    account_id: ${ZOOM_ACCOUNT_ID}
    meeting_id: "1234567890"
    meeting_passcode: "abc123"
    display_name: "VoiceKit AI"
    auto_join: true
```

### SIP / Phone (Phase 4)

Make and receive phone calls via any SIP provider (Twilio, Vonage, FreePBX, etc.).

**Requirements:**
- `pip install voicekit[sip]` (installs aiosip)
- SIP account credentials

**How it works:**
- SIP signalling via `aiosip` (REGISTER, INVITE, BYE)
- RTP audio transport with G.711 u-law (8 kHz) or L16 (16 kHz wideband)
- DTMF support via RFC 2833 telephone events
- Outbound calling via `make_call()` API

```yaml
platforms:
  sip:
    enabled: true
    server: "sip.provider.com"
    username: "your_sip_user"
    password: ${SIP_PASSWORD}
    port: 5060
    auto_answer: true
    allowed_numbers: ["+15551234567"]
```

### Microsoft Teams (Phase 5)

Join Microsoft Teams meetings as an AI bot via the Graph Communications API.

**Requirements:**
- `pip install voicekit[teams]` (installs aiohttp + msal)
- Azure Bot registration with Communications API permissions
- Azure AD app with `Calls.JoinGroupCall.All` and `Calls.InitiateGroupCall.All`

**Limitations:**
- Requires Azure Bot Framework registration (complex setup)
- The Communications API media session requires a publicly-accessible callback URL

```yaml
platforms:
  teams:
    enabled: true
    client_id: ${TEAMS_CLIENT_ID}
    client_secret: ${TEAMS_CLIENT_SECRET}
    tenant_id: ${TEAMS_TENANT_ID}
    meeting_url: "https://teams.microsoft.com/l/meetup-join/..."
    callback_url: "https://your-server.com/callback"
    auto_join: true
```

### WebRTC Browser (Phase 5)

Generic WebRTC platform with a built-in signalling server. Browsers connect directly — no third-party dependencies.

**Requirements:**
- `pip install voicekit[webrtc]` (installs aiortc + aiohttp)

**How it works:**
- Built-in aiohttp signalling server with WebSocket SDP/ICE exchange
- aiortc handles the WebRTC peer connection server-side
- Includes a test HTML page at `/` for browser-based testing
- Configurable STUN servers for NAT traversal

```yaml
platforms:
  webrtc:
    enabled: true
    host: "0.0.0.0"
    port: 8080
    stun_servers: ["stun:stun.l.google.com:19302"]
```

### Google Meet (Phase 5)

Joins Google Meet calls via Playwright browser automation.

**Requirements:**
- `pip install voicekit[meet]` (installs playwright)
- `playwright install chromium`
- PulseAudio or PipeWire for audio routing

**Limitations:**
- Fragile — relies on Meet's DOM structure which may change without notice
- Requires a Google account session (cookies or login)
- No official Meet bot API exists; this is a best-effort automation

```yaml
platforms:
  google_meet:
    enabled: true
    meeting_url: "https://meet.google.com/abc-defg-hij"
    headless: true
    pulse_sink_name: "voicekit_meet"
```

### FaceTime (Phase 5)

Answer FaceTime calls on macOS via AppleScript automation.

**Requirements:**
- macOS only
- BlackHole or similar virtual audio driver (`brew install blackhole-2ch`)
- Accessibility permissions for AppleScript automation

**Limitations:**
- macOS only — FaceTime has no cross-platform API
- Cannot initiate outbound calls (Apple provides no API for this)
- Call detection relies on polling FaceTime window titles
- UI automation may break across macOS versions

```yaml
platforms:
  facetime:
    enabled: true
    auto_answer: true
    allowed_contacts: ["Alice", "+15551234567"]
    virtual_device_name: "BlackHole 2ch"
    poll_interval: 1.0
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
│   │   ├── audio.py           # Audio buffer, format conversion, resampling
│   │   ├── router.py          # Routes audio between platform ↔ AI
│   │   ├── events.py          # Event system
│   │   ├── middleware.py       # Audio middleware pipeline (AEC, noise gate, etc.)
│   │   ├── persistence.py     # Conversation history persistence
│   │   └── pulse_bridge.py    # PulseAudio per-app routing (desktop)
│   ├── providers/
│   │   ├── base.py            # Abstract provider class
│   │   ├── openai_realtime.py # OpenAI Realtime API
│   │   ├── google_gemini.py   # Google Gemini Multimodal Live API
│   │   ├── elevenlabs.py      # ElevenLabs Conversational AI
│   │   ├── deepgram.py        # Deepgram Voice Agent API
│   │   ├── anthropic_claude.py# Anthropic Claude (text-bridge: STT→LLM→TTS)
│   │   └── failover.py        # Multi-provider failover wrapper
│   └── platforms/
│       ├── base.py            # Abstract platform class
│       ├── virtual_audio.py   # Virtual audio device adapter
│       ├── telegram.py        # Telegram voice (py-tgcalls + Pyrogram)
│       ├── discord.py         # Discord voice (discord.py + voice-recv)
│       ├── whatsapp.py        # WhatsApp Desktop (PulseAudio + xdotool)
│       ├── signal.py          # Signal Desktop (PulseAudio + signal-cli)
│       ├── slack.py           # Slack Huddles (Bolt + PulseAudio)
│       ├── zoom.py            # Zoom meetings (Meeting SDK)
│       ├── sip.py             # SIP/phone calls (aiosip + RTP)
│       ├── teams.py           # Microsoft Teams (Graph API)
│       ├── webrtc.py          # Browser WebRTC (generic)
│       ├── google_meet.py     # Google Meet (Playwright automation)
│       └── facetime.py        # FaceTime (macOS AppleScript)
├── tests/
├── Dockerfile
├── docker-compose.yml
├── .github/
│   └── workflows/
│       └── ci.yml
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

| Phase | Status | Scope |
|-------|--------|-------|
| **Phase 1** | ✅ Done | Audio router, OpenAI Realtime, virtual audio device |
| **Phase 2** | ✅ Done | Telegram calls (py-tgcalls), Discord voice (discord.py) |
| **Phase 3** | ✅ Done | WhatsApp Desktop, Signal Desktop, Slack Huddles |
| **Phase 4** | ✅ Done | Zoom Meeting SDK, SIP/phone (aiosip + RTP) |
| **Phase 5** | ✅ Done | Google Meet, FaceTime, Teams, WebRTC, Gemini/ElevenLabs/Deepgram/Claude providers, failover, middleware pipeline, persistence, Docker, CI/CD |

## Future Development

### Near-term

| Area | Description | Priority |
|------|-------------|----------|
| **Native Anthropic Voice** | Replace the text-bridge (STT→Claude→TTS) provider with Anthropic's native voice API when it ships. The current `AnthropicClaudeProvider` is a placeholder that pipelines through external STT/TTS services — latency is higher than native solutions. | High |
| **Adaptive Echo Cancellation** | The current AEC uses basic cross-correlation. Integrate WebRTC AEC3 (via `webrtc-audio-processing` bindings) or `speexdsp` for real acoustic environments with hardware speakers. | Medium |
| **SQLite / Redis Persistence** | The JSON-file conversation store works for single-instance deployments. For multi-instance or high-volume use, add SQLite (local) and Redis (distributed) storage backends. | Medium |
| **Google Meet DOM Hardening** | The Playwright-based Meet adapter is fragile. Consider a MutationObserver-based approach or Puppeteer CDP hooks to detect DOM changes and auto-adapt selectors. | Medium |
| **SIP TLS + SRTP** | The SIP adapter supports UDP only. Add TLS transport for SIP signalling and SRTP for encrypted media (required by many enterprise PBX systems). | Medium |

### Medium-term

| Area | Description | Priority |
|------|-------------|----------|
| **Multi-language STT/TTS** | Add configurable STT/TTS engines per provider (Whisper, Deepgram, Azure Speech, Google STT). Currently only the Claude text-bridge needs external STT/TTS. | Medium |
| **Web Dashboard** | A lightweight web UI for monitoring active calls, viewing transcripts, controlling the daemon, and managing configuration. Could extend the existing health check server. | Low |
| **Hot Reload** | Watch `config.yaml` for changes and dynamically add/remove platforms and providers without restarting the daemon. The event bus architecture already supports this. | Low |
| **Audio Streaming Analytics** | Track per-route metrics (latency, jitter, packet loss, audio levels) and expose them via the health endpoint for monitoring dashboards (Grafana, Datadog). | Low |
| **Plugin System** | Allow third-party middleware and platform adapters to be installed as Python packages and discovered via entry points (`voicekit.plugins`). | Low |

### Long-term Vision

| Area | Description |
|------|-------------|
| **Multi-party Routing** | Support N-way calls where VoiceKit bridges multiple participants (e.g., conference calls with per-speaker AI processing). Requires audio mixing and speaker diarization. |
| **Voice Cloning / Custom Voices** | Integrate voice cloning APIs (ElevenLabs, Resemble) so the AI can respond in a specific voice profile. Useful for branded voice assistants. |
| **Real-time Translation** | Bidirectional translation pipeline: STT in language A → translate → TTS in language B. Could be implemented as middleware. |
| **Edge Deployment** | Optimize for edge devices (Raspberry Pi, Jetson Nano) with quantized models and reduced memory footprint. Some providers (Whisper.cpp, Piper TTS) can run fully local. |
| **Function Calling / Tool Use** | Bridge AI provider tool/function calls to external APIs during voice conversations — e.g., check calendar, look up weather, create tickets. OpenAI Realtime already supports this; wire it through the event bus. |

### Known Limitations

- **Google Meet / FaceTime**: These platforms have no official bot APIs. The adapters rely on UI automation (Playwright for Meet, AppleScript for FaceTime) which is inherently fragile across app updates.
- **WhatsApp / Signal / Slack**: Desktop-based adapters require a running desktop app instance with PulseAudio. They cannot run in headless server environments without a virtual display (Xvfb).
- **Anthropic Claude Provider**: Currently a text-bridge (STT→text→TTS) with higher latency than native voice providers. Will be replaced when a native voice API becomes available.
- **Echo Cancellation**: The built-in AEC is a lightweight cross-correlation approach suitable for virtual audio setups. For real microphone+speaker scenarios, integrate a dedicated AEC library.
- **Conversation Persistence**: JSON file storage is single-process only. Use caution with concurrent access from multiple daemon instances.

## License

MIT — see [LICENSE](LICENSE).
