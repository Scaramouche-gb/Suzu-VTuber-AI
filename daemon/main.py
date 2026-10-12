"""Main daemon entrypoint for Suzu VTuber AI."""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from aiohttp import web

from daemon.bus import EventBus
from daemon.config import Config, load_config

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


def create_app(cfg: Config, bus: EventBus) -> web.Application:
    """Create and configure aiohttp web application."""
    app = web.Application()
    app["config"] = cfg
    app["bus"] = bus

    app.router.add_get("/health", handle_health)
    return app


async def run_server(cfg: Config, bus: EventBus) -> None:
    """Run aiohttp application with graceful shutdown."""
    app = create_app(cfg, bus)
    runner = web.AppRunner(app)
    await runner.setup()

    site = web.TCPSite(runner, host=cfg.server.host, port=cfg.server.port)
    await site.start()
    logger.info("Daemon web server listening on http://%s:%d", cfg.server.host, cfg.server.port)

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except (NotImplementedError, RuntimeError):
            pass

    try:
        await stop_event.wait()
    finally:
        logger.info("Shutting down daemon server...")
        await runner.cleanup()
        logger.info("Daemon server stopped")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Suzu AI-VTuber Daemon")
    parser.add_argument("--config", type=str, default="config.toml", help="Path to config.toml")
    parser.add_argument("--text", action="store_true", help="Run in interactive text mode (Phase 2)")
    parser.add_argument("--dry-run-llm", action="store_true", help="Mock LLM responses for testing")
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
        asyncio.run(run_server(cfg, bus))
    except (KeyboardInterrupt, asyncio.CancelledError):
        logger.info("Daemon terminated by user")


if __name__ == "__main__":
    main()
