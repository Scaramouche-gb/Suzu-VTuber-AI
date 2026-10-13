"""Unit tests for Antigravity (AGY) Autonomous Coding Agent integration."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from daemon.bus import EventBus
from daemon.coder import AntigravityBridge, CodingTask
from daemon.conductor import Conductor
from daemon.config import Config
from daemon.llm import LLMClient
from daemon.sentence import SentenceSplitter
from daemon.tagparser import CommandTag
from daemon.ws_server import WebSocketServer


class MockProcess:
    def __init__(self, returncode: int = 0, stdout: bytes = b"", stderr: bytes = b"") -> None:
        self.returncode: int | None = None
        self._exit_code = returncode
        self._stdout = stdout
        self._stderr = stderr
        self.terminated = False

    async def communicate(self) -> tuple[bytes, bytes]:
        self.returncode = self._exit_code
        return self._stdout, self._stderr

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15


class MockWebSocketServer(WebSocketServer):
    def __init__(self, cfg: Config, bus: EventBus) -> None:
        super().__init__(cfg, bus)
        self.sent_messages: list[dict[str, str]] = []

    async def broadcast(self, message: dict[str, str]) -> None:
        self.sent_messages.append(message)


@pytest.mark.asyncio
async def test_coder_bridge_idle_status() -> None:
    bridge = AntigravityBridge()
    assert not bridge.is_busy()
    status = bridge.get_status()
    assert status["active"] is False
    assert status["status"] == "idle"


@pytest.mark.asyncio
async def test_coder_bridge_successful_task() -> None:
    bridge = AntigravityBridge()
    mock_proc = MockProcess(
        returncode=0,
        stdout=b"Refactored conductor.py successfully.\n[Automatic Persistent Memory Context]: ignore me",
        stderr=b"",
    )

    callback_called = asyncio.Event()
    completed_task: CodingTask | None = None

    async def on_complete(task: CodingTask) -> None:
        nonlocal completed_task
        completed_task = task
        callback_called.set()

    with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=mock_proc)):
        ok, msg = await bridge.start_task("Refactor conductor", on_complete=on_complete)
        assert ok is True
        assert "Запустила задачу" in msg
        assert bridge.is_busy()

        await asyncio.wait_for(callback_called.wait(), timeout=2.0)

    assert completed_task is not None
    assert completed_task.status == "completed"
    assert "Refactored conductor.py successfully." in completed_task.summary
    assert "[Automatic Persistent Memory Context]" not in completed_task.summary
    assert not bridge.is_busy()


@pytest.mark.asyncio
async def test_coder_bridge_failed_task() -> None:
    bridge = AntigravityBridge()
    mock_proc = MockProcess(
        returncode=1,
        stdout=b"",
        stderr=b"SyntaxError in generated code",
    )

    callback_called = asyncio.Event()
    completed_task: CodingTask | None = None

    async def on_complete(task: CodingTask) -> None:
        nonlocal completed_task
        completed_task = task
        callback_called.set()

    with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=mock_proc)):
        ok, _ = await bridge.start_task("Fix bug", on_complete=on_complete)
        assert ok is True

        await asyncio.wait_for(callback_called.wait(), timeout=2.0)

    assert completed_task is not None
    assert completed_task.status == "failed"
    assert "SyntaxError" in completed_task.error


@pytest.mark.asyncio
async def test_coder_bridge_busy_and_cancel() -> None:
    bridge = AntigravityBridge()
    mock_proc = MockProcess(returncode=0, stdout=b"done")

    # Keep worker pending in communicate
    async def slow_communicate() -> tuple[bytes, bytes]:
        await asyncio.sleep(5.0)
        return b"done", b""

    mock_proc.communicate = slow_communicate  # type: ignore[assignment]

    with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=mock_proc)):
        ok, _ = await bridge.start_task("Long task")
        assert ok is True
        assert bridge.is_busy()
        await asyncio.sleep(0.01)

        # Starting second task while first is running should fail
        ok2, msg2 = await bridge.start_task("Second task")
        assert ok2 is False
        assert "уже занят" in msg2

        # Cancel current task
        c_ok, c_msg = await bridge.cancel_current_task()
        assert c_ok is True
        assert "Остановила" in c_msg
        assert mock_proc.terminated is True


@pytest.mark.asyncio
async def test_conductor_coder_intent_routing() -> None:
    cfg = Config()
    bus = EventBus()
    ws_mock = MockWebSocketServer(cfg, bus)
    llm = LLMClient(cfg.llm, dry_run=True)
    bridge_mock = MagicMock(spec=AntigravityBridge)
    bridge_mock.start_task = AsyncMock(return_value=(True, "Запустила задачу"))
    bridge_mock.get_status = MagicMock(
        return_value={"active": True, "elapsed_seconds": 12, "prompt": "тесты", "latest_snippet": "writing test"}
    )
    bridge_mock.cancel_current_task = AsyncMock(return_value=(True, "Остановила задачу"))

    conductor = Conductor(llm=llm, ws_server=ws_mock, bus=bus, coder=bridge_mock)

    # 1. Ask to code
    await conductor.process_user_turn("Сузу, попроси антигравити написать новые тесты")
    bridge_mock.start_task.assert_called_once()
    assert "тесты" in bridge_mock.start_task.call_args[1]["prompt"]

    # 2. Check status
    await conductor.process_user_turn("как там антигравити?")
    bridge_mock.get_status.assert_called()

    # 3. Stop coder
    await conductor.process_user_turn("стоп антигравити")
    bridge_mock.cancel_current_task.assert_called_once()

    await conductor.close()


@pytest.mark.asyncio
async def test_conductor_coder_command_tags() -> None:
    cfg = Config()
    bus = EventBus()
    ws_mock = MockWebSocketServer(cfg, bus)
    llm = LLMClient(cfg.llm, dry_run=True)
    bridge_mock = MagicMock(spec=AntigravityBridge)
    bridge_mock.start_task = AsyncMock(return_value=(True, "ok"))
    bridge_mock.cancel_current_task = AsyncMock(return_value=(True, "ok"))

    conductor = Conductor(llm=llm, ws_server=ws_mock, bus=bus, coder=bridge_mock)
    splitter = SentenceSplitter()

    await conductor._handle_tag(CommandTag(action="code:fix login form"), splitter)
    await asyncio.sleep(0.01)
    bridge_mock.start_task.assert_called_once()
    assert bridge_mock.start_task.call_args[0][0] == "fix login form"

    await conductor._handle_tag(CommandTag(action="code_cancel"), splitter)
    await asyncio.sleep(0.01)
    bridge_mock.cancel_current_task.assert_called_once()

    await conductor.close()
