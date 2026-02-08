"""Configuration schema for VoiceBridge.

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


# --- Provider ---


class TurnDetectionConfig(BaseModel):
    """Voice activity / turn detection settings for the AI provider."""

    threshold: float = 0.5
    silence_duration_ms: int = 500
    prefix_padding_ms: int = 300


class ProviderConfig(BaseModel):
    """AI voice provider configuration."""

    type: str = "openai_realtime"
    api_key: str = ""
    model: str = "gpt-4o-realtime-preview"
    voice: str = "alloy"
    instructions: str = "You are a helpful voice assistant."
    turn_detection: TurnDetectionConfig = Field(default_factory=TurnDetectionConfig)


# --- Platforms ---


class TelegramPlatformConfig(BaseModel):
    """Telegram voice call platform configuration."""

    enabled: bool = False
    api_id: str = ""
    api_hash: str = ""
    phone_number: str = ""
    auto_answer: bool = True
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
    command_prefix: str = "!vb"


class ZoomPlatformConfig(BaseModel):
    """Zoom meeting platform configuration."""

    enabled: bool = False
    client_id: str = ""
    client_secret: str = ""
    bot_jid: str = ""


class WhatsAppPlatformConfig(BaseModel):
    """WhatsApp Desktop automation platform configuration."""

    enabled: bool = False
    app_path: str = ""
    auto_answer: bool = True
    allowed_contacts: list[str] = Field(default_factory=list)


class SignalPlatformConfig(BaseModel):
    """Signal Desktop automation platform configuration."""

    enabled: bool = False
    auto_answer: bool = True
    allowed_contacts: list[str] = Field(default_factory=list)


class SlackPlatformConfig(BaseModel):
    """Slack Huddles platform configuration."""

    enabled: bool = False
    bot_token: str = ""
    app_token: str = ""


class SipPlatformConfig(BaseModel):
    """SIP/phone call platform configuration."""

    enabled: bool = False
    server: str = ""
    username: str = ""
    password: str = ""
    port: int = 5060
    auto_answer: bool = True
    allowed_numbers: list[str] = Field(default_factory=list)


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


# --- Root ---


class VoiceBridgeConfig(BaseModel):
    """Root configuration for VoiceBridge."""

    daemon: DaemonConfig = Field(default_factory=DaemonConfig)
    provider: ProviderConfig = Field(default_factory=ProviderConfig)
    platforms: PlatformsConfig = Field(default_factory=PlatformsConfig)


def load_config(path: str | Path) -> VoiceBridgeConfig:
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
    return VoiceBridgeConfig.model_validate(expanded)


def default_config() -> VoiceBridgeConfig:
    """Return a default configuration instance."""
    return VoiceBridgeConfig()
