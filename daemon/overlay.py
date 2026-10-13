"""Transparent borderless desktop overlay for Suzu VTuber AI avatar."""

from __future__ import annotations

import os
import sys

# Allow WebEngine transparency, prevent NVIDIA/Wayland Vulkan segfault, and enable audio autoplay
os.environ.setdefault(
    "QTWEBENGINE_CHROMIUM_FLAGS",
    "--disable-gpu-compositing --disable-features=Vulkan --enable-transparent-visuals --disable-gpu-vsync --autoplay-policy=no-user-gesture-required",
)


def run_overlay(
    url: str = "http://127.0.0.1:8765/index.html?fit=bust", width: int = 486, height: int = 547
) -> None:
    try:
        from PyQt6.QtCore import Qt, QUrl
        from PyQt6.QtWebEngineCore import QWebEngineSettings
        from PyQt6.QtWebEngineWidgets import QWebEngineView
        from PyQt6.QtWidgets import QApplication
    except ImportError:
        print("\n[!] Ошибка: Для запуска оверлея требуется пакет PyQt6 и PyQt6-WebEngine.")
        print("Установи их командой: uv add PyQt6 PyQt6-WebEngine\n")
        sys.exit(1)

    app = QApplication(sys.argv)
    # Set desktop file name for Wayland app_id matching in driftwm window rules
    app.setApplicationName("vtuber-overlay")
    app.setDesktopFileName("vtuber-overlay")

    view = QWebEngineView()
    view.setWindowTitle("Suzu VTuber")
    view.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
    view.setStyleSheet("background: transparent;")
    view.page().setBackgroundColor(Qt.GlobalColor.transparent)

    # Audio autoplay and rendering settings
    settings = view.settings()
    settings.setAttribute(QWebEngineSettings.WebAttribute.PlaybackRequiresUserGesture, False)
    settings.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, True)
    settings.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessFileUrls, True)
    settings.setAttribute(QWebEngineSettings.WebAttribute.ScrollAnimatorEnabled, False)

    # Frameless always-on-top window for Wayland / driftwm
    view.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint)

    # Position in bottom-right corner
    screen = app.primaryScreen()
    if screen:
        geo = screen.availableGeometry()
        x = geo.width() - width - 16
        y = geo.height() - height - 4
        view.setGeometry(x, y, width, height)
    else:
        view.resize(width, height)

    view.load(QUrl(url))
    view.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    target_url = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8765/index.html?fit=bust"
    run_overlay(target_url)
