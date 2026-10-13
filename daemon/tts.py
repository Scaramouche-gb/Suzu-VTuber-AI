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
from daemon.rvc import RVCVoiceConverter

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


def get_rvc_semitones(base_f0: int, emotion: str, intensity: float = 1.0) -> int:
    """Compute expressive RVC vocal semitone transposition from emotion tag."""
    emo = (emotion or "neutral").lower()
    scale = min(1.0, max(0.0, intensity))
    if emo in ("excited", "surprised"):
        return base_f0 + round(4 * scale)
    if emo in ("happy", "joy", "laugh"):
        return base_f0 + round(3 * scale)
    if emo in ("playful", "silly", "smug"):
        return base_f0 + round(2 * scale)
    if emo == "whisper":
        return base_f0 + round(1 * scale)
    if emo in ("sad", "worried", "sorrow"):
        return base_f0 - round(2 * scale)
    if emo in ("angry", "annoyed", "pout"):
        return base_f0 - round(1 * scale)
    return base_f0


def build_silero_ssml(text: str, emotion: str, intensity: float = 1.0) -> str:
    """Build expressive SSML for Silero TTS with emotional prosody and organic breath breaks."""
    clean = text.strip()
    # Add subtle micro-breaks after lively human interjections
    interjections = (
        "Хе-хе", "Ха-ха", "Ой", "Ой-й", "Ух ты", "Ого", "Ого-го",
        "Пс-с", "Тсс", "Хм", "Хм-м", "Эх", "Эх-х", "Эй",
    )
    for interj in interjections:
        for punct in (",", "!", ".", "...", "—"):
            target = f"{interj}{punct}"
            if target in clean:
                clean = clean.replace(target, f"{interj}{punct} <break time=\"110ms\"/>")

    emo = (emotion or "neutral").lower()
    if emo == "whisper":
        prosody = '<prosody rate="medium" pitch="high">'
    elif emo in ("excited", "surprised"):
        prosody = '<prosody rate="fast" pitch="x-high">'
    elif emo in ("happy", "joy", "laugh", "playful", "silly", "smug"):
        prosody = '<prosody rate="medium" pitch="high">'
    elif emo in ("sad", "worried", "sorrow", "pout"):
        prosody = '<prosody rate="slow" pitch="low">'
    elif emo in ("angry", "annoyed"):
        prosody = '<prosody rate="fast" pitch="medium">'
    elif emo in ("sleepy", "tired", "relaxed"):
        prosody = '<prosody rate="slow" pitch="x-low">'
    else:
        prosody = '<prosody rate="medium" pitch="medium">'

    return f"<speak>{prosody}{clean}</prosody></speak>"


