"""Antigravity (AGY) Autonomous Coding Agent integration bridge for Suzu."""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_AGY_PATH = "/home/scaramouche/.local/bin/agy"


@dataclass
class CodingTask:
    id: str
    prompt: str
    start_ts: float
    status: str = "running"  # "running" | "completed" | "failed" | "cancelled"
    output_lines: deque[str] = field(default_factory=lambda: deque(maxlen=50))
    summary: str = ""
    error: str = ""
    process: asyncio.subprocess.Process | None = None


class AntigravityBridge:
    """Manages an autonomous background coding session via the Antigravity (agy) CLI."""

    def __init__(self, workspace_dir: str | Path | None = None) -> None:
        self.workspace_dir = Path(workspace_dir or Path.cwd())
        self.agy_path = self._resolve_agy_path()
        self.current_task: CodingTask | None = None
        self._task_counter = 0

    def _resolve_agy_path(self) -> str:
        if Path(DEFAULT_AGY_PATH).is_file():
            return DEFAULT_AGY_PATH
        which = shutil.which("agy")
        if which:
            return which
        return "agy"

    def is_busy(self) -> bool:
        return self.current_task is not None and self.current_task.status == "running"

    def get_status(self) -> dict[str, Any]:
        if not self.current_task:
            return {"active": False, "status": "idle", "message": "Антигравити сейчас свободен."}

        task = self.current_task
        elapsed = int(time.time() - task.start_ts)
        latest_snippet = "\n".join(task.output_lines)[-300:] if task.output_lines else ""

        return {
            "active": task.status == "running",
            "task_id": task.id,
            "status": task.status,
            "prompt": task.prompt,
            "elapsed_seconds": elapsed,
            "summary": task.summary,
            "error": task.error,
            "latest_snippet": latest_snippet,
        }

    async def start_task(self, prompt: str, on_complete: Any | None = None) -> tuple[bool, str]:
        """Start a new coding task in the background using Antigravity CLI."""
        if self.is_busy():
            return (
                False,
                f"Антигравити уже занят задачей «{self.current_task.prompt[:40]}...». Подожди или отмени её.",
            )

        self._task_counter += 1
        task_id = f"task_{self._task_counter}"

        task = CodingTask(
            id=task_id,
            prompt=prompt,
            start_ts=time.time(),
            status="running",
        )
        self.current_task = task

        logger.info("[Antigravity] Starting coding task %s: %s", task_id, prompt)
        asyncio.create_task(self._run_task_worker(task, on_complete))
        return True, f"Запустила задачу для Антигравити: {prompt[:60]}"

    async def _run_task_worker(self, task: CodingTask, on_complete: Any | None = None) -> None:
        """Worker executing agy CLI process."""
        cmd = [
            self.agy_path,
            "-p",
            task.prompt,
            "--continue",
            "--dangerously-skip-permissions",
            "--output-format",
            "text",
        ]

        logger.info("[Antigravity] Executing command: %s (cwd: %s)", cmd, self.workspace_dir)

        try:
            env = os.environ.copy()
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(self.workspace_dir),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
            task.process = proc

            stdout_data, stderr_data = await proc.communicate()
            exit_code = proc.returncode

            stdout_str = stdout_data.decode("utf-8", errors="replace").strip()
            stderr_str = stderr_data.decode("utf-8", errors="replace").strip()

            if stdout_str:
                for line in stdout_str.splitlines():
                    task.output_lines.append(line)
            if stderr_str:
                for line in stderr_str.splitlines():
                    task.output_lines.append(line)

            if exit_code == 0:
                task.status = "completed"
                # Strip out trailing persistent memory context hook if present
                clean_output = stdout_str.split("[Automatic Persistent Memory Context]:")[0].strip()
                task.summary = clean_output[-400:] if clean_output else (stdout_str[-400:] if stdout_str else "Задача успешно выполнена.")
                logger.info("[Antigravity] Task %s completed successfully", task.id)
            else:
                task.status = "failed"
                task.error = stderr_str or f"Код выхода: {exit_code}"
                task.summary = f"Ошибка при выполнении: {task.error[:200]}"
                logger.warning("[Antigravity] Task %s failed (exit %d): %s", task.id, exit_code, task.error)

        except asyncio.CancelledError:
            task.status = "cancelled"
            task.summary = "Задача была прервана пользователем."
            if task.process and task.process.returncode is None:
                try:
                    task.process.terminate()
                except OSError:
                    pass
            logger.info("[Antigravity] Task %s cancelled", task.id)
            raise
        except (OSError, RuntimeError) as exc:
            task.status = "failed"
            task.error = str(exc)
            task.summary = f"Ошибка запуска Антигравити: {exc}"
            logger.error("[Antigravity] Failed to run agy: %s", exc)
        finally:
            if on_complete:
                try:
                    res = on_complete(task)
                    if asyncio.iscoroutine(res):
                        await res
                except Exception as exc:  # noqa: BLE001
                    logger.debug("Error in on_complete callback: %s", exc)

    async def cancel_current_task(self) -> tuple[bool, str]:
        """Cancel currently active task."""
        if not self.is_busy() or not self.current_task:
            return False, "Сейчас нет активных задач Антигравити."

        task = self.current_task
        task.status = "cancelled"
        if task.process and task.process.returncode is None:
            try:
                task.process.terminate()
            except OSError:
                pass
        return True, "Остановила задачу Антигравити."
