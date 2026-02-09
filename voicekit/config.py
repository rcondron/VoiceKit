"""Configuration schema for VoiceKit.

Loads configuration from YAML files with environment variable expansion
and validates using Pydantic models.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field


def _expand_env_vars(value: Any) -> Any:
    """Recursively expand ${VAR} references in strings."""
    if isinstance(value, str):
        pattern = re.compile(r"\$\{([^}]+)\}")
        def replacer(match: re.Match[str]) -> str:
            var_name = match.group(1)
            return os.environ.get(var_name, match.group(0))
        return pattern.sub(replacer, value)
    elif isinstance(value, dict):
        return {k: _expand_env_vars(v) for k, v in value.items()}
    elif isinstance(value, list):
        return [_expand_env_vars(item) for item in value]
    return value


# --- Daemon ---


class DaemonConfig(BaseModel):
    """Top-level daemon settings."""

    log_level: str = "info"
    log_file: str | None = None
    health_port: int = 0  # 0 = disabled; set e.g. 8080 to enable /health endpoint


# --- Provider ---


class TurnDetectionConfig(BaseModel):
    """Voice activity / turn detection settings for the AI provider."""

    threshold: float = 0.5
    silence_duration_ms: int = 500
    prefix_padding_ms: int = 300


class FailoverProviderConfig(BaseModel):
    """Configuration for a single provider in a failover chain."""

    type: str = ""
    api_key: str = ""
    model: str = ""
    voice: str = ""
    instructions: str = ""


class ProviderConfig(BaseModel):
    """AI voice provider configuration."""

    type: str = "openai_realtime"
    api_key: str = ""
    model: str = "gpt-4o-realtime-preview"
    voice: str = "alloy"
    instructions: str = "You are a helpful voice assistant."
    turn_detection: TurnDetectionConfig = Field(default_factory=TurnDetectionConfig)
    failover_providers: list[FailoverProviderConfig] = Field(default_factory=list)


# --- Platforms ---


class TelegramPlatformConfig(BaseModel):
    """Telegram voice call platform configuration."""

    enabled: bool = False
    api_id: str = ""
    api_hash: str = ""
    phone_number: str = ""
    session_name: str = "voicekit"
    auto_answer: bool = True
    auto_join_group_calls: bool = False
    allowed_users: list[str] = Field(default_factory=list)


class VirtualAudioPlatformConfig(BaseModel):
    """Virtual audio device platform configuration."""

    enabled: bool = False
    input_device: str = ""
    output_device: str = ""
    sample_rate: int = 24000
    channels: int = 1
    chunk_size: int = 4800


class DiscordPlatformConfig(BaseModel):
    """Discord voice channel platform configuration."""

    enabled: bool = False
    bot_token: str = ""
    auto_join_channels: list[str] = Field(default_factory=list)
    command_prefix: str = "!vk"
    guild_ids: list[int] = Field(default_factory=list)
    listen_to_all_users: bool = True


class ZoomPlatformConfig(BaseModel):
    """Zoom meeting platform configuration.

    Requires a Zoom Server-to-Server OAuth app or General App with
    Meeting SDK credentials. See https://marketplace.zoom.us/
    """

    enabled: bool = False
    client_id: str = ""
    client_secret: str = ""
    account_id: str = ""
    meeting_id: str = ""
    meeting_passcode: str = ""
    display_name: str = "VoiceKit AI"
    auto_join: bool = False
    enable_sdk_log: bool = False


class WhatsAppPlatformConfig(BaseModel):
    """WhatsApp Desktop automation platform configuration.

    Uses PulseAudio virtual devices for audio routing and xdotool
    for window automation (call detection, answer, reject).
    """

    enabled: bool = False
    app_path: str = ""
    auto_answer: bool = True
    allowed_contacts: list[str] = Field(default_factory=list)
    process_name: str = "WhatsApp"
    pulse_sink_name: str = "voicekit_whatsapp"


class SignalPlatformConfig(BaseModel):
    """Signal Desktop automation platform configuration.

    Supports two call detection mechanisms: signal-cli (when available)
    and window polling via xdotool. Audio is routed via PulseAudio
    virtual devices.
    """

    enabled: bool = False
    auto_answer: bool = True
    allowed_contacts: list[str] = Field(default_factory=list)
    process_name: str = "Signal"
    pulse_sink_name: str = "voicekit_signal"
    signal_cli_path: str = "signal-cli"
    phone_number: str = ""
    config_dir: str = ""


class SlackPlatformConfig(BaseModel):
    """Slack Huddles platform configuration.

    Uses Slack Bolt with Socket Mode for API interactions and
    PulseAudio for desktop audio routing.
    """

    enabled: bool = False
    bot_token: str = ""
    app_token: str = ""
    auto_join_channels: list[str] = Field(default_factory=list)
    command_prefix: str = "/voicekit"
    process_name: str = "slack"
    pulse_sink_name: str = "voicekit_slack"


class SipPlatformConfig(BaseModel):
    """SIP/phone call platform configuration.

    Requires a SIP account (e.g. Twilio Elastic SIP, Vonage, FreePBX).
    """

    enabled: bool = False
    server: str = ""
    username: str = ""
    password: str = ""
    port: int = 5060
    auto_answer: bool = True
    allowed_numbers: list[str] = Field(default_factory=list)
    local_rtp_port_start: int = 10000
    register_expires: int = 3600


class TeamsPlatformConfig(BaseModel):
    """Microsoft Teams meeting platform configuration.

    Requires an Azure Bot registration with Communications API permissions.
    """

    enabled: bool = False
    client_id: str = ""
    client_secret: str = ""
    tenant_id: str = ""
    meeting_url: str = ""
    callback_url: str = ""
    auto_join: bool = False


class WebRTCPlatformConfig(BaseModel):
    """Generic WebRTC platform configuration.

    Runs a signalling server that browsers can connect to directly.
    """

    enabled: bool = False
    host: str = "0.0.0.0"
    port: int = 8080
    stun_servers: list[str] = Field(
        default_factory=lambda: ["stun:stun.l.google.com:19302"]
    )


class GoogleMeetPlatformConfig(BaseModel):
    """Google Meet platform configuration (Playwright browser automation).

    NOTE: Fragile — relies on Meet's DOM structure which may change.
    """

    enabled: bool = False
    meeting_url: str = ""
    headless: bool = True
    pulse_sink_name: str = "voicekit_meet"


class FaceTimePlatformConfig(BaseModel):
    """FaceTime platform configuration (macOS only).

    Requires BlackHole or similar virtual audio driver on macOS.
    """

    enabled: bool = False
    auto_answer: bool = True
    allowed_contacts: list[str] = Field(default_factory=list)
    virtual_device_name: str = "BlackHole 2ch"
    poll_interval: float = 1.0


class MiddlewareConfig(BaseModel):
    """Audio middleware pipeline configuration."""

    echo_cancellation: bool = False
    echo_tail_ms: int = 150
    noise_gate: bool = False
    noise_gate_threshold_db: float = -40
    recording: bool = False
    recording_dir: str = "recordings"
    transcript_logging: bool = False
    transcript_dir: str = "transcripts"
    transcript_format: str = "json"
    rate_limiting: bool = False
    rate_limit_bytes_per_second: int = 96000


class PersistenceConfig(BaseModel):
    """Conversation persistence configuration."""

    enabled: bool = False
    storage_dir: str = "conversations"
    max_history: int = 50
    ttl_hours: float = 0  # 0 = no expiry


class PlatformsConfig(BaseModel):
    """All platform configurations."""

    telegram: TelegramPlatformConfig = Field(default_factory=TelegramPlatformConfig)
    virtual_audio: VirtualAudioPlatformConfig = Field(
        default_factory=VirtualAudioPlatformConfig
    )
    discord: DiscordPlatformConfig = Field(default_factory=DiscordPlatformConfig)
    zoom: ZoomPlatformConfig = Field(default_factory=ZoomPlatformConfig)
    whatsapp: WhatsAppPlatformConfig = Field(default_factory=WhatsAppPlatformConfig)
    signal: SignalPlatformConfig = Field(default_factory=SignalPlatformConfig)
    slack: SlackPlatformConfig = Field(default_factory=SlackPlatformConfig)
    sip: SipPlatformConfig = Field(default_factory=SipPlatformConfig)
    teams: TeamsPlatformConfig = Field(default_factory=TeamsPlatformConfig)
    webrtc: WebRTCPlatformConfig = Field(default_factory=WebRTCPlatformConfig)
    google_meet: GoogleMeetPlatformConfig = Field(
        default_factory=GoogleMeetPlatformConfig
    )
    facetime: FaceTimePlatformConfig = Field(default_factory=FaceTimePlatformConfig)


# --- Root ---


class VoiceKitConfig(BaseModel):
    """Root configuration for VoiceKit."""

    daemon: DaemonConfig = Field(default_factory=DaemonConfig)
    provider: ProviderConfig = Field(default_factory=ProviderConfig)
    platforms: PlatformsConfig = Field(default_factory=PlatformsConfig)
    middleware: MiddlewareConfig = Field(default_factory=MiddlewareConfig)
    persistence: PersistenceConfig = Field(default_factory=PersistenceConfig)


def load_config(path: str | Path) -> VoiceKitConfig:
    """Load and validate configuration from a YAML file.

    Environment variables referenced as ``${VAR_NAME}`` are expanded
    before validation.

    Args:
        path: Path to the YAML configuration file.

    Returns:
        Validated configuration object.

    Raises:
        FileNotFoundError: If the config file does not exist.
        pydantic.ValidationError: If the config is invalid.
    """
    config_path = Path(path)
    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")

    with open(config_path) as f:
        raw = yaml.safe_load(f) or {}

    expanded = _expand_env_vars(raw)
    return VoiceKitConfig.model_validate(expanded)


def default_config() -> VoiceKitConfig:
    """Return a default configuration instance."""
    return VoiceKitConfig()
