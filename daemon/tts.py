"""Text-to-Speech (TTS) synthesis engine supporting edge-tts, emotional modulation and pronunciation replacements."""

from __future__ import annotations

import abc
import asyncio
import io
import logging
import re
import subprocess
import time
import tomllib
from pathlib import Path
from typing import Any

import aiohttp
import edge_tts
import numpy as np
import torch

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
    async def synthesize(
        self, text: str, emotion: str = "neutral", intensity: float = 1.0
    ) -> bytes:
        """Synthesize text to audio bytes (mp3 or wav)."""


class EdgeTTSEngine(BaseTTSEngine):
    """Cloud-based Edge TTS implementation with emotional pitch and rate adjustment."""

    def __init__(self, cfg: TTSConfig, dict_path: Path | None = None) -> None:
        self.cfg = cfg
        self.pronounce = PronunciationDictionary(dict_path)
        self._silero_fallback: SileroTTSEngine | None = None

    def _get_emotion_modulation(self, emotion: str, intensity: float) -> tuple[str, str]:
        """Compute rate and pitch offsets based on emotion."""
        rate = self.cfg.rate
        # Microsoft Edge TTS rejects non-standard pitch modulations with 403 Forbidden;
        # keep pitch at +0Hz to prevent connection rejections while modulating speech rate.
        pitch = self.cfg.pitch

        scale = min(1.0, max(0.0, intensity))
        if emotion == "happy":
            rate = f"+{int(4 + 4 * scale)}%"
            pitch = f"+{int(2 + 2 * scale)}Hz"
        elif emotion == "sad":
            rate = f"-{int(5 + 5 * scale)}%"
            pitch = f"-{int(3 + 2 * scale)}Hz"
        elif emotion == "angry":
            rate = f"+{int(6 + 4 * scale)}%"
            pitch = f"+{int(3 + 2 * scale)}Hz"
        elif emotion == "surprised":
            rate = f"+{int(5 + 3 * scale)}%"
            pitch = f"+{int(3 + 2 * scale)}Hz"
        elif emotion == "relaxed":
            rate = f"-{int(3 + 3 * scale)}%"
            pitch = f"-{int(1 + 2 * scale)}Hz"

        return rate, pitch

    async def synthesize(
        self, text: str, emotion: str = "neutral", intensity: float = 1.0
    ) -> bytes:
        clean_text = self.pronounce.apply(text).strip()
        if not clean_text:
            return b""

        rate, pitch = self._get_emotion_modulation(emotion, intensity)

        # Retry loop to handle transient Sec-MS-GEC handshake 403 or network drops
        last_error: Exception | None = None
        for attempt in range(3):
            try:
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

                audio_data = audio_buffer.getvalue()
                if audio_data:
                    shift = getattr(self.cfg, "pitch_shift", 1.0)
                    if shift != 1.0:
                        audio_data = self._apply_pitch_shift(audio_data, shift)
                    return audio_data
            except (
                aiohttp.ClientError,
                edge_tts.exceptions.EdgeTTSException,
                OSError,
                RuntimeError,
                ConnectionError,
                TimeoutError,
            ) as exc:
                last_error = exc
                logger.debug("Edge TTS attempt %d failed: %s, retrying...", attempt + 1, exc)
                if attempt < 2:
                    await asyncio.sleep(0.3 * (attempt + 1))

        if last_error:
            logger.warning("Edge TTS failed (%s). Falling back to local Silero TTS...", last_error)
            if self._silero_fallback is None:
                self._silero_fallback = SileroTTSEngine(self.cfg)
            return await self._silero_fallback.synthesize(text, emotion, intensity)
        return b""

    def _apply_pitch_shift(self, audio_bytes: bytes, shift: float) -> bytes:
        """Post-process audio with FFmpeg to create bright anime voice tone."""
        if shift == 1.0 or not audio_bytes:
            return audio_bytes
        try:
            cmd = [
                "ffmpeg",
                "-y",
                "-i",
                "pipe:0",
                "-af",
                f"asetrate=24000*{shift},aresample=24000,atempo={1.0 / shift},volume=1.15",
                "-f",
                "mp3",
                "pipe:1",
            ]
            proc = subprocess.run(cmd, input=audio_bytes, capture_output=True, check=False)
            if proc.returncode == 0 and proc.stdout:
                return proc.stdout
        except (subprocess.SubprocessError, OSError) as exc:
            logger.warning("FFmpeg pitch shifting failed: %s", exc)
        return audio_bytes