def apply_emotion_mastering(
    audio_bytes: bytes, emotion: str, intensity: float = 1.0
) -> bytes:
    """Master post-TTS/post-RVC audio with clean acoustic EQ, de-essing, noise/aliasing filter, soft volume leveling, and zero-clipping limiter."""
    if not audio_bytes:
        return audio_bytes
    emo = (emotion or "neutral").lower()
    scale = min(1.0, max(0.0, intensity))

    # Base clean vocal chain:
    # 1. highpass=f=80: cuts subsonic rumble and plosives
    # 2. lowpass=f=10500: cuts harsh neural vocoder hash/grit above 10.5kHz
    # 3. equalizer=f=6000:width_type=q:width=2.0:g=-3.0: de-esser notch taming mic sibilance ('с', 'ш', 'ц')
    # 4. equalizer=f=2200:width_type=q:width=1.5:g=1.2: warm vocal body and intimacy
    filters = [
        "highpass=f=80",
        "lowpass=f=10500",
        "equalizer=f=6000:width_type=q:width=2.0:g=-3.0",
        "equalizer=f=2200:width_type=q:width=1.5:g=1.2",
    ]

    # Softer volume levels and nuanced emotional curves
    if emo == "whisper":
        vol = max(0.50, 0.62 - 0.08 * scale)
        filters.append("highpass=f=200,lowshelf=f=500:g=-4.0,equalizer=f=4500:width_type=q:width=1.5:g=1.5")
    elif emo in ("excited", "surprised"):
        vol = min(0.92, 0.82 + 0.08 * scale)
        filters.append("equalizer=f=3500:width_type=q:width=1.5:g=1.0")
    elif emo in ("happy", "joy", "laugh", "playful", "silly", "smug"):
        vol = min(0.88, 0.80 + 0.06 * scale)
        filters.append("equalizer=f=3000:width_type=q:width=1.5:g=0.8")
    elif emo in ("sad", "sorrow", "worried", "pout"):
        vol = max(0.65, 0.74 - 0.08 * scale)
        filters.append("equalizer=f=2800:width_type=q:width=1.5:g=-1.5")
    elif emo in ("angry", "annoyed"):
        vol = min(0.90, 0.82 + 0.06 * scale)
        filters.append("equalizer=f=2500:width_type=q:width=1.5:g=1.2")
    else:
        vol = 0.78  # Comfortable, pleasant non-fatiguing volume

    filters.append(f"volume={vol:.2f}")
    # Smooth vocal leveling and peak limiter with -1.2 dB true-peak headroom
    filters.append("compand=attacks=0.03:decays=0.15:points=-80/-80|-36/-26|-12/-8|0/-2:gain=0")
    filters.append("alimiter=limit=0.88:attack=5:release=50:asc=1")

    af = ",".join(filters)

    try:
        cmd = [
            "ffmpeg",
            "-y",
            "-i",
            "pipe:0",
            "-af",
            af,
            "-b:a",
            "256k",
            "-q:a",
            "0",
            "-f",
            "mp3",
            "pipe:1",
        ]
        proc = subprocess.run(cmd, input=audio_bytes, capture_output=True, check=False)
        if proc.returncode == 0 and proc.stdout:
            return proc.stdout
    except (subprocess.SubprocessError, OSError) as exc:
        logger.warning("Post-mastering FFmpeg failed (%s), returning base audio", exc)
    return audio_bytes


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
        self.rvc = RVCVoiceConverter(self.cfg.rvc)
        self._silero_fallback: SileroTTSEngine | None = None

    def _get_emotion_modulation(self, emotion: str, intensity: float) -> tuple[str, str]:
        """Compute rate and pitch offsets based on emotion."""
        rate = self.cfg.rate
        pitch = self.cfg.pitch

        scale = min(1.0, max(0.0, intensity))
        emo = (emotion or "neutral").lower()
        if emo in ("happy", "joy"):
            rate = f"+{int(4 + 4 * scale)}%"
            pitch = f"+{int(2 + 2 * scale)}Hz"
        elif emo in ("excited", "surprised"):
            rate = f"+{int(6 + 4 * scale)}%"
            pitch = f"+{int(4 + 3 * scale)}Hz"
        elif emo in ("playful", "silly", "laugh"):
            rate = f"+{int(4 + 3 * scale)}%"
            pitch = f"+{int(2 + 2 * scale)}Hz"
        elif emo == "whisper":
            rate = f"+{int(2 + 2 * scale)}%"
            pitch = f"+{int(1 + 2 * scale)}Hz"
        elif emo in ("sad", "sorrow", "worried", "pout"):
            rate = f"-{int(5 + 5 * scale)}%"
            pitch = f"-{int(3 + 2 * scale)}Hz"
        elif emo in ("angry", "annoyed"):
            rate = f"+{int(6 + 4 * scale)}%"
            pitch = f"+{int(3 + 2 * scale)}Hz"
        elif emo in ("relaxed", "sleepy", "tired"):
            rate = f"-{int(4 + 3 * scale)}%"
            pitch = f"-{int(2 + 2 * scale)}Hz"

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
                    if self.cfg.rvc.enabled:
                        rvc_semitones = get_rvc_semitones(self.cfg.rvc.f0_up_key, emotion, intensity)
                        audio_data = await self.rvc.convert(audio_data, f0_up_key=rvc_semitones)
                    return apply_emotion_mastering(audio_data, emotion, intensity)
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
        self.rvc = RVCVoiceConverter(self.cfg.rvc)
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
        raw_audio = await loop.run_in_executor(
            None, self._sync_synthesize, clean_text, emotion, intensity
        )
        if self.cfg.rvc.enabled and raw_audio:
            rvc_semitones = get_rvc_semitones(self.cfg.rvc.f0_up_key, emotion, intensity)
            audio = await self.rvc.convert(raw_audio, f0_up_key=rvc_semitones)
        else:
            audio = raw_audio
        return apply_emotion_mastering(audio, emotion, intensity)

    def _sync_synthesize(
        self, text: str, emotion: str = "neutral", intensity: float = 1.0
    ) -> bytes:
        try:
            model = self._get_model()
            ssml_text = build_silero_ssml(text, emotion, intensity)
            try:
                audio = model.apply_tts(ssml_text=ssml_text, speaker="kseniya", sample_rate=24000)
            except (RuntimeError, ValueError, TypeError, KeyError) as ssml_err:
                logger.debug("Silero SSML synthesis fallback (%s), using raw text", ssml_err)
                audio = model.apply_tts(text=text, speaker="kseniya", sample_rate=24000)
            pcm_data = (audio.numpy() * 32767).astype(np.int16).tobytes()

            base_shift = getattr(self.cfg, "pitch_shift", 1.04)
            scale = min(1.0, max(0.0, intensity))
            emo = (emotion or "neutral").lower()

            shift = base_shift
            tempo_factor = 1.0
            volume = 1.25
            eq_filter = (
                "equalizer=f=280:width_type=h:width=120:g=1.8,"
                "equalizer=f=3400:width_type=q:width=1.8:g=-2.0,"
                "treble=g=1.5:f=7500"
            )

            if emo == "whisper":
                # Intimate close-mic whisper: preserve youthful anime pitch, no slow dragging
                shift = base_shift + 0.02 * scale
                tempo_factor = 1.02
                volume = 0.85
                eq_filter = (
                    "highpass=f=400,"
                    "lowshelf=f=650:g=-7.0,"
                    "treble=g=5.0:f=7500"
                )
            elif emo in ("excited", "surprised"):
                # Elevated vocal pitch, high energy, crisp presence
                shift = base_shift + 0.12 * scale
                tempo_factor = 1.0 + 0.08 * scale
                volume = 1.35
                eq_filter = (
                    "highpass=f=80,"
                    "equalizer=f=280:width_type=h:width=120:g=1.2,"
                    "equalizer=f=3200:width_type=q:width=1.8:g=2.5,"
                    "treble=g=2.2:f=7500"
                )
            elif emo in ("happy", "joy", "laugh"):
                shift = base_shift + 0.08 * scale
                tempo_factor = 1.0 + 0.05 * scale
                volume = 1.28
                eq_filter = "highpass=f=75,equalizer=f=280:width_type=h:width=120:g=1.5,treble=g=1.8:f=7500"
            elif emo in ("playful", "silly", "smug"):
                shift = base_shift + 0.05 * scale
                tempo_factor = 1.0 + 0.04 * scale
                volume = 1.25
                eq_filter = "highpass=f=75,equalizer=f=280:width_type=h:width=120:g=1.2,treble=g=1.5:f=7500"
            elif emo in ("angry", "annoyed", "pout"):
                shift = base_shift - 0.05 * scale
                tempo_factor = 1.0 + 0.06 * scale
                volume = 1.35
                eq_filter = "highpass=f=60,equalizer=f=250:width_type=h:width=100:g=2.8,treble=g=-1.0:f=6000"
            elif emo in ("sad", "worried", "sorrow"):
                shift = base_shift - 0.08 * scale
                tempo_factor = 1.0 - 0.10 * scale
                volume = 1.05
                eq_filter = "highpass=f=90,equalizer=f=300:width_type=h:width=150:g=-2.0,treble=g=-1.5:f=6000"
            elif emo in ("sleepy", "tired"):
                shift = base_shift - 0.06 * scale
                tempo_factor = 1.0 - 0.12 * scale
                volume = 1.0
                eq_filter = "highpass=f=90,equalizer=f=300:width_type=h:width=150:g=-1.5,treble=g=-1.0:f=6000"

            final_tempo = max(0.6, min(1.8, tempo_factor))
            vocal_filter = (
                f"rubberband=pitch={shift:.3f}:tempo={final_tempo:.3f}:formant=preserved,"
                f"{eq_filter},"
                f"compand=attacks=0.02:decays=0.1:points=-80/-80|-32/-18|-12/-6|0/-2:gain={volume:.2f}"
            )

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
                vocal_filter,
                "-f",
                "mp3",
                "pipe:1",
            ]
            proc = subprocess.run(cmd, input=pcm_data, capture_output=True, check=False)
            if proc.returncode == 0 and proc.stdout:
                return proc.stdout

            # Fallback to legacy asetrate if rubberband fails
            fallback_tempo = max(0.6, min(1.8, (1.0 / shift) * final_tempo))
            cmd_fallback = [
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
                f"asetrate=24000*{shift:.3f},aresample=24000,atempo={fallback_tempo:.3f},{eq_filter},volume={volume:.2f}",
                "-f",
                "mp3",
                "pipe:1",
            ]
            proc_fb = subprocess.run(cmd_fallback, input=pcm_data, capture_output=True, check=False)
            if proc_fb.returncode == 0 and proc_fb.stdout:
                return proc_fb.stdout
        except (subprocess.SubprocessError, OSError, RuntimeError, ValueError) as exc:
            logger.error("Silero TTS synthesis failed: %s", exc)
        return b""


