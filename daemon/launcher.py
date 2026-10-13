"""Desktop application launcher for Suzu AI-VTuber."""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
from pathlib import Path

logger = logging.getLogger(__name__)

# Common aliases mapped to executable names or desktop file IDs
APP_ALIASES: dict[str, list[str]] = {
    "telegram": ["org.telegram.desktop", "telegram-desktop", "ayugram-desktop"],
    "телеграм": ["org.telegram.desktop", "telegram-desktop"],
    "телега": ["org.telegram.desktop", "telegram-desktop"],
    "zed": ["/home/scaramouche/.local/bin/zed", "dev.zed.Zed", "zed"],
    "зед": ["/home/scaramouche/.local/bin/zed", "dev.zed.Zed", "zed"],
    "kitty": ["kitty"],
    "терминал": ["kitty", "xterm"],
    "консоль": ["kitty"],
    "zen": ["zen-browser", "zen"],
    "zen-browser": ["zen-browser", "zen"],
    "браузер": ["zen-browser", "zen", "firefox", "chromium"],
    "дзен": ["zen-browser", "zen"],
    "spotify": ["spotify"],
    "спотифай": ["spotify"],
    "музыка": ["spotify"],
    "discord": ["discord", "equibop", "vesktop"],
    "дискорд": ["discord", "equibop", "vesktop"],
    "code": ["code-oss", "codium", "code"],
    "vscode": ["code-oss", "codium", "code"],
    "вскод": ["code-oss", "codium", "code"],
    "nautilus": ["org.gnome.Nautilus", "nautilus"],
    "файлы": ["org.gnome.Nautilus", "nautilus"],
    "проводник": ["org.gnome.Nautilus", "nautilus"],
    "obs": ["com.obsproject.Studio", "obs"],
    "обс": ["com.obsproject.Studio", "obs"],
}


