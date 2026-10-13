"""Unit tests for AppLauncher desktop application launcher."""

import pytest

from daemon.launcher import AppLauncher


def test_app_launcher_resolve_alias() -> None:
    # Known alias checks
    assert AppLauncher.resolve_app_command("telegram") is not None
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
