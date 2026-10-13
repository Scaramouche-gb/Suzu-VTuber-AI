"""Unit tests for configuration loader and security constraints."""

from pathlib import Path

import pytest

from daemon.config import Config, ServerConfig, load_config


def test_load_config_defaults() -> None:
    config = load_config("config.toml")
    assert config.general.name == "Сузу"
    assert config.server.host == "127.0.0.1"
    assert config.server.port == 8765
    assert config.llm.max_tokens == 300
    assert config.tts.engine in ("edge", "silero")
    assert config.audio_in.sample_rate == 16000


def test_server_host_security_validation() -> None:
    # Must accept 127.0.0.1 or localhost
    ServerConfig(host="127.0.0.1", port=8765)
    ServerConfig(host="localhost", port=8765)

    # Must reject 0.0.0.0 or any public/external binding
    with pytest.raises(ValueError, match="Security violation"):
        ServerConfig(host="0.0.0.0", port=8765)

    with pytest.raises(ValueError, match="Security violation"):
        ServerConfig(host="192.168.1.100", port=8765)


def test_missing_config_falls_back_to_defaults(tmp_path: Path) -> None:
    missing_file = tmp_path / "non_existent.toml"
    config = load_config(missing_file)
    assert isinstance(config, Config)
    assert config.server.host == "127.0.0.1"
