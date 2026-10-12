"""Unit tests for Phase 7 initiative, liveliness, and proactive triggers."""

from __future__ import annotations

import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from daemon.bus import EventBus
from daemon.conductor import Conductor
from daemon.config import InitiativeConfig, LLMConfig
from daemon.initiative import (
    InitiativeManager,
)
from daemon.llm import LLMClient
from daemon.ws_server import WebSocketServer


def test_hazard_rate_calculation() -> None:
    cfg = InitiativeConfig(mean_interval_s=100.0, backoff_factor=2.0, max_backoff_s=1000.0)
    mgr = InitiativeManager(cfg)

    # Initial tick
    p1, interval1 = mgr.calculate_hazard_rate(dt=5.0)
    assert 0.0 < p1 < 1.0
    # playful mood factor is 0.8, energy=1.0 -> 100 * 0.8 = 80.0
    assert interval1 == pytest.approx(80.0)

    # Backoff with ignored count
    mgr.ignored_count = 2
    p2, interval2 = mgr.calculate_hazard_rate(dt=5.0)
    # backoff = 2^2 = 4 -> 80 * 4 = 320.0
    assert interval2 == pytest.approx(320.0)
    assert p2 < p1


def test_quiet_hours_intervals() -> None:
    cfg = InitiativeConfig(quiet_hours=["01:00-08:00", "23:00-00:30"])
    mgr = InitiativeManager(cfg)

    # Normal daytime (14:30) -> False
    dt_day = datetime.datetime(2026, 10, 13, 14, 30)
    assert mgr.is_in_quiet_hours(dt_day) is False

    # Quiet morning (04:15) -> True
    dt_morning = datetime.datetime(2026, 10, 13, 4, 15)
    assert mgr.is_in_quiet_hours(dt_morning) is True

    # Quiet midnight crossing (23:45) -> True
    dt_night = datetime.datetime(2026, 10, 13, 23, 45)
    assert mgr.is_in_quiet_hours(dt_night) is True


def test_hard_prohibitions() -> None:
    cfg = InitiativeConfig(min_gap_s=30.0, quiet_hours=[])
    mgr = InitiativeManager(cfg)

    # 1. Disabled
    mgr.config.enabled = False
    d = mgr.evaluate_tick(5.0, False, False, False, False)
    assert not d.should_speak
    assert d.reason == "initiative_disabled"
    mgr.config.enabled = True

    # 2. DND
    mgr.set_dnd(True)
    d = mgr.evaluate_tick(5.0, False, False, False, False)
    assert not d.should_speak
    assert d.reason == "dnd_enabled"
    mgr.set_dnd(False)

    # 3. Muted
    mgr.set_muted(True)
    d = mgr.evaluate_tick(5.0, False, False, False, False)
    assert not d.should_speak
    assert d.reason == "muted"
    mgr.set_muted(False)

    # 4. User speaking
    d = mgr.evaluate_tick(5.0, is_user_speaking=True, is_avatar_speaking=False, is_llm_busy=False, budget_exceeded=False)
    assert not d.should_speak
    assert d.reason == "user_is_speaking"

    # 5. Avatar speaking
    d = mgr.evaluate_tick(5.0, is_user_speaking=False, is_avatar_speaking=True, is_llm_busy=False, budget_exceeded=False)
    assert not d.should_speak
    assert d.reason == "avatar_is_speaking"

    # 6. LLM busy
    d = mgr.evaluate_tick(5.0, is_user_speaking=False, is_avatar_speaking=False, is_llm_busy=True, budget_exceeded=False)
    assert not d.should_speak
    assert d.reason == "llm_is_busy"

    # 7. Budget exceeded
    d = mgr.evaluate_tick(5.0, is_user_speaking=False, is_avatar_speaking=False, is_llm_busy=False, budget_exceeded=True)
    assert not d.should_speak
    assert d.reason == "budget_exceeded"

    # 8. Min gap
    mgr.last_avatar_speech_ts = mgr.last_avatar_speech_ts  # recent
    d = mgr.evaluate_tick(5.0, False, False, False, False)
    assert not d.should_speak
    assert "min_gap_not_reached" in d.reason


def test_app_switch_trigger() -> None:
    cfg = InitiativeConfig(min_gap_s=0.0, quiet_hours=[])
    mgr = InitiativeManager(cfg)
    mgr.last_avatar_speech_ts = 0.0

    d = mgr.evaluate_tick(
        dt=5.0,
        is_user_speaking=False,
        is_avatar_speaking=False,
        is_llm_busy=False,
        budget_exceeded=False,
        active_app="code",
    )
    assert d.should_speak is True
    assert "app_switch_trigger" in d.reason
    assert d.seed == "tease_screen"


@pytest.mark.asyncio
async def test_conductor_proactive_silent() -> None:
    bus = EventBus()
    llm = LLMClient(LLMConfig(), dry_run=True)
    ws = MagicMock(spec=WebSocketServer)
    ws.broadcast = AsyncMock()
    ws.set_state = MagicMock()

    cfg_init = InitiativeConfig(min_gap_s=0.0)
    init_mgr = InitiativeManager(cfg_init)

    # Mock stream to output only <silent>
    async def mock_silent(*args, **kwargs):
        yield "<silent>"

    llm.stream_response = mock_silent  # type: ignore

    conductor = Conductor(
        llm=llm,
        ws_server=ws,
        bus=bus,
        initiative=init_mgr,
    )

    await conductor.process_proactive_turn(seed="random_thought")
    # No say broadcast, no history added
    assert len(conductor.history) == 0
    await conductor.close()


@pytest.mark.asyncio
async def test_conductor_proactive_speaking() -> None:
    bus = EventBus()
    llm = LLMClient(LLMConfig(), dry_run=True)
    ws = MagicMock(spec=WebSocketServer)
    ws.broadcast = AsyncMock()
    ws.set_state = MagicMock()

    cfg_init = InitiativeConfig(min_gap_s=0.0)
    init_mgr = InitiativeManager(cfg_init)

    async def mock_reply(*args, **kwargs):
        yield "<emo=happy:0.7> "
        yield "Хороший день для кодинга!"

    llm.stream_response = mock_reply  # type: ignore

    conductor = Conductor(
        llm=llm,
        ws_server=ws,
        bus=bus,
        initiative=init_mgr,
    )

    await conductor.process_proactive_turn(seed="tease_screen")
    assert len(conductor.history) == 1
    assert conductor.history[0]["role"] == "assistant"
    assert "Хороший день для кодинга!" in conductor.history[0]["content"]
    assert init_mgr.ignored_count == 1
    await conductor.close()
