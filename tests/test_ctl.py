"""Unit tests for Phase 9 vtuberctl and REST API endpoints."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web

from daemon.bus import EventBus, UserCommand
from daemon.ctl import build_parser
from daemon.memory import MemoryManager
from daemon.ws_server import (
    handle_api_command,
    handle_api_memory,
    handle_api_memory_forget,
    handle_api_memory_wipe,
    handle_api_status,
)


def test_ctl_argparser() -> None:
    parser = build_parser()

    # status
    args = parser.parse_args(["status"])
    assert args.command == "status"

    # mute
    args = parser.parse_args(["mute", "on"])
    assert args.command == "mute"
    assert args.state == "on"

    # blind
    args = parser.parse_args(["blind", "off"])
    assert args.command == "blind"
    assert args.state == "off"

    # dnd
    args = parser.parse_args(["dnd", "on"])
    assert args.command == "dnd"
    assert args.state == "on"

    # say
    args = parser.parse_args(["say", "Привет", "мир!"])
    assert args.command == "say"
    assert args.text == ["Привет", "мир!"]

    # ask
    args = parser.parse_args(["ask", "Что", "нового?"])
    assert args.command == "ask"
    assert args.text == ["Что", "нового?"]

    # look
    args = parser.parse_args(["look", "screen"])
    assert args.command == "look"
    assert args.target == "screen"

    # mood
    args = parser.parse_args(["mood", "sassy"])
    assert args.command == "mood"
    assert args.mood == "sassy"

    # hide / show
    args = parser.parse_args(["hide"])
    assert args.command == "hide"

    args = parser.parse_args(["show"])
    assert args.command == "show"

    # stop
    args = parser.parse_args(["stop"])
    assert args.command == "stop"

    # memory
    args = parser.parse_args(["memory", "list"])
    assert args.command == "memory"
    assert args.subaction == "list"

    args = parser.parse_args(["memory", "forget", "42"])
    assert args.command == "memory"
    assert args.subaction == "forget"
    assert args.fact_id == 42


@pytest.mark.asyncio
async def test_api_status_endpoint() -> None:
    app = web.Application()
    conductor = MagicMock()
    conductor.state = "speaking"
    conductor.llm.budget.daily_spent = 0.12
    conductor.llm.cfg.max_usd_per_day = 3.0

    initiative = MagicMock()
    initiative.mood.value = "playful"
    initiative.energy = 0.85
    initiative.dnd = False
    initiative.muted = False

    app["conductor"] = conductor
    app["initiative"] = initiative

    req = MagicMock(spec=web.Request)
    req.app = app

    resp = await handle_api_status(req)
    assert resp.status == 200


@pytest.mark.asyncio
async def test_api_command_endpoint() -> None:
    app = web.Application()
    bus = EventBus()
    ws_server = MagicMock()
    ws_server.broadcast = AsyncMock()

    app["bus"] = bus
    app["ws_server"] = ws_server

    captured_cmds: list[UserCommand] = []

    async def on_user_cmd(event: UserCommand) -> None:
        captured_cmds.append(event)

    bus.subscribe(UserCommand, on_user_cmd)

    req = MagicMock(spec=web.Request)
    req.app = app
    req.json = AsyncMock(return_value={"command": "mood", "args": {"mood": "curious"}})

    resp = await handle_api_command(req)
    assert resp.status == 200
    assert len(captured_cmds) == 1
    assert captured_cmds[0].command == "mood"
    assert captured_cmds[0].args == {"mood": "curious"}


@pytest.mark.asyncio
async def test_api_memory_endpoints(tmp_path) -> None:
    app = web.Application()
    mem = MemoryManager(db_path=str(tmp_path / "mem_api.db"))
    await mem.add_fact("Любит пить кофе утром")
    app["memory"] = mem

    req = MagicMock(spec=web.Request)
    req.app = app

    # 1. GET /api/memory
    resp = await handle_api_memory(req)
    assert resp.status == 200

    # 2. POST /api/memory/forget
    facts = await mem.get_facts()
    fact_id = facts[0].id
    req.json = AsyncMock(return_value={"id": fact_id})
    resp_forget = await handle_api_memory_forget(req)
    assert resp_forget.status == 200
    assert len(await mem.get_facts()) == 0

    # 3. POST /api/memory/wipe
    resp_wipe = await handle_api_memory_wipe(req)
    assert resp_wipe.status == 200

    await mem.close()
