"""Unit tests for Conductor dialog state machine and turn processing."""

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from daemon.bus import EventBus, Interrupt, UserCommand
from daemon.coder import AntigravityBridge
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


@pytest.mark.asyncio
async def test_conductor_command_tag() -> None:
    cfg = Config()
    bus = EventBus()
    ws_mock = MockWebSocketServer(cfg, bus)
    llm = LLMClient(cfg.llm, dry_run=True)
    conductor = Conductor(llm=llm, ws_server=ws_mock, bus=bus)

    # Simulate command tag execution
    from daemon.sentence import SentenceSplitter
    from daemon.tagparser import CommandTag

    splitter = SentenceSplitter()
    await conductor._handle_tag(CommandTag(action="launch:telegram"), splitter)
    # Shouldn't raise any exception
    await conductor.close()


@pytest.mark.asyncio
async def test_conductor_is_stop_command() -> None:
    cfg = Config()
    bus = EventBus()
    ws_mock = MockWebSocketServer(cfg, bus)
    llm = LLMClient(cfg.llm, dry_run=True)
    conductor = Conductor(llm=llm, ws_server=ws_mock, bus=bus)

    # Positive matches
    assert conductor._is_stop_command("стоп")
    assert conductor._is_stop_command("Сузу, стоп!")
    assert conductor._is_stop_command("стоп машина")
    assert conductor._is_stop_command("хватит")
    assert conductor._is_stop_command("остановись")
    assert conductor._is_stop_command("быстро стоп проект")
    assert conductor._is_stop_command("стоп код")
    assert conductor._is_stop_command("стопни")
    assert conductor._is_stop_command("хватит кодить")
    assert conductor._is_stop_command("отмена")
    assert conductor._is_stop_command("отбой")

    # Negative matches (regular queries with conversational words)
    assert not conductor._is_stop_command("стоп, а как называется функция?")
    assert not conductor._is_stop_command("что такое стоп-сигнал?")
    assert not conductor._is_stop_command("привет Сузу")

    await conductor.close()


@pytest.mark.asyncio
async def test_conductor_fast_stop_aborts_active_coder() -> None:
    cfg = Config()
    bus = EventBus()
    ws_mock = MockWebSocketServer(cfg, bus)
    llm = LLMClient(cfg.llm, dry_run=True)
    coder_mock = MagicMock(spec=AntigravityBridge)
    coder_mock.is_busy.return_value = True
    coder_mock.cancel_current_task = AsyncMock(return_value=(True, "Остановила задачу"))

    conductor = Conductor(llm=llm, ws_server=ws_mock, bus=bus, coder=coder_mock)

    # Trigger fast emergency stop via user turn
    await conductor.process_user_turn("Сузу, стоп!")

    # Coding task must be canceled immediately
    coder_mock.cancel_current_task.assert_called_once()

    # WebSocket events must include stop and surprise
    types = [m["type"] for m in ws_mock.sent_messages]
    assert "stop" in types

    # Last assistant message should reflect emergency stop
    assert len(conductor.history) == 2
    assert "стопнула задачу в проекте" in conductor.history[-1]["content"].lower()

    await conductor.close()


@pytest.mark.asyncio
async def test_conductor_user_command_stop() -> None:
    cfg = Config()
    bus = EventBus()
    ws_mock = MockWebSocketServer(cfg, bus)
    llm = LLMClient(cfg.llm, dry_run=True)
    conductor = Conductor(llm=llm, ws_server=ws_mock, bus=bus)

    await conductor.handle_user_command(UserCommand(command="stop"))

    types = [m["type"] for m in ws_mock.sent_messages]
    assert "stop" in types
    assert len(conductor.history) == 2
    assert "стопнула" in conductor.history[-1]["content"].lower()

    await conductor.close()


@pytest.mark.asyncio
async def test_conductor_sentence_capping_dialogue_and_proactive() -> None:
    cfg = Config()
    bus = EventBus()
    ws_mock = MockWebSocketServer(cfg, bus)
    llm = LLMClient(cfg.llm, dry_run=True)
    conductor = Conductor(llm=llm, ws_server=ws_mock, bus=bus)

    # In dry-run mode, mock_stream produces multiple sentences.
    # Regular dialogue turn must cap at 2 sentences max.
    await conductor.process_user_turn("Сузу, привет!")
    say_msgs = [m for m in ws_mock.sent_messages if m["type"] == "say"]
    assert len(say_msgs) <= 2

    # Proactive turn must cap at 1 sentence max.
    ws_mock.sent_messages.clear()
    await conductor.process_proactive_turn(seed="geek_curiosity")
    proactive_say_msgs = [m for m in ws_mock.sent_messages if m["type"] == "say"]
    assert len(proactive_say_msgs) <= 1

    await conductor.close()

