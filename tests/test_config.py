"""Tests for voicekit.config module."""

import os
import tempfile
from pathlib import Path

import pytest

from voicekit.config import (
    VoiceKitConfig,
    default_config,
    load_config,
)


class TestDefaultConfig:
    def test_default_config_is_valid(self):
        cfg = default_config()
        assert isinstance(cfg, VoiceKitConfig)
        assert cfg.daemon.log_level == "info"
        assert cfg.provider.type == "openai_realtime"
        assert cfg.provider.voice == "alloy"

    def test_default_platforms_disabled(self):
        cfg = default_config()
        assert not cfg.platforms.telegram.enabled
        assert not cfg.platforms.discord.enabled
        assert not cfg.platforms.virtual_audio.enabled
        assert not cfg.platforms.zoom.enabled
        assert not cfg.platforms.sip.enabled

    def test_default_turn_detection(self):
        cfg = default_config()
        assert cfg.provider.turn_detection.threshold == 0.5
        assert cfg.provider.turn_detection.silence_duration_ms == 500


class TestLoadConfig:
    def test_load_minimal_config(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write("daemon:\n  log_level: debug\n")
            f.flush()
            cfg = load_config(f.name)
            assert cfg.daemon.log_level == "debug"
        os.unlink(f.name)

    def test_load_with_provider(self):
        content = """\
provider:
  type: openai_realtime
  api_key: test-key-123
  voice: nova
  instructions: Be helpful.
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(content)
            f.flush()
            cfg = load_config(f.name)
            assert cfg.provider.api_key == "test-key-123"
            assert cfg.provider.voice == "nova"
        os.unlink(f.name)

    def test_load_with_platforms(self):
        content = """\
platforms:
  virtual_audio:
    enabled: true
    input_device: "Test Input"
    output_device: "Test Output"
  telegram:
    enabled: true
    api_id: "12345"
    api_hash: "abcdef"
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(content)
            f.flush()
            cfg = load_config(f.name)
            assert cfg.platforms.virtual_audio.enabled
            assert cfg.platforms.virtual_audio.input_device == "Test Input"
            assert cfg.platforms.telegram.enabled
            assert cfg.platforms.telegram.api_id == "12345"
        os.unlink(f.name)

    def test_env_var_expansion(self, monkeypatch):
        monkeypatch.setenv("TEST_API_KEY", "sk-secret-key")
        content = """\
provider:
  api_key: ${TEST_API_KEY}
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(content)
            f.flush()
            cfg = load_config(f.name)
            assert cfg.provider.api_key == "sk-secret-key"
        os.unlink(f.name)

    def test_env_var_not_set_kept_as_literal(self):
        content = """\
provider:
  api_key: ${NONEXISTENT_VAR_12345}
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(content)
            f.flush()
            cfg = load_config(f.name)
            assert cfg.provider.api_key == "${NONEXISTENT_VAR_12345}"
        os.unlink(f.name)

    def test_file_not_found(self):
        with pytest.raises(FileNotFoundError):
            load_config("/nonexistent/config.yaml")

    def test_empty_config_file(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write("")
            f.flush()
            cfg = load_config(f.name)
            assert isinstance(cfg, VoiceKitConfig)
        os.unlink(f.name)
