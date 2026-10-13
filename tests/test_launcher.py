"""Unit tests for AppLauncher desktop application launcher."""

import pytest

from daemon.launcher import AppLauncher


def test_app_launcher_resolve_alias() -> None:
    # Known alias checks
    assert AppLauncher.resolve_app_command("telegram") in (
        "/usr/bin/Telegram",
        "Telegram",
        "telegram",
        "org.telegram.desktop",
    )
    assert AppLauncher.resolve_app_command("телеграм") is not None
    assert AppLauncher.resolve_app_command("kitty") is not None
    assert AppLauncher.resolve_app_command("терминал") is not None
    assert AppLauncher.resolve_app_command("zed") is not None


def test_app_launcher_resolve_unknown() -> None:
    assert AppLauncher.resolve_app_command("nonexistent_fake_app_xyz") is None


@pytest.mark.asyncio
async def test_app_launcher_launch_unknown() -> None:
    success, msg = await AppLauncher.launch("nonexistent_fake_app_xyz")
    assert not success
    assert "Не нашла" in msg


@pytest.mark.asyncio
async def test_app_launcher_type_text_empty() -> None:
    success, msg = await AppLauncher.type_text("")
    assert not success
    assert "Пустой текст" in msg


def test_app_launcher_key_mapping() -> None:
    assert AppLauncher._KEY_MAP["enter"] == "Return"
    assert AppLauncher._KEY_MAP["space"] == "space"
    assert AppLauncher._KEY_MAP["пробел"] == "space"
    assert AppLauncher._KEY_MAP["backspace"] == "BackSpace"
    assert AppLauncher._KEY_MAP["стереть"] == "BackSpace"


@pytest.mark.asyncio
async def test_app_launcher_press_key_empty() -> None:
    success, msg = await AppLauncher.press_key("")
    assert not success
    assert "Пустая клавиша" in msg


