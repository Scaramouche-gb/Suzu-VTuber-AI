"""WebSocket server and static file handler for Suzu VTuber AI daemon."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

from aiohttp import WSMsgType, web

from daemon.bus import EventBus, PlaybackEnded, PlaybackStarted, UserCommand
from daemon.config import Config

logger = logging.getLogger(__name__)

RENDERER_DIST_DIR = Path("renderer/dist")
DEFAULT_AUDIO_CACHE = Path.home() / ".cache" / "vtuber" / "audio"


class WebSocketServer:
    """Manages WebSocket connections between daemon and frontend renderer."""

    def __init__(self, cfg: Config, bus: EventBus) -> None:
        self.cfg = cfg
        self.bus = bus
        self._clients: set[web.WebSocketResponse] = set()
        self.current_state: str = "idle"

    @property
    def clients(self) -> set[web.WebSocketResponse]:
        """Set of active connected WebSocket clients."""
        return self._clients

    @property
    def client_count(self) -> int:
        """Number of active connected WebSocket clients."""
        return len(self._clients)

    async def broadcast(self, message: dict[str, Any]) -> None:
        """Broadcast JSON payload to all connected renderers."""
        if not self._clients:
            return

        payload = json.dumps(message)
        dead_clients: list[web.WebSocketResponse] = []

        for ws in self._clients:
            try:
                if not ws.closed:
                    await ws.send_str(payload)
                else:
                    dead_clients.append(ws)
            except (ConnectionResetError, RuntimeError, OSError) as exc:
                logger.warning("Error sending to WS client: %s", exc)
                dead_clients.append(ws)

        for dead in dead_clients:
            self._clients.discard(dead)

    async def handle_ws(self, request: web.Request) -> web.WebSocketResponse:
        """Handle incoming WebSocket connection from renderer."""
        ws = web.WebSocketResponse(heartbeat=15.0)
        await ws.prepare(request)

        self._clients.add(ws)
        logger.info("Renderer connected via WebSocket (%d active clients)", len(self._clients))

        # Send initial config and state
        await ws.send_str(json.dumps({
            "type": "config",
            "scale": self.cfg.avatar.scale,
            "y_offset": self.cfg.avatar.y_offset,
            "fps": self.cfg.avatar.fps,
        }))
        await ws.send_str(json.dumps({
            "type": "state",
            "value": self.current_state,
        }))

        try:
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    await self._process_client_message(msg.data)
                elif msg.type == WSMsgType.ERROR:
                    logger.warning("WS connection error: %s", ws.exception())
        finally:
            self._clients.discard(ws)
            logger.info("Renderer disconnected from WebSocket (%d remaining)", len(self._clients))

        return ws

    async def _process_client_message(self, raw_text: str) -> None:
        try:
            data = json.loads(raw_text)
        except json.JSONDecodeError:
            logger.warning("Received invalid JSON from renderer: %s", raw_text)
            return

        msg_type = data.get("type")
        if msg_type == "ready":
            logger.info("Renderer reported READY")
            await self.broadcast({"type": "state", "value": self.current_state})

        elif msg_type == "playback_started":
            utterance_id = data.get("id", "")
            logger.debug("Renderer started playback: %s", utterance_id)
            await self.bus.publish(PlaybackStarted(utterance_id=utterance_id))

        elif msg_type == "playback_ended":
            utterance_id = data.get("id", "")
            logger.debug("Renderer finished playback: %s", utterance_id)
            await self.bus.publish(PlaybackEnded(utterance_id=utterance_id))

        elif msg_type == "click":
            region = data.get("region", "body")
            logger.info("User clicked on avatar (%s)", region)
            await self.bus.publish(UserCommand(command="click", args={"region": region}))

        else:
            logger.debug("Received unknown message type from renderer: %s", msg_type)

    def set_state(self, new_state: str) -> asyncio.Task[None]:
        """Update and broadcast daemon state (idle|listening|thinking|speaking)."""
        self.current_state = new_state
        return asyncio.create_task(self.broadcast({"type": "state", "value": new_state}))


async def handle_api_status(request: web.Request) -> web.Response:
    """REST API endpoint returning daemon status."""
    conductor = request.app.get("conductor")
    initiative = request.app.get("initiative")
    vision = request.app.get("vision")

    status_data = {
        "state": conductor.state if conductor else "idle",
        "mood": initiative.mood.value if initiative else "unknown",
        "energy": initiative.energy if initiative else 1.0,
        "dnd": initiative.dnd if initiative else False,
        "muted": initiative.muted if initiative else False,
        "blind": vision.blind_mode if vision else False,
        "active_app": vision.active_app if vision else "",
        "active_title": vision.active_title if vision else "",
        "screen_context": vision.screen_context if vision else "",
        "daily_cost": conductor.llm.budget.daily_spent if (conductor and conductor.llm) else 0.0,
        "max_usd_per_day": conductor.llm.cfg.max_usd_per_day if (conductor and conductor.llm) else 0.0,
    }
    return web.json_response(status_data)


async def handle_api_command(request: web.Request) -> web.Response:
    """REST API endpoint receiving control commands."""
    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        return web.json_response({"error": "Invalid JSON"}, status=400)

    cmd = body.get("command", "")
    args = body.get("args", {})
    bus: EventBus = request.app["bus"]
    ws_server: WebSocketServer = request.app["ws_server"]

    if cmd in ("hide", "show"):
        await ws_server.broadcast({"type": cmd})

    await bus.publish(UserCommand(command=cmd, args=args))
    return web.json_response({"status": "ok", "command": cmd})


async def handle_api_memory(request: web.Request) -> web.Response:
    """REST API endpoint returning memory facts and episodes."""
    memory = request.app.get("memory")
    if not memory:
        return web.json_response({"facts": [], "episodes": []})

    facts = await memory.list_facts()
    episodes = await memory.get_recent_episodes(limit=5)
    return web.json_response({
        "facts": facts,
        "episodes": [{"id": e.id, "summary": e.summary} for e in episodes],
    })


async def handle_api_memory_forget(request: web.Request) -> web.Response:
    """REST API endpoint to delete a fact by ID."""
    memory = request.app.get("memory")
    if not memory:
        return web.json_response({"status": "error", "message": "Memory not configured"}, status=400)

    try:
        body = await request.json()
        fact_id = int(body.get("id", 0))
    except (ValueError, TypeError, json.JSONDecodeError):
        return web.json_response({"status": "error", "message": "Invalid ID"}, status=400)

    deleted = await memory.delete_fact(fact_id)
    return web.json_response({"status": "ok", "deleted": deleted})


async def handle_api_memory_wipe(request: web.Request) -> web.Response:
    """REST API endpoint to wipe all persistent memory."""
    memory = request.app.get("memory")
    if not memory:
        return web.json_response({"status": "error", "message": "Memory not configured"}, status=400)

    await memory.wipe_memory()
    return web.json_response({"status": "ok", "wiped": True})


def setup_routes(app: web.Application, ws_server: WebSocketServer) -> None:
    """Setup HTTP, REST API, and WebSocket routes on aiohttp web application."""
    app.router.add_get("/ws", ws_server.handle_ws)

    # REST Control API
    app.router.add_get("/api/status", handle_api_status)
    app.router.add_post("/api/command", handle_api_command)
    app.router.add_get("/api/memory", handle_api_memory)
    app.router.add_post("/api/memory/forget", handle_api_memory_forget)
    app.router.add_post("/api/memory/wipe", handle_api_memory_wipe)

    # Audio cache directory serving
    DEFAULT_AUDIO_CACHE.mkdir(parents=True, exist_ok=True)
    app.router.add_static("/audio", path=DEFAULT_AUDIO_CACHE, name="audio")

    # Static build of renderer if dist exists
    if RENDERER_DIST_DIR.exists():
        app.router.add_static("/", path=RENDERER_DIST_DIR, name="static", show_index=True)