class SileroTTSEngine(BaseTTSEngine):
    """Local 100% offline Silero TTS engine on CPU with anime pitch shifting."""

    def __init__(self, cfg: TTSConfig, dict_path: Path | None = None) -> None:
        self.cfg = cfg
        self.pronounce = PronunciationDictionary(dict_path)
        self._model: Any = None
        self._device = torch.device("cpu")

    def _get_model(self) -> Any:
        if self._model is None:
            logger.info("Loading local Silero TTS model (100% offline)...")
            model, _ = torch.hub.load(
                repo_or_dir="snakers4/silero-models",
                model="silero_tts",
                language="ru",
                speaker="v4_ru",
            )
            model.to(self._device)
            self._model = model
        return self._model

    async def synthesize(
        self, text: str, emotion: str = "neutral", intensity: float = 1.0
    ) -> bytes:
        clean_text = self.pronounce.apply(text).strip()
        if not clean_text:
            return b""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None, self._sync_synthesize, clean_text, emotion, intensity
        )

    def _sync_synthesize(
        self, text: str, emotion: str = "neutral", intensity: float = 1.0
    ) -> bytes:
        try:
            model = self._get_model()
            audio = model.apply_tts(text=text, speaker="kseniya", sample_rate=24000)
            pcm_data = (audio.numpy() * 32767).astype(np.int16).tobytes()

            base_shift = getattr(self.cfg, "pitch_shift", 1.04)
            scale = min(1.0, max(0.0, intensity))
            emo = (emotion or "neutral").lower()

            shift = base_shift
            tempo_factor = 1.0
            volume = 1.25
            eq = ",equalizer=f=300:width_type=h:width=150:g=2.2,lowpass=f=9200"

            if emo in ("happy", "joy"):
                shift = base_shift + 0.025 * scale
                tempo_factor = 1.0 + 0.04 * scale
            elif emo in ("playful", "silly", "smug"):
                shift = base_shift + 0.03 * scale
                tempo_factor = 1.0 + 0.04 * scale
            elif emo in ("surprised", "excited"):
                shift = base_shift + 0.045 * scale
                tempo_factor = 1.0 + 0.06 * scale
            elif emo in ("angry", "annoyed", "pout"):
                shift = base_shift - 0.02 * scale
                tempo_factor = 1.0 + 0.06 * scale
                volume = 1.30
            elif emo in ("sad", "worried", "sorrow"):
                shift = base_shift - 0.03 * scale
                tempo_factor = 1.0 - 0.06 * scale
                volume = 1.10
            elif emo in ("sleepy", "tired"):
                shift = base_shift - 0.025 * scale
                tempo_factor = 1.0 - 0.10 * scale
                volume = 1.05
            elif emo in ("relaxed", "calm"):
                shift = base_shift - 0.01 * scale
                tempo_factor = 1.0 - 0.02 * scale
                volume = 1.15
            elif emo in ("curious", "thinking", "doubt"):
                shift = base_shift + 0.015 * scale
                tempo_factor = 1.01
                volume = 1.20

            final_tempo = max(0.6, min(1.8, (1.0 / shift) * tempo_factor))

            cmd = [
                "ffmpeg",
                "-y",
                "-f",
                "s16le",
                "-ar",
                "24000",
                "-ac",
                "1",
                "-i",
                "pipe:0",
                "-af",
                f"asetrate=24000*{shift:.3f},aresample=24000,atempo={final_tempo:.3f},volume={volume:.2f}{eq}",
                "-f",
                "mp3",
                "pipe:1",
            ]
            proc = subprocess.run(cmd, input=pcm_data, capture_output=True, check=False)
            if proc.returncode == 0 and proc.stdout:
                return proc.stdout
        except (subprocess.SubprocessError, OSError, RuntimeError, ValueError) as exc:
            logger.error("Silero TTS synthesis failed: %s", exc)
        return b""


class MockTTSEngine(BaseTTSEngine):
    """Mock TTS generating tiny synthetic audio frames for testing and dry-run."""

    def __init__(self) -> None:
        pass

    async def synthesize(
        self, text: str, emotion: str = "neutral", intensity: float = 1.0
    ) -> bytes:
        await asyncio.sleep(0.05)
        # 1-second silence/beep header in MP3 or dummy bytes
        return b"\xff\xfb\x90\x64\x00\x00\x00\x00" * 64


def save_audio_file(utterance_id: str, audio_bytes: bytes) -> Path:
    """Save synthesized audio bytes to ~/.cache/vtuber/audio/<id>.mp3."""
    AUDIO_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    file_path = AUDIO_CACHE_DIR / f"{utterance_id}.mp3"
    file_path.write_bytes(audio_bytes)
    return file_path


def get_audio_path(utterance_id: str) -> Path:
    """Return cache file path for an utterance ID."""
    return AUDIO_CACHE_DIR / f"{utterance_id}.mp3"


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
    if cfg.engine == "silero":
        return SileroTTSEngine(cfg)
    if cfg.engine == "edge":
        return EdgeTTSEngine(cfg)
    return EdgeTTSEngine(cfg)
