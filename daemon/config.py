"""Configuration loader and validator for Suzu VTuber AI daemon."""

from __future__ import annotations

import logging
import os
import stat
import tomllib
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

logger = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = Path("config.toml")
SECRETS_PATH = Path.home() / ".config" / "vtuber" / "secrets.env"


class GeneralConfig(BaseModel):
    name: str = "Сузу"
    user_name: str = "Хозяин"
    language: str = "ru"
    log_level: str = "INFO"


class ServerConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8765

    @field_validator("host")
    @classmethod
    def validate_localhost_only(cls, v: str) -> str:
        if v not in ("127.0.0.1", "localhost"):
            raise ValueError(f"Security violation: server must only bind to 127.0.0.1, got '{v}'")
        return v


class LLMConfig(BaseModel):
    chat_model: str = "openai/claude-sonnet-5-5"
    fast_model: str = "openai/claude-haiku-4-5-20251001"
    fallback_model: str = "openai/claude-haiku-4-5-20251001"
    api_base: str = "https://codex.sale/v1"
    api_key: str = ""
    temperature: float = 0.8
    max_tokens: int = 300
    max_usd_per_day: float = 10.0


class STTConfig(BaseModel):
    engine: str = "faster-whisper"
    model: str = "large-v3-turbo"
    device: str = "cuda"
    compute_type: float | str = "float16"
    language: str = ""
    initial_prompt: str = "Разговор с подругой по имени Сузу."


class AudioInConfig(BaseModel):
    device: str = ""
    sample_rate: int = 16000
    vad_threshold: float = 0.5
    min_speech_ms: int = 250
    end_silence_ms: int = 700
    preroll_ms: int = 300


class SystemAudioConfig(BaseModel):
    enabled: bool = True
    device: str = ""
    sample_rate: int = 16000
    vad_threshold: float = 0.50
    min_speech_ms: int = 350
    end_silence_ms: int = 750
    preroll_ms: int = 350


class RVCConfig(BaseModel):
    enabled: bool = False
    mode: str = "sidecar"  # "sidecar" or "onnx"
    server_url: str = "http://127.0.0.1:5050/infer"
    model_path: str = ""
    index_path: str = ""
    f0_up_key: int = 0


class XTTSConfig(BaseModel):
    enabled: bool = True
    server_url: str = "http://127.0.0.1:5055/synthesize"
    speaker_wav: str = "models/xtts/speaker.wav"
    language: str = "ru"
    speed: float = 1.0


class TTSConfig(BaseModel):
    engine: str = "edge"
    voice: str = "ru-RU-SvetlanaNeural"
    rate: str = "+5%"
    pitch: str = "+2Hz"
    pitch_shift: float = 1.15
    rvc: RVCConfig = Field(default_factory=RVCConfig)
    xtts: XTTSConfig = Field(default_factory=XTTSConfig)


class VisionConfig(BaseModel):
    enabled: bool = True
    output: str = ""
    interval_s: float = 1.0
    scale: float = 0.4
    jpeg_quality: int = 70
    change_threshold: float = 0.08
    send_cooldown_s: float = 25.0
    max_vlm_calls_per_hour: int = 60
    blacklist_app_ids: list[str] = Field(default_factory=list)
    blacklist_title_globs: list[str] = Field(default_factory=list)


class InitiativeConfig(BaseModel):
    enabled: bool = True
    mean_interval_s: float = 180.0
    min_gap_s: float = 45.0
    quiet_hours: list[str] = Field(default_factory=lambda: ["01:00-08:00"])
    backoff_factor: float = 2.0
    max_backoff_s: float = 1800.0


class MemoryConfig(BaseModel):
    db_path: str = "~/.local/share/vtuber/memory.db"
    context_turns: int = 12
    summarize_every: int = 20


class AvatarConfig(BaseModel):
    model: str = "models/avatar.vrm"
    fps: int = 30
    scale: float = 1.0
    y_offset: float = 0.0


class TelegramConfig(BaseModel):
    enabled: bool = False
    mode: str = "both"  # "bot", "userbot", "both"
    bot_token: str = Field(default_factory=lambda: os.getenv("TELEGRAM_BOT_TOKEN", ""))
    default_chat_id: int | str = Field(default_factory=lambda: os.getenv("TELEGRAM_DEFAULT_CHAT_ID", ""))
    api_id: int = Field(default_factory=lambda: int(os.getenv("TELEGRAM_API_ID", "0") or "0"))
    api_hash: str = Field(default_factory=lambda: os.getenv("TELEGRAM_API_HASH", ""))
    session_name: str = "suzu_userbot"
    auto_reply: bool = True

    @model_validator(mode="before")
    @classmethod
    def apply_env_fallbacks(cls, data: Any) -> Any:
        if isinstance(data, dict):
            if not data.get("bot_token"):
                data["bot_token"] = os.getenv("TELEGRAM_BOT_TOKEN", "")
            if not data.get("default_chat_id"):
                data["default_chat_id"] = os.getenv("TELEGRAM_DEFAULT_CHAT_ID", "")
            if not data.get("api_id") or data.get("api_id") == 0:
                env_api_id = os.getenv("TELEGRAM_API_ID", "0").strip()
                data["api_id"] = int(env_api_id) if env_api_id.isdigit() else 0
            if not data.get("api_hash"):
                data["api_hash"] = os.getenv("TELEGRAM_API_HASH", "")
        return data


class Config(BaseModel):
    general: GeneralConfig = Field(default_factory=GeneralConfig)
    server: ServerConfig = Field(default_factory=ServerConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    stt: STTConfig = Field(default_factory=STTConfig)
    audio_in: AudioInConfig = Field(default_factory=AudioInConfig)
    system_audio: SystemAudioConfig = Field(default_factory=SystemAudioConfig)
    tts: TTSConfig = Field(default_factory=TTSConfig)
    vision: VisionConfig = Field(default_factory=VisionConfig)
    initiative: InitiativeConfig = Field(default_factory=InitiativeConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    avatar: AvatarConfig = Field(default_factory=AvatarConfig)
    telegram: TelegramConfig = Field(default_factory=TelegramConfig)


def load_secrets(secrets_file: Path | None = None) -> None:
    """Load secrets into os.environ from ~/.config/vtuber/secrets.env if present."""
    target_path = secrets_file or SECRETS_PATH
    if not target_path.exists():
        return

    # Check file permissions (must be 0600 or stricter: no group/other permissions)
    file_stat = target_path.stat()
    file_mode = file_stat.st_mode
    if bool(file_mode & (stat.S_IRWXG | stat.S_IRWXO)):
        logger.warning(
            "Security warning: secrets file %s has insecure permissions (expected 0600, got %o). "
            "Please fix with 'chmod 600 %s'.",
            target_path,
            stat.S_IMODE(file_mode),
            target_path,
        )

    try:
        with open(target_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    key, val = line.split("=", 1)
                    key = key.strip()
                    val = val.strip().strip("'\"")
                    # Do not overwrite already existing environment variables
                    if key and key not in os.environ:
                        os.environ[key] = val
    except OSError as exc:
        logger.error("Failed to read secrets file %s: %s", target_path, exc)


def load_config(config_path: Path | str | None = None) -> Config:
    """Load and validate configuration from toml file and environment secrets."""
    load_secrets()

    cfg_file = Path(config_path) if config_path else DEFAULT_CONFIG_PATH
    if not cfg_file.exists():
        logger.warning("Config file %s not found, using default configuration", cfg_file)
        return Config()

    with open(cfg_file, "rb") as f:
        raw_data = tomllib.load(f)

    return Config.model_validate(raw_data)
