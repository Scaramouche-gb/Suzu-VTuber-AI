# Suzu VTuber AI

AI-VTuber companion for Arch Linux with 3D VRM avatar, voice interaction, screen vision, proactive behavior, and layered desktop overlay.

## Архитектура
- **daemon (Python/asyncio)**: ядро логики (VAD, STT, LLM, TTS, Vision, Memory, Initiative Engine).
- **renderer (three.js / three-vrm)**: браузерная сцена с 3D VRM аватаром, прозрачным фоном, мимикой и липсинком.
- **shell (Quickshell / Electron)**: рабочий стол Wayland поверх окон.

## Быстрый старт
```bash
uv sync
uv run python -m daemon.main
```
