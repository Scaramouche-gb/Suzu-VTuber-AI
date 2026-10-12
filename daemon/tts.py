"""Text-to-Speech (TTS) synthesis engine supporting edge-tts, emotional modulation and pronunciation replacements."""

from __future__ import annotations

import abc
import asyncio
import io
import logging
import re
import time
import tomllib
from pathlib import Path

import edge_tts

from daemon.config import TTSConfig

logger = logging.getLogger(__name__)

DEFAULT_PRONOUNCE_PATH = Path("pronounce.toml")
AUDIO_CACHE_DIR = Path.home() / ".cache" / "vtuber" / "audio"


class PronunciationDictionary:
    """Preprocesses text using word boundary replacements from pronounce.toml."""

    def __init__(self, dict_path: Path | None = None) -> None:
        self.replacements: list[tuple[re.Pattern[str], str]] = []
        path = dict_path or DEFAULT_PRONOUNCE_PATH
        if path.exists():
            try:
                with open(path, "rb") as f:
                    data = tomllib.load(f)
                reps = data.get("replacements", {})
                for k, v in reps.items():
                    # Word boundary case-insensitive regex
                    pattern = re.compile(rf"\b{re.escape(k)}\b", re.IGNORECASE)
                    self.replacements.append((pattern, str(v)))
            except (OSError, tomllib.TOMLDecodeError) as exc:
                logger.warning("Failed to load pronunciation dictionary %s: %s", path, exc)

    def apply(self, text: str) -> str:
        for pattern, replacement in self.replacements:
            text = pattern.sub(replacement, text)
        return text


class BaseTTSEngine(abc.ABC):
    """Abstract base class for TTS synthesis engines."""

    @abc.abstractmethod
    async def synthesize(self, text: str, emotion: str = "neutral", intensity: float = 1.0) -> bytes:
        """Synthesize text to audio bytes (mp3 or wav)."""


class EdgeTTSEngine(BaseTTSEngine):
    """Cloud-based Edge TTS implementation with emotional pitch and rate adjustment."""

    def __init__(self, cfg: TTSConfig, dict_path: Path | None = None) -> None:
        self.cfg = cfg
        self.pronounce = PronunciationDictionary(dict_path)

    def _get_emotion_modulation(self, emotion: str, intensity: float) -> tuple[str, str]:
        """Compute rate and pitch offsets based on emotion."""
        rate = self.cfg.rate
        pitch = self.cfg.pitch

        # Moderate emotional modulations to prevent cartoonish sounds
        scale = min(1.0, max(0.0, intensity))
        if emotion == "happy":
            rate = f"+{int(5 + 5 * scale)}%"
            pitch = f"+{int(2 + 2 * scale)}Hz"
        elif emotion == "sad":
            rate = f"-{int(6 + 6 * scale)}%"
            pitch = f"-{int(3 + 2 * scale)}Hz"
        elif emotion == "angry":
            rate = f"+{int(8 + 6 * scale)}%"
            pitch = f"+{int(3 + 3 * scale)}Hz"
        elif emotion == "surprised":
            rate = f"+{int(6 + 6 * scale)}%"
            pitch = f"+{int(4 + 3 * scale)}Hz"
        elif emotion == "relaxed":
            rate = f"-{int(3 + 4 * scale)}%"
            pitch = f"-{int(1 + 2 * scale)}Hz"

        return rate, pitch

    async def synthesize(self, text: str, emotion: str = "neutral", intensity: float = 1.0) -> bytes:
        clean_text = self.pronounce.apply(text).strip()
        if not clean_text:
            return b""

        rate, pitch = self._get_emotion_modulation(emotion, intensity)

        communicate = edge_tts.Communicate(
            text=clean_text,
            voice=self.cfg.voice,
            rate=rate,
            pitch=pitch,
        )

        audio_buffer = io.BytesIO()
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                audio_buffer.write(chunk["data"])

        return audio_buffer.getvalue()


class MockTTSEngine(BaseTTSEngine):
    """Mock TTS generating tiny synthetic audio frames for testing and dry-run."""

    def __init__(self) -> None:
        pass

    async def synthesize(self, text: str, emotion: str = "neutral", intensity: float = 1.0) -> bytes:
        await asyncio.sleep(0.05)
        # 1-second silence/beep header in MP3 or dummy bytes
        return b"\xff\xfb\x90\x64\x00\x00\x00\x00" * 64


def save_audio_file(utterance_id: str, audio_bytes: bytes) -> Path:
    """Save synthesized audio bytes to ~/.cache/vtuber/audio/<id>.mp3."""
    AUDIO_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    file_path = AUDIO_CACHE_DIR / f"{utterance_id}.mp3"
    file_path.write_bytes(audio_bytes)
    return file_path


def cleanup_audio_cache(max_age_s: float = 3600.0) -> int:
    """Remove audio cache files older than max_age_s (default: 1 hour)."""
    if not AUDIO_CACHE_DIR.exists():
        return 0

    now = time.time()
    deleted_count = 0
    for f in AUDIO_CACHE_DIR.glob("*.mp3"):
        try:
            if now - f.stat().st_mtime > max_age_s:
                f.unlink()
                deleted_count += 1
        except OSError:
            pass
    return deleted_count


def create_tts_engine(cfg: TTSConfig, dry_run: bool = False) -> BaseTTSEngine:
    """TTS engine factory."""
    if dry_run:
        return MockTTSEngine()
    if cfg.engine == "edge":
        return EdgeTTSEngine(cfg)
    # Default fallback
    return EdgeTTSEngine(cfg)
