"""Vision engine capturing screen via grim, driftwm IPC state parsing, diff detection and privacy filtering."""

from __future__ import annotations

import asyncio
import fnmatch
import io
import json
import logging
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
    window_jpeg_bytes: bytes | None = None


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
        self.active_geometry: str = ""
        self.other_apps: list[str] = []
        self.media_context: str = ""

        # Tracking metrics
        self._last_small_img: np.ndarray | None = None
        self._last_phash: imagehash.ImageHash | None = None
        self._last_trigger_time: float = 0.0
        self._vlm_calls_this_hour: int = 0
        self._hour_start_time: float = time.time()

        self._consecutive_heavy_diffs = 0
        self._is_video_mode = False

    @staticmethod
    def parse_driftwm_state_dict(data: dict) -> tuple[str, str, str, list[str]]:
        """Parse driftwm JSON state dict into (active_app, active_title, active_output, other_apps)."""
        state = data.get("Ok", {}).get("State", {}) if "Ok" in data else data.get("State", data)
        windows = state.get("windows", [])
        outputs = state.get("outputs", [])

        active_app = ""
        active_title = ""
        active_output = ""
        other_apps: list[str] = []

        for out in outputs:
            if out.get("active"):
                active_output = out.get("name", "")
                break

        for w in windows:
            if w.get("is_widget"):
                continue
            app_name = w.get("app_id", "")
            title_name = w.get("title", "")
            if w.get("is_focused"):
                active_app = app_name
                active_title = title_name
            else:
                if app_name:
                    desc = f"{app_name} («{title_name[:35]}»)" if title_name else app_name
                    if desc not in other_apps:
                        other_apps.append(desc)

        return active_app, active_title, active_output, other_apps

    async def get_driftwm_state(self) -> tuple[str, str, str, list[str]]:
        """Query driftwm msg state via JSON and parse active app, title, monitor, and background windows."""
        if not shutil.which("driftwm"):
            return "", "", "", []

        try:
            proc = await asyncio.create_subprocess_exec(
                "driftwm",
                "msg",
                "state",
                "--json",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=1.5)
            if proc.returncode != 0 or not stdout:
                return "", "", "", []

            data = json.loads(stdout.decode("utf-8", errors="replace"))
            active_app, active_title, active_output, other_apps = self.parse_driftwm_state_dict(data)

            # Extract window geometry for focused window
            state_data = data.get("Ok", {}).get("State", {}) if "Ok" in data else data.get("State", data)
            for w in state_data.get("windows", []):
                if w.get("is_focused") and not w.get("is_widget"):
                    pos = w.get("position")
                    size = w.get("size")
                    if pos and size and len(pos) >= 2 and len(size) >= 2:
                        self.active_geometry = f"{pos[0]},{pos[1]} {size[0]}x{size[1]}"
                    break

            # Fallback if focus is not marked on windows
            if not active_app:
                focus_proc = await asyncio.create_subprocess_exec(
                    "driftwm",
                    "msg",
                    "focus",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                f_out, _ = await asyncio.wait_for(focus_proc.communicate(), timeout=1.0)
                if focus_proc.returncode == 0 and f_out:
                    focus_line = f_out.decode().strip()
                    parts = focus_line.split(maxsplit=1)
                    if len(parts) == 2:
                        active_app = parts[1]

            return active_app, active_title, active_output, other_apps
        except (OSError, TimeoutError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            logger.debug("Failed to query driftwm state: %s", exc)
            return "", "", "", []

    @staticmethod
    async def get_media_context() -> str:
        """Query currently playing media metadata via playerctl."""
        if not shutil.which("playerctl"):
            return ""
        try:
            proc = await asyncio.create_subprocess_exec(
                "playerctl",
                "metadata",
                "--format",
                "{{status}}: {{artist}} — {{title}}",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=0.8)
            text = stdout.decode().strip()
            if text and not text.lower().startswith("no player"):
                return text
        except (OSError, TimeoutError):
            pass
        return ""

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

    async def capture_window_jpeg(self, geometry: str | None = None) -> bytes:
        """Capture cropped focused window screenshot via grim into memory bytes."""
        if not shutil.which("grim"):
            return b""

        target_geom = geometry or self.active_geometry
        if not target_geom:
            return b""

        cmd = [
            "grim",
            "-g",
            target_geom,
            "-t",
            "jpeg",
            "-q",
            str(self.cfg.jpeg_quality or 85),
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
            logger.warning("grim window capture failed: %s", exc)
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

        # Query active window, output, background windows and media
        app_id, title, output_name, other_apps = await self.get_driftwm_state()
        self.active_app = app_id
        self.active_title = title
        self.other_apps = other_apps

        # Query media status
        media_str = await self.get_media_context()
        self.media_context = media_str

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

        # Capture focused window crop if geometry is available
        window_crop_bytes: bytes | None = None
        if self.active_geometry and not self._is_video_mode:
            window_crop_bytes = await self.capture_window_jpeg()

        # Save to ring buffer
        rec = FrameRecord(
            timestamp=now,
            jpeg_bytes=jpeg_data,
            app_id=app_id,
            title=title,
            diff_ratio=diff_ratio,
            window_jpeg_bytes=window_crop_bytes,
        )
        self.frame_buffer.append(rec)

        # Detect video / gaming activity (continuous heavy diffs)
        if diff_ratio > 0.25:
            self._consecutive_heavy_diffs += 1
            if self._consecutive_heavy_diffs >= 5:
                self._is_video_mode = True
        else:
            self._consecutive_heavy_diffs = 0
            self._is_video_mode = False

        other_desc = f"; открыты также: {', '.join(other_apps[:3])}" if other_apps else ""
        media_desc = f" | играет: {media_str}" if media_str else ""

        if self._is_video_mode:
            self.screen_context = f"видео или динамичный контент в {app_id}{media_desc}"
        elif app_id:
            if "zed" in app_id.lower():
                self.screen_context = f"код в Zed: «{title}»{other_desc}{media_desc}"
            elif "telegram" in app_id.lower():
                self.screen_context = f"Telegram: чат «{title}»{other_desc}{media_desc}"
            elif "zen" in app_id.lower() or "browser" in app_id.lower():
                self.screen_context = f"браузер Zen: вкладка «{title}»{other_desc}{media_desc}"
            elif "kitty" in app_id.lower() or "terminal" in app_id.lower():
                self.screen_context = f"терминал Kitty: «{title}»{other_desc}{media_desc}"
            else:
                self.screen_context = f"активно {app_id}: «{title}»{other_desc}{media_desc}"
        else:
            self.screen_context = f"рабочий стол driftwm{other_desc}{media_desc}"

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
            trigger_image = window_crop_bytes or jpeg_data
            await self.bus.publish(
                ScreenChange(
                    image_bytes=trigger_image,
                    diff_ratio=diff_ratio,
                    active_app=app_id,
                    active_title=title,
                )
            )

    def get_latest_frame(self, prefer_window: bool = True) -> bytes | None:
        """Return the most recent frame from memory buffer (window crop preferred for OCR/VLM)."""
        if self.frame_buffer:
            last = self.frame_buffer[-1]
            if prefer_window and last.window_jpeg_bytes:
                return last.window_jpeg_bytes
            return last.jpeg_bytes
        return None
