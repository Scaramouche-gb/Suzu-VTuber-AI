"""Vision engine capturing screen via grim, driftwm IPC state parsing, diff detection and privacy filtering."""

from __future__ import annotations

import asyncio
import fnmatch
import io
import logging
import re
import shutil
import time
from collections import deque
from dataclasses import dataclass

import imagehash
import numpy as np
from PIL import Image

from daemon.bus import EventBus, ScreenChange
from daemon.config import VisionConfig

logger = logging.getLogger(__name__)

DRIFT_ACTIVE_WINDOW_RE = re.compile(r"^\s*\*\s*#\d+\s+([^\s]+)\s+\[[^\]]+\]\s+\S+\s+\"(.*)\"$")
DRIFT_ACTIVE_OUTPUT_RE = re.compile(r"^\s*\*\s*([^\s]+)\s+camera\s+")


@dataclass(slots=True)
class WindowInfo:
    app_id: str = ""
    title: str = ""
    is_active: bool = False


@dataclass(slots=True)
class FrameRecord:
    timestamp: float
    jpeg_bytes: bytes
    app_id: str
    title: str
    diff_ratio: float


class VisionManager:
    """Manages Wayland screen capture, driftwm state inspection, diffs, and privacy."""

    def __init__(self, cfg: VisionConfig, bus: EventBus) -> None:
        self.cfg = cfg
        self.bus = bus
        self.blind_mode = False
        self._running = False
        self._worker_task: asyncio.Task[None] | None = None

        # Ring buffer of recent frames in memory (last ~30 seconds)
        self.frame_buffer: deque[FrameRecord] = deque(maxlen=30)
        self.screen_context: str = "экран в покое"
        self.active_app: str = ""
        self.active_title: str = ""

        # Tracking metrics
        self._last_small_img: np.ndarray | None = None
        self._last_phash: imagehash.ImageHash | None = None
        self._last_trigger_time: float = 0.0
        self._vlm_calls_this_hour: int = 0
        self._hour_start_time: float = time.time()

        self._consecutive_heavy_diffs = 0
        self._is_video_mode = False

    async def get_driftwm_state(self) -> tuple[str, str, str]:
        """Query driftwm msg state and parse active app, active title, active monitor."""
        if not shutil.which("driftwm"):
            return "", "", ""

        try:
            proc = await asyncio.create_subprocess_exec(
                "driftwm",
                "msg",
                "state",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=1.5)
            text = stdout.decode("utf-8", errors="replace")

            active_app = ""
            active_title = ""
            active_output = ""

            for line in text.splitlines():
                win_match = DRIFT_ACTIVE_WINDOW_RE.match(line)
                if win_match:
                    active_app = win_match.group(1)
                    active_title = win_match.group(2)
                    continue

                out_match = DRIFT_ACTIVE_OUTPUT_RE.match(line)
                if out_match:
                    active_output = out_match.group(1)

            return active_app, active_title, active_output
        except (OSError, TimeoutError, UnicodeDecodeError) as exc:
            logger.debug("Failed to query driftwm state: %s", exc)
            return "", "", ""

    def is_blacklisted(self, app_id: str, title: str) -> bool:
        """Check if active window violates blacklist constraints."""
        clean_app = app_id.lower()
        clean_title = title.lower()

        # Check app_ids
        for b_app in self.cfg.blacklist_app_ids:
            if b_app.lower() in clean_app:
                return True

        # Check title globs
        for b_glob in self.cfg.blacklist_title_globs:
            if fnmatch.fnmatch(clean_title, b_glob.lower()):
                return True

        return False

    async def capture_jpeg(self, output_name: str | None = None) -> bytes:
        """Capture screenshot via grim into memory bytes."""
        if not shutil.which("grim"):
            return b""

        target_out = output_name or self.cfg.output or "eDP-1"
        cmd = [
            "grim",
            "-o",
            target_out,
            "-s",
            str(self.cfg.scale),
            "-t",
            "jpeg",
            "-q",
            str(self.cfg.jpeg_quality),
            "-",
        ]

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=2.0)
            return stdout
        except (OSError, TimeoutError) as exc:
            logger.warning("grim capture failed: %s", exc)
            return b""

    def set_blind_mode(self, enabled: bool) -> None:
        self.blind_mode = enabled
        logger.info("Vision blind mode %s", "ENABLED" if enabled else "DISABLED")

    def start(self) -> None:
        if not self._running:
            self._running = True
            self._worker_task = asyncio.create_task(self._vision_loop())
            logger.info("VisionManager started (interval: %.1fs)", self.cfg.interval_s)

    async def stop(self) -> None:
        self._running = False
        if self._worker_task and not self._worker_task.done():
            self._worker_task.cancel()
            self._worker_task = None
        logger.info("VisionManager stopped")

    async def _vision_loop(self) -> None:
        while self._running:
            try:
                await self._process_tick()
            except asyncio.CancelledError:
                break
            except (OSError, RuntimeError, ValueError) as exc:
                logger.warning("Error in vision loop: %s", exc)

            await asyncio.sleep(self.cfg.interval_s)

    async def _process_tick(self) -> None:
        # Check hourly rate limit reset
        now = time.time()
        if now - self._hour_start_time >= 3600:
            self._hour_start_time = now
            self._vlm_calls_this_hour = 0

        # Blind mode check
        if self.blind_mode:
            self.screen_context = "экран скрыт (слепая зона)"
            return

        # Query active window and output
        app_id, title, output_name = await self.get_driftwm_state()
        self.active_app = app_id
        self.active_title = title

        # Blacklist check
        if self.is_blacklisted(app_id, title):
            self.screen_context = f"экран скрыт (чёрный список: {app_id})"
            return

        # Layer 0: Capture JPEG and compute pixel diff
        jpeg_data = await self.capture_jpeg(output_name)
        if not jpeg_data:
            return

        try:
            img = Image.open(io.BytesIO(jpeg_data)).convert("L")
            small_img = img.resize((64, 36), Image.Resampling.BILINEAR)
            small_arr = np.array(small_img, dtype=np.int16)
            current_phash = imagehash.phash(small_img)
        except (OSError, ValueError) as exc:
            logger.debug("Failed to process image frame: %s", exc)
            return

        diff_ratio = 0.0
        hash_diff = 0
        if self._last_small_img is not None and self._last_phash is not None:
            pixel_diff = np.abs(small_arr - self._last_small_img)
            significant_pixels = np.count_nonzero(pixel_diff > 16)
            diff_ratio = significant_pixels / float(small_arr.size)
            hash_diff = current_phash - self._last_phash

        self._last_small_img = small_arr
        self._last_phash = current_phash

        # Save to ring buffer
        rec = FrameRecord(
            timestamp=now,
            jpeg_bytes=jpeg_data,
            app_id=app_id,
            title=title,
            diff_ratio=diff_ratio,
        )
        self.frame_buffer.append(rec)

        # Detect video / gaming activity (continuous heavy diffs)
        if diff_ratio > 0.25:
            self._consecutive_heavy_diffs += 1
            if self._consecutive_heavy_diffs >= 5:
                self._is_video_mode = True
                self.screen_context = (
                    f"пользователь смотрит видео или динамичный контент в {app_id}"
                )
        else:
            self._consecutive_heavy_diffs = 0
            self._is_video_mode = False
            if app_id:
                self.screen_context = f"активно окно {app_id}: «{title[:40]}»"
            else:
                self.screen_context = "экран в покое"

        # Check trigger: noticeable stabilized change outside video mode
        if (
            not self._is_video_mode
            and (diff_ratio >= self.cfg.change_threshold or hash_diff >= 8)
            and (now - self._last_trigger_time >= self.cfg.send_cooldown_s)
            and (self._vlm_calls_this_hour < self.cfg.max_vlm_calls_per_hour)
        ):
            self._last_trigger_time = now
            self._vlm_calls_this_hour += 1
            logger.info(
                "Screen noticeably changed (diff=%.2f, hash=%d, app=%s)",
                diff_ratio,
                hash_diff,
                app_id,
            )
            await self.bus.publish(
                ScreenChange(
                    image_bytes=jpeg_data,
                    diff_ratio=diff_ratio,
                    active_app=app_id,
                    active_title=title,
                )
            )

    def get_latest_frame(self) -> bytes | None:
        """Return the most recent frame from memory buffer."""
        if self.frame_buffer:
            return self.frame_buffer[-1].jpeg_bytes
        return None
