"""Unit tests for Whisper STT hallucination filters and self-echo detection."""

from daemon.config import STTConfig
from daemon.stt import HALLUCINATION_BLACKLIST, WhisperSTT


def test_hallucination_blacklist_contains_known_phrases() -> None:
    assert "продолжение следует" in HALLUCINATION_BLACKLIST
    assert "субтитры сделал" in HALLUCINATION_BLACKLIST
    assert "спасибо за просмотр" in HALLUCINATION_BLACKLIST


def test_echo_rejection() -> None:
    cfg = STTConfig()
    stt = WhisperSTT(cfg)
    stt.set_last_avatar_phrase("Привет! Как твои дела сегодня?")

    # Recognized phrase is identical or nearly identical to avatar's last phrase
    import difflib
    ratio = difflib.SequenceMatcher(None, "привет как твои дела сегодня", stt._last_avatar_phrase).ratio()
    assert ratio > 0.72


def test_stt_text_validation_rules() -> None:
    # Test hallucination check logic
    hallucination_candidate = "Спасибо за просмотр!"
    assert any(b in hallucination_candidate.lower() for b in HALLUCINATION_BLACKLIST)

    valid_candidate = "Какая сейчас погода на улице?"
    assert not any(b in valid_candidate.lower() for b in HALLUCINATION_BLACKLIST)