class XTTSEngine(BaseTTSEngine):
    """Coqui XTTS-v2 sidecar engine with voice cloning and dynamic acoustic mastering."""

    def __init__(self, cfg: TTSConfig) -> None:
        self.cfg = cfg
        self.pronounce = PronunciationDictionary()
        self.rvc = (
            RVCVoiceConverter(
                model_path=cfg.rvc.model_path,
                index_path=cfg.rvc.index_path,
                f0_up_key=cfg.rvc.f0_up_key,
                server_url=cfg.rvc.server_url,
            )
            if cfg.rvc.enabled
            else None
        )

    async def synthesize(
        self, text: str, emotion: str = "neutral", intensity: float = 1.0
    ) -> bytes:
        clean = self.pronounce.apply(text).strip()
        if not clean:
            return b""

        payload = {
            "text": clean,
            "speaker_wav": self.cfg.xtts.speaker_wav,
            "language": self.cfg.xtts.language,
            "speed": self.cfg.xtts.speed,
        }

        try:
            async with (
                aiohttp.ClientSession() as session,
                session.post(
                    self.cfg.xtts.server_url,
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=60.0),
                ) as resp,
            ):
                if resp.status != 200:
                    err_text = await resp.text()
                    logger.error("XTTS server error (%d): %s", resp.status, err_text)
                    return b""
                raw_wav = await resp.read()

            if not raw_wav:
                return b""

            # If RVC is also enabled for timbre matching, pass it through RVC
            if self.rvc and self.cfg.rvc.enabled:
                semitones = get_rvc_semitones(self.cfg.rvc.f0_up_key, emotion, intensity)
                processed_wav = await self.rvc.convert(raw_wav, f0_up_key=semitones)
                if processed_wav:
                    raw_wav = processed_wav

            # Apply emotional post-mastering (EQ, presence, dynamics) and encode to MP3
            return apply_emotion_mastering(raw_wav, emotion, intensity)
        except (aiohttp.ClientError, TimeoutError, OSError) as exc:
            logger.error("XTTS synthesis request failed: %s", exc)
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
    if cfg.engine == "xtts":
        return XTTSEngine(cfg)
    if cfg.engine == "silero":
        return SileroTTSEngine(cfg)
    if cfg.engine == "edge":
        return EdgeTTSEngine(cfg)
    return EdgeTTSEngine(cfg)
