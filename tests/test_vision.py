"""Unit tests for VisionManager privacy filters and driftwm state parsing."""

from daemon.bus import EventBus
from daemon.config import VisionConfig
from daemon.vision import DRIFT_ACTIVE_OUTPUT_RE, DRIFT_ACTIVE_WINDOW_RE, VisionManager


def test_driftwm_regex_matching() -> None:
    line_win = '  * #28 zen [1422, -1081] 1896x931  "(1308) Что сделает ИИ, если дать ПОЛНЫЙ доступ к ПК? - YouTube — Zen Browser"'
    m = DRIFT_ACTIVE_WINDOW_RE.match(line_win)
    assert m is not None
    assert m.group(1) == "zen"
    assert "Zen Browser" in m.group(2)

    line_out = "  * eDP-1 camera 1422 -1104 zoom 1 1920x1080"
    m_out = DRIFT_ACTIVE_OUTPUT_RE.match(line_out)
    assert m_out is not None
    assert m_out.group(1) == "eDP-1"


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
