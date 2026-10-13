"""Unit tests for VisionManager privacy filters and driftwm state parsing."""

from daemon.bus import EventBus
from daemon.config import VisionConfig
from daemon.vision import VisionManager


def test_driftwm_state_dict_parsing() -> None:
    sample_state = {
        "Ok": {
            "State": {
                "outputs": [
                    {"name": "eDP-1", "active": True},
                    {"name": "HDMI-A-1", "active": False},
                ],
                "windows": [
                    {
                        "app_id": "zen",
                        "title": "GitHub — Zen Browser",
                        "is_focused": False,
                        "is_widget": False,
                    },
                    {
                        "app_id": "dev.zed.Zed",
                        "title": "Suzu-VTuber-AI — main.py",
                        "is_focused": True,
                        "is_widget": False,
                    },
                    {
                        "app_id": "quickshell",
                        "title": "desktop-overlay",
                        "is_focused": False,
                        "is_widget": True,
                    },
                ],
            }
        }
    }
    active_app, active_title, active_out, other_apps = VisionManager.parse_driftwm_state_dict(sample_state)
    assert active_app == "dev.zed.Zed"
    assert active_title == "Suzu-VTuber-AI — main.py"
    assert active_out == "eDP-1"
    assert len(other_apps) == 1
    assert "zen («GitHub — Zen Browser»)" in other_apps[0]


def test_vision_blacklist_privacy() -> None:
    cfg = VisionConfig(
        blacklist_app_ids=["keepassxc", "bitwarden", "telegram", "org.telegram.desktop"],
        blacklist_title_globs=["*bank*", "*банк*", "*пароль*", "*password*"],
    )
    bus = EventBus()
    vision = VisionManager(cfg, bus)

    # Blacklisted by app_id
    assert vision.is_blacklisted("org.telegram.desktop", "General Chat") is True
    assert vision.is_blacklisted("org.keepassxc.KeePassXC", "Database.kdbx") is True

    # Blacklisted by title
    assert vision.is_blacklisted("zen", "Личный Банк Онлайн") is True
    assert vision.is_blacklisted("firefox", "Введите пароль для входа") is True

    # Allowed safe app
    assert vision.is_blacklisted("dev.zed.Zed", "Suzu-VTuber-AI — main.py") is False


def test_vision_blind_mode() -> None:
    cfg = VisionConfig()
    bus = EventBus()
    vision = VisionManager(cfg, bus)

    assert vision.blind_mode is False
    vision.set_blind_mode(True)
    assert vision.blind_mode is True
