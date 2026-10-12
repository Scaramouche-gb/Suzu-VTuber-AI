"""Speech-to-Text (STT) transcription with faster-whisper and hallucination filtering."""

from __future__ import annotations

import difflib
import logging
from typing import Any

import numpy as np
from faster_whisper import WhisperModel

from daemon.config import STTConfig

logger = logging.getLogger(__name__)

# Typical Whisper hallucination phrases on noise/silence
HALLUCINATION_BLACKLIST = [
    "продолжение следует",
    "субтитры сделал",
    "субтитры создавал",
    "субтитры добавил",
    "перевод субтитров",
    "редактор субтитров",
    "спасибо за просмотр",
    "подписывайтесь на канал",
    "ставьте лайки",
    "до скорой встречи",
    "конец фильма",
    "титры",
    "всем пока",
    "благодарю за внимание",
    "thanks for watching",
    "subtitles by",
    "translated by",
    "subscribe to my channel",
]


class WhisperSTT:
    """Faster-whisper wrapper with lazy loading and hallucination filtering."""

    def __init__(self, cfg: STTConfig) -> None:
        self.cfg = cfg
        self._model: WhisperModel | None = None
        self._last_avatar_phrase = ""

    def set_last_avatar_phrase(self, phrase: str) -> None:
        """Track last avatar utterance to prevent self-echo."""
        self._last_avatar_phrase = phrase.strip().lower()

    def load_model(self) -> WhisperModel:
        """Lazy load faster-whisper model."""
        if self._model is None:
            logger.info("Loading faster-whisper model '%s' on %s (%s)...", self.cfg.model, self.cfg.device, self.cfg.compute_type)
            try:
                self._model = WhisperModel(
                    self.cfg.model,
                    device=self.cfg.device,
                    compute_type=self.cfg.compute_type,
                )
            except (RuntimeError, ValueError, OSError) as exc:
                logger.warning("Failed to load Whisper on %s (%s): %s. Falling back to cpu/int8", self.cfg.device, self.cfg.compute_type, exc)
                self._model = WhisperModel(
                    self.cfg.model,
                    device="cpu",
                    compute_type="int8",
                )
            logger.info("faster-whisper model loaded successfully")
        return self._model

    def transcribe(self, audio: np.ndarray) -> str:
        """Transcribe float32 16kHz audio buffer to filtered text."""
        if len(audio) == 0:
            return ""

        model = self.load_model()

        language = self.cfg.language if self.cfg.language else None
        segments, info = model.transcribe(
            audio,
            language=language,
            vad_filter=False,  # Audio is already pre-filtered by Silero VAD
            beam_size=1,
            initial_prompt=self.cfg.initial_prompt,
            condition_on_previous_text=False,
        )

        valid_texts: list[str] = []
        for segment in segments:
            text = segment.text.strip()
            if not text:
                continue

            # 1. Metric filtering
            if segment.no_speech_prob > 0.65:
                logger.debug("STT: Discarded segment due to no_speech_prob (%.2f): '%s'", segment.no_speech_prob, text)
                continue
            if segment.avg_logprob < -1.1:
                logger.debug("STT: Discarded segment due to low avg_logprob (%.2f): '%s'", segment.avg_logprob, text)
                continue
            if segment.compression_ratio > 2.4:
                logger.debug("STT: Discarded repetitive hallucination (compression=%.2f): '%s'", segment.compression_ratio, text)
                continue

            # 2. Blacklist check
            clean_lower = text.lower()
            if any(b in clean_lower for b in HALLUCINATION_BLACKLIST):
                logger.debug("STT: Discarded blacklisted hallucination: '%s'", text)
                continue

            # 3. Minimum length
            if len(clean_lower.strip(".,!?-—~ ")) < 2:
                continue

            valid_texts.append(text)

        result_text = " ".join(valid_texts).strip()
        if not result_text:
            return ""

        # 4. Self-echo rejection
        if self._last_avatar_phrase:
            similarity = difflib.SequenceMatcher(None, result_text.lower(), self._last_avatar_phrase).ratio()
            if similarity > 0.72:
                logger.info("STT: Discarded acoustic self-echo (similarity=%.2f): '%s'", similarity, result_text)
                return ""

        logger.info("STT recognized: '%s' (lang: %s, prob: %.2f)", result_text, info.language, info.language_probability)
        return result_text


class MockSTT:
    """Mock STT transcriber for testing and dry-run."""

    def __init__(self) -> None:
        pass

    def transcribe(self, audio: Any) -> str:
        return "Привет, Сузу!"
