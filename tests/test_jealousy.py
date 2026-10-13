from unittest.mock import AsyncMock, patch

import pytest

from daemon.bus import EventBus, SystemSpeech
from daemon.conductor import Conductor, detect_jealousy_trigger
from daemon.config import Config
from daemon.llm import LLMClient
from daemon.ws_server import WebSocketServer


def test_detect_jealousy_trigger_positive() -> None:
    """Test phrases that MUST trigger jealousy mode."""
    positive_cases = [
        "Смотри какая стримерша милая",
        "Алиса намного лучше тебя отвечает",
        "Она такая классная девчонка",
        "Какая классная стримерша на экране",
        "Маша такая красивая",
        "Сири лучше соображает",
        "Ты видела, какая она милая?",
        "Эта витуберша просто огонь",
        "Какая классная девчонка",
        "Влюбился в эту стримершу",
    ]
    for phrase in positive_cases:
        assert detect_jealousy_trigger(phrase) is True, f"Failed for positive phrase: {phrase}"


def test_detect_jealousy_trigger_negative() -> None:
    """Test phrases that MUST NOT trigger jealousy mode."""
    negative_cases = [
        "Сузу, ты такая милая!",
        "Ты самая лучшая, Сузу",
        "Привет, как твои дела?",
        "Запусти редактор Zed",
        "Тесты упали на строке сорок два",
        "Стоп",
        "Открой браузер",
    ]
    for phrase in negative_cases:
        assert detect_jealousy_trigger(phrase) is False, f"Failed for negative phrase: {phrase}"


@pytest.mark.asyncio
async def test_conductor_user_turn_jealousy_injection() -> None:
    """Test that conductor enriches context_info when user praises someone else."""
    cfg = Config()
    bus = EventBus()
    ws_mock = AsyncMock(spec=WebSocketServer)
    llm = LLMClient(cfg.llm, dry_run=True)
    conductor = Conductor(llm=llm, ws_server=ws_mock, bus=bus)

    with patch.object(conductor, "_generate_turn", new_callable=AsyncMock) as mock_gen:
        await conductor.process_user_turn("Смотри какая стримерша милая")
        assert mock_gen.called
        kwargs = mock_gen.call_args.kwargs
        assert "режим забавной собственницы и ревности" in kwargs.get("context_info", "")
        assert "<emo=pout:0.9>" in kwargs.get("context_info", "")


@pytest.mark.asyncio
async def test_conductor_system_speech_jealousy_injection() -> None:
    """Test that conductor enriches prompt when system voice praises a rival."""
    cfg = Config()
    bus = EventBus()
    ws_mock = AsyncMock(spec=WebSocketServer)
    llm = LLMClient(cfg.llm, dry_run=True)
    conductor = Conductor(llm=llm, ws_server=ws_mock, bus=bus)

    with patch.object(conductor, "_generate_turn", new_callable=AsyncMock) as mock_gen:
        event = SystemSpeech(audio_data=b"", text="Какая классная девчонка на стриме!", duration_s=1.5)
        await conductor.handle_system_speech(event)
        assert mock_gen.called
        kwargs = mock_gen.call_args.kwargs
        prompt = kwargs.get("user_text", "")
        assert "хвалит другую девушку, стримершу или ИИ" in prompt
        assert "<emo=pout:0.9>" in prompt
