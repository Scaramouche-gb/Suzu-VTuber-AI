"""Transparent borderless desktop overlay for Suzu VTuber AI avatar."""

from __future__ import annotations

import sys


def run_overlay(url: str = "http://127.0.0.1:8765/?fit=bust", width: int = 420, height: int = 720) -> None:
    try:
        from PyQt6.QtCore import Qt, QUrl
        from PyQt6.QtWebEngineWidgets import QWebEngineView
        from PyQt6.QtWidgets import QApplication
    except ImportError:
        print("\n[!] Ошибка: Для запуска оверлея требуется пакет python-pyqt6-webengine.")
        print("Установи его командой: sudo pacman -S python-pyqt6-webengine\n")
        sys.exit(1)

    app = QApplication(sys.argv)
    app.setApplicationName("Suzu-VTuber-Overlay")

    view = QWebEngineView()
    view.setWindowTitle("Suzu VTuber")
    view.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
    view.setStyleSheet("background: transparent;")
    view.page().setBackgroundColor(Qt.GlobalColor.transparent)

    # Wayland transparent always-on-top frameless window flags
    view.setWindowFlags(
        Qt.WindowType.FramelessWindowHint
        | Qt.WindowType.WindowStaysOnTopHint
        | Qt.WindowType.SubWindow
    )

    # Position in bottom-right corner
    screen = app.primaryScreen()
    if screen:
        geo = screen.availableGeometry()
        x = geo.width() - width - 16
        y = geo.height() - height - 16
        view.setGeometry(x, y, width, height)
    else:
        view.resize(width, height)

    view.load(QUrl(url))
    view.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    target_url = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8765/?fit=bust"
    run_overlay(target_url)
