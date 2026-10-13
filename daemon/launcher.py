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
