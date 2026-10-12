"""Unit tests for WebSocket server and protocol handling."""

import json

import pytest
from aiohttp import web

from daemon.bus import EventBus, PlaybackEnded, PlaybackStarted, UserCommand
from daemon.config import Config
from daemon.ws_server import WebSocketServer, setup_routes


@pytest.mark.asyncio
async def test_ws_server_handshake_and_protocol(aiohttp_client) -> None:
    cfg = Config()
    bus = EventBus()
    app = web.Application()
    ws_server = WebSocketServer(cfg, bus)
    setup_routes(app, ws_server)

    events: list[object] = []

    async def on_playback_started(event: PlaybackStarted) -> None:
        events.append(event)

    async def on_playback_ended(event: PlaybackEnded) -> None:
        events.append(event)

    async def on_user_cmd(event: UserCommand) -> None:
        events.append(event)

    bus.subscribe(PlaybackStarted, on_playback_started)
    bus.subscribe(PlaybackEnded, on_playback_ended)
    bus.subscribe(UserCommand, on_user_cmd)

    client = await aiohttp_client(app)
    async with client.ws_connect("/ws") as ws:
        # First message: config
        msg1 = await ws.receive_json()
        assert msg1["type"] == "config"
        assert msg1["fps"] == 30

        # Second message: state
        msg2 = await ws.receive_json()
        assert msg2["type"] == "state"
        assert msg2["value"] == "idle"

        # Client sends playback_started
        await ws.send_str(json.dumps({"type": "playback_started", "id": "utt_1"}))
        # Client sends playback_ended
        await ws.send_str(json.dumps({"type": "playback_ended", "id": "utt_1"}))
        # Client sends click
        await ws.send_str(json.dumps({"type": "click", "region": "head"}))

        # Yield to let bus process async handlers
        import asyncio
        await asyncio.sleep(0.05)

    assert any(isinstance(e, PlaybackStarted) and e.utterance_id == "utt_1" for e in events)
    assert any(isinstance(e, PlaybackEnded) and e.utterance_id == "utt_1" for e in events)
    assert any(isinstance(e, UserCommand) and e.args.get("region") == "head" for e in events)