class AppLauncher:
    """Safely launches desktop applications via gtk-launch or background process."""

    @staticmethod
    def resolve_app_command(app_name: str) -> str | None:
        """Resolve app name or alias to executable or desktop ID."""
        key = app_name.strip().lower()

        # Check alias table first
        candidates = APP_ALIASES.get(key, [key])

        for candidate in candidates:
            # Check if it's an absolute path
            if os.path.isabs(candidate) and Path(candidate).is_file():
                return candidate
            # Check if candidate is executable in PATH
            if shutil.which(candidate):
                return candidate
            # Check if desktop file exists
            for d in ("/usr/share/applications", str(Path.home() / ".local/share/applications")):
                cand_desktop = Path(d) / f"{candidate}.desktop"
                if cand_desktop.is_file():
                    return candidate

        return None

    @classmethod
    async def launch(cls, app_name: str) -> tuple[bool, str]:
        """Launch an application detached in the desktop environment."""
        clean_name = app_name.strip().lower()
        target = cls.resolve_app_command(clean_name)

        if not target:
            logger.warning("[AppLauncher] App not found: '%s'", app_name)
            return False, f"Не нашла приложение «{app_name}»"

        try:
            # Prefer gtk-launch if target looks like a desktop ID or exists in desktop dirs
            is_desktop_id = "." in target and not os.path.isabs(target)
            if is_desktop_id and shutil.which("gtk-launch"):
                cmd = ["gtk-launch", target]
            else:
                cmd = [target]

            logger.info("[AppLauncher] Launching command: %s", cmd)
            # Launch completely detached so it doesn't block or close with daemon
            await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                start_new_session=True,
            )
            return True, f"Запустила {clean_name}"
        except (OSError, RuntimeError) as exc:
            logger.error("[AppLauncher] Failed to launch '%s': %s", app_name, exc)
            return False, f"Ошибка запуска {app_name}: {exc}"

    @classmethod
    async def close(cls, app_name: str) -> tuple[bool, str]:
        """Close an application by process pattern or binary name."""
        clean_name = app_name.strip().lower()
        candidates = APP_ALIASES.get(clean_name, [clean_name])

        for cand in candidates:
            # Strip path if full path
            bin_name = Path(cand).name.removesuffix(".desktop")

            try:
                proc = await asyncio.create_subprocess_exec(
                    "pkill",
                    "-f",
                    bin_name,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await proc.wait()
                if proc.returncode == 0:
                    logger.info("[AppLauncher] Closed process matching '%s'", bin_name)
                    return True, f"Закрыла {clean_name}"
            except (OSError, RuntimeError) as exc:
                logger.debug("[AppLauncher] pkill '%s' error: %s", bin_name, exc)

        return False, f"Не смогла закрыть «{app_name}»"

    @staticmethod
    async def media(action: str) -> tuple[bool, str]:
        """Control media playback via playerctl (play-pause, next, previous)."""
        if not shutil.which("playerctl"):
            return False, "playerctl не установлен"

        act = action.strip().lower()
        cmd_arg = "play-pause" if act in ("play-pause", "toggle", "pause", "play") else act

        try:
            proc = await asyncio.create_subprocess_exec(
                "playerctl",
                cmd_arg,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.wait()
            return True, f"Медиа: {cmd_arg}"
        except (OSError, RuntimeError) as exc:
            return False, f"Ошибка медиа: {exc}"

    @staticmethod
    async def volume(direction: str) -> tuple[bool, str]:
        """Adjust system volume via wpctl (+5% / -5%)."""
        if not shutil.which("wpctl"):
            return False, "wpctl не установлен"

        delta = "+5%" if direction in ("up", "+", "louder") else "-5%"
        try:
            proc = await asyncio.create_subprocess_exec(
                "wpctl",
                "set-volume",
                "@DEFAULT_AUDIO_SINK@",
                delta,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.wait()
            return True, f"Громкость {delta}"
        except (OSError, RuntimeError) as exc:
            return False, f"Ошибка громкости: {exc}"

    @staticmethod
    async def type_text(text: str, press_enter: bool = False) -> tuple[bool, str]:
        """Type text into currently active window via wtype or ydotool with clipboard fallback."""
        if not text:
            return False, "Пустой текст для ввода"

        # 1. Native Wayland virtual-keyboard via wtype
        if shutil.which("wtype"):
            try:
                proc = await asyncio.create_subprocess_exec(
                    "wtype",
                    "-s",
                    "10",
                    "--",
                    text,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await proc.wait()
                if press_enter:
                    await asyncio.sleep(0.05)
                    ret_proc = await asyncio.create_subprocess_exec(
                        "wtype",
                        "-k",
                        "Return",
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.DEVNULL,
                    )
                    await ret_proc.wait()
                return True, f"Напечатала: {text[:30]}"
            except (OSError, RuntimeError) as exc:
                logger.warning("[AppLauncher] wtype execution failed: %s", exc)

        # 2. ydotool fallback
        if shutil.which("ydotool"):
            try:
                proc = await asyncio.create_subprocess_exec(
                    "ydotool",
                    "type",
                    text,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await proc.wait()
                if press_enter:
                    await asyncio.sleep(0.05)
                    ret_proc = await asyncio.create_subprocess_exec(
                        "ydotool",
                        "key",
                        "28:1",
                        "28:0",
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.DEVNULL,
                    )
                    await ret_proc.wait()
                return True, f"Напечатала через ydotool: {text[:30]}"
            except (OSError, RuntimeError) as exc:
                logger.warning("[AppLauncher] ydotool execution failed: %s", exc)

        # 3. wl-copy fallback: copies into Wayland clipboard
        if shutil.which("wl-copy"):
            try:
                proc = await asyncio.create_subprocess_exec(
                    "wl-copy",
                    "--",
                    text,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await proc.wait()
                return True, f"Скопировала в буфер обмена: {text[:30]}"
            except (OSError, RuntimeError) as exc:
                logger.warning("[AppLauncher] wl-copy execution failed: %s", exc)

        return False, "Не найден инструмент ввода текста (установи wtype)"

    @staticmethod
    async def press_key(key: str) -> tuple[bool, str]:
        """Press a keyboard key (Return, Escape, BackSpace, Tab) via wtype or ydotool."""
        if shutil.which("wtype"):
            try:
                proc = await asyncio.create_subprocess_exec(
                    "wtype",
                    "-k",
                    key,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await proc.wait()
                return True, f"Нажата клавиша {key}"
            except (OSError, RuntimeError) as exc:
                return False, f"Ошибка нажатия клавиши {key}: {exc}"
        return False, "wtype не установлен"
