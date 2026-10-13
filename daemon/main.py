"""Main daemon entrypoint for Suzu VTuber AI."""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from aiohttp import web

from daemon.audio_in import AudioInManager
from daemon.bus import EventBus
from daemon.conductor import Conductor
from daemon.config import Config, load_config
from daemon.initiative import InitiativeManager
from daemon.llm import LLMClient
from daemon.memory import MemoryManager
from daemon.stt import MockSTT, WhisperSTT
from daemon.tts import create_tts_engine
from daemon.vision import VisionManager
from daemon.ws_server import WebSocketServer, setup_routes

logger = logging.getLogger("vtuber")

DEFAULT_LOG_DIR = Path.home() / ".local" / "state" / "vtuber"
DEFAULT_LOG_FILE = DEFAULT_LOG_DIR / "daemon.log"


def setup_logging(level_name: str = "INFO") -> None:
    """Configure console and rotating file logging."""
    level = getattr(logging, level_name.upper(), logging.INFO)
    root = logging.getLogger()
    root.setLevel(level)

    # Clear existing handlers
    for handler in list(root.handlers):
        root.removeHandler(handler)

    formatter = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Console Handler (stderr)
    console_handler = logging.StreamHandler(sys.stderr)
    console_handler.setFormatter(formatter)
    console_handler.setLevel(level)
    root.addHandler(console_handler)

    # Rotating File Handler
    try:
        DEFAULT_LOG_DIR.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            filename=DEFAULT_LOG_FILE,
            maxBytes=10 * 1024 * 1024,  # 10 MB
            backupCount=5,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        file_handler.setLevel(level)
        root.addHandler(file_handler)
    except OSError as exc:
        logger.warning("Failed to initialize rotating file logger at %s: %s", DEFAULT_LOG_FILE, exc)


async def handle_health(request: web.Request) -> web.Response:
    """Health check endpoint."""
    return web.Response(text="ok", content_type="text/plain")


@web.middleware
async def cors_middleware(request: web.Request, handler: Any) -> web.Response:
    """Allow CORS for local web renderer on port 5173 or overlay webviews."""
    if request.method == "OPTIONS":
        response = web.Response()
    else:
        try:
            response = await handler(request)
        except web.HTTPException as ex:
            response = ex
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = (
        "Content-Type, Authorization, X-Requested-With"
    )
    return response


def create_app(cfg: Config, bus: EventBus) -> tuple[web.Application, WebSocketServer]:
    """Create and configure aiohttp web application with WebSocket server."""
    app = web.Application(middlewares=[cors_middleware])
    app["config"] = cfg
    app["bus"] = bus

    app.router.add_get("/health", handle_health)

    ws_server = WebSocketServer(cfg, bus)
    setup_routes(app, ws_server)
    app["ws_server"] = ws_server

    return app, ws_server


async def text_repl_loop(conductor: Conductor, stop_event: asyncio.Event) -> None:
    """Interactive command-line loop for text chat mode (Phase 2)."""
    print("\n=== Suzu VTuber Text Mode ===")
    print("Вводите сообщения в терминале. Для выхода нажмите Ctrl+C или введите 'exit'.\n")

    while not stop_event.is_set():
        try:
            line = await asyncio.to_thread(
                input, f"[{conductor.llm.cfg.chat_model.split('/')[-1]}] Вы: "
            )
            text = line.strip()
            if not text:
                continue
            if text.lower() in ("exit", "quit"):
                stop_event.set()
                break

            await conductor.process_user_turn(text)
        except (EOFError, KeyboardInterrupt):
            stop_event.set()
            break
        except asyncio.CancelledError:
            break


async def run_server(
    cfg: Config,
    bus: EventBus,
    text_mode: bool = False,
    dry_run: bool = False,
    no_mic: bool = False,
    with_overlay: bool = False,
) -> None:
    """Run aiohttp application with graceful shutdown and optional CLI loop."""
    app, ws_server = create_app(cfg, bus)

    # Initialize STT, Audio In and Vision
    stt = MockSTT() if dry_run else WhisperSTT(cfg.stt)
    audio_in = AudioInManager(cfg.audio_in, bus)
    vision = VisionManager(cfg.vision, bus)

    # Initialize LLM client, TTS engine, memory, initiative and Conductor
    llm = LLMClient(cfg.llm, dry_run=dry_run)
    tts = create_tts_engine(cfg.tts, dry_run=dry_run)
    memory = MemoryManager(
        db_path=cfg.memory.db_path,
        context_turns=cfg.memory.context_turns,
        summarize_every=cfg.memory.summarize_every,
    )
    initiative = InitiativeManager(cfg.initiative)
    conductor = Conductor(
        llm=llm,
        ws_server=ws_server,
        bus=bus,
        tts=tts,
        stt=stt,
        vision=vision,
        initiative=initiative,
        memory=memory,
    )

    app["conductor"] = conductor
    app["audio_in"] = audio_in
    app["vision"] = vision
    app["initiative"] = initiative
    app["memory"] = memory

    runner = web.AppRunner(app)
    await runner.setup()

    site = web.TCPSite(runner, host=cfg.server.host, port=cfg.server.port)
    await site.start()
    logger.info("Daemon web server listening on http://%s:%d", cfg.server.host, cfg.server.port)

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    if not no_mic:
        audio_in.start(loop)
    if cfg.vision.enabled:
        vision.start()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except (NotImplementedError, RuntimeError):
            pass

    overlay_proc: asyncio.subprocess.Process | None = None
    if with_overlay:
        overlay_proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "daemon.overlay",
        )

    repl_task: asyncio.Task[None] | None = None
    if text_mode:
        repl_task = asyncio.create_task(text_repl_loop(conductor, stop_event))

    try:
        await stop_event.wait()
    finally:
        if overlay_proc and overlay_proc.returncode is None:
            overlay_proc.terminate()
        if repl_task and not repl_task.done():
            repl_task.cancel()
        if not no_mic:
            await audio_in.stop()
        if cfg.vision.enabled:
            await vision.stop()
        await conductor.close()
        logger.info("Shutting down daemon server...")
        await runner.cleanup()
        logger.info("Daemon server stopped")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Suzu AI-VTuber Daemon")
    parser.add_argument("--config", type=str, default="config.toml", help="Path to config.toml")
    parser.add_argument(
        "--text", action="store_true", help="Run in interactive text mode (Phase 2)"
    )
    parser.add_argument("--dry-run-llm", action="store_true", help="Mock LLM responses for testing")
    parser.add_argument("--no-mic", action="store_true", help="Disable microphone input capture")
    parser.add_argument(
        "--overlay", action="store_true", help="Launch desktop transparent overlay window"
    )
    return parser.parse_args()


def main() -> None:
    """Daemon entrypoint."""
    args = parse_args()
    cfg = load_config(args.config)
    setup_logging(cfg.general.log_level)

    logger.info("Starting Suzu AI-VTuber Daemon for '%s'...", cfg.general.name)
    logger.info("Log file: %s", DEFAULT_LOG_FILE)

    bus = EventBus()

    try:
        asyncio.run(
            run_server(
                cfg,
                bus,
                text_mode=args.text,
                dry_run=args.dry_run_llm,
                no_mic=args.no_mic,
                with_overlay=args.overlay,
            )
        )
    except (KeyboardInterrupt, asyncio.CancelledError):
        logger.info("Daemon terminated by user")


if __name__ == "__main__":
    main()
