"""Unit tests for Conductor dialog state machine and turn processing."""

import asyncio
from typing import Any

import pytest

from daemon.bus import EventBus, Interrupt
from daemon.conductor import Conductor
from daemon.config import Config
from daemon.llm import LLMClient
from daemon.ws_server import WebSocketServer


class MockWebSocketServer(WebSocketServer):
    def __init__(self, cfg: Config, bus: EventBus) -> None:
        super().__init__(cfg, bus)
        self.sent_messages: list[dict[str, Any]] = []

    async def broadcast(self, message: dict[str, Any]) -> None:
        self.sent_messages.append(message)


@pytest.mark.asyncio
async def test_conductor_process_user_turn() -> None:
    cfg = Config()
    bus = EventBus()
    ws_mock = MockWebSocketServer(cfg, bus)
    llm = LLMClient(cfg.llm, dry_run=True)
    conductor = Conductor(llm=llm, ws_server=ws_mock, bus=bus)

    await conductor.process_user_turn("Привет Сузу!")

    # Check history turns
    assert len(conductor.history) == 2
    assert conductor.history[0]["role"] == "user"
    assert conductor.history[0]["content"] == "Привет Сузу!"
    assert conductor.history[1]["role"] == "assistant"

    # Check that emotions, gestures and say were broadcast
    types = [m["type"] for m in ws_mock.sent_messages]
    assert "emotion" in types
    assert "gesture" in types
    assert "say" in types

    # Final state should return to idle
    assert conductor.state == "idle"


@pytest.mark.asyncio
async def test_conductor_barge_in_interrupt() -> None:
    cfg = Config()
    bus = EventBus()
    ws_mock = MockWebSocketServer(cfg, bus)
    llm = LLMClient(cfg.llm, dry_run=True)
    conductor = Conductor(llm=llm, ws_server=ws_mock, bus=bus)

    # Start turn in background
    turn_task = asyncio.create_task(conductor.process_user_turn("Привет Сузу!"))
    await asyncio.sleep(0.01)

    # Trigger interrupt (user started speaking)
    await bus.publish(Interrupt(reason="user_speaking"))

    # Turn should finish or cancel cleanly
    try:
        await turn_task
    except asyncio.CancelledError:
        pass

    # Stop command must be broadcast to avatar
    types = [m["type"] for m in ws_mock.sent_messages]
    assert "stop" in types
    assert conductor.state == "idle"
