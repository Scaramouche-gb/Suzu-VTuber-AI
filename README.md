# Suzu VTuber AI (涼)

AI-VTuber персонаж-компаньон для Arch Linux (Wayland / driftwm) с 3D VRM аватаром, распознаванием русской речи на GPU, зрением экрана, проактивной инициативой и долговременной памятью.

---

## 🏗 Архитектура

- **`daemon/`** (Python 3.13, asyncio):
  - **Дирижёр (`conductor.py`)**: машина состояний (`idle` ⇄ `listening` ⇄ `thinking` ⇄ `speaking`), стриминг предложений, бесшовный barge-in перебиваний.
  - **Зрение (`vision.py`)**: захват экрана через `grim`, парсинг активного окна в driftwm, фильтрация приватных приложений (Telegram, KeePassXC, банки), слепая зона.
  - **Уши & Голос (`audio_in.py`, `stt.py`, `tts.py`)**: Silero VAD (32 мс кадры, гистерезис, pre-roll), `faster-whisper` (`large-v3-turbo` на CUDA), Edge-TTS с эмоциональной модуляцией и словарём произношения (`pronounce.toml`).
  - **Инициатива и живость (`initiative.py`)**: пуассоновский hazard rate таймер, экспоненциальный backoff при игнорировании, смена настроений, тихие часы и DND.
  - **Память (`memory.py`)**: SQLite (`aiosqlite`) с автоматической санитизацией чувствительных данных (ключи, карты, пароли), извлечением фактов и сводок эпизодов.
  - **Управление (`ctl.py`)**: CLI-утилита `vtuberctl` и REST API (`/api/...`).
- **`renderer/`** (Vite, Three.js, `@pixiv/three-vrm`):
  - WebGL сцена со 100% прозрачным фоном, моргание, дыхание, шум Перлина, слежение взглядом за курсором и окнами.
  - Web Audio API липсинк в реальном времени через `AnalyserNode`.
- **`desktop/` & `quickshell/`**:
  - `quickshell/vtuber/shell.qml`: LayerShell оверлей над окнами с поддержкой click-through.
  - `desktop/driftwm-rule.toml`: правило для driftwm `pinned_to_screen = true`.
  - `desktop/electron/`: резервная прозрачная Wayland-оболочка на Electron.
  - `systemd/vtuber-daemon.service`: шаблон пользовательской службы systemd.

---

## 🚀 Быстрый старт

### 1. Установка зависимостей
```bash
# Python зависимости
uv sync

# Фронтенд рендерер
cd renderer && npm install && cd ..
```

### 2. Запуск в тестовом текстовом режиме (без траты токенов)
```bash
uv run python -m daemon.main --text --dry-run-llm
```
В соседнем терминале запустите рендерер:
```bash
cd renderer && npm run dev
```
Откройте `http://127.0.0.1:5173` в браузере. Вводите текст в терминале — аватар реагирует жестами, мимикой и анимациями.

### 3. Запуск полного голосового цикла
Сконфигурируйте `~/.config/vtuber/secrets.env` (например, `ANTHROPIC_API_KEY=...` или `OPENAI_API_KEY=...`):
```bash
chmod 600 ~/.config/vtuber/secrets.env
uv run python -m daemon.main
```

---

## 🎮 Управление через `vtuberctl`

```bash
# Проверить статус, настроение, энергию и суточные расходы
uv run vtuberctl status

# Управление микрофоном и зрением
uv run vtuberctl mute on|off
uv run vtuberctl blind on|off

# Режим "не беспокоить" (отключить проактивные фразы)
uv run vtuberctl dnd on|off

# Заставить аватара сказать фразу
uv run vtuberctl say "Привет! Как твои дела?"

# Посмотреть на экран, курсор или пользователя
uv run vtuberctl look screen
uv run vtuberctl look cursor

# Сменить настроение
uv run vtuberctl mood sassy       # playful, curious, sleepy, sassy, caring

# Скрыть / показать окно аватара
uv run vtuberctl hide
uv run vtuberctl show

# Долговременная память
uv run vtuberctl memory list
uv run vtuberctl memory forget 1
uv run vtuberctl memory wipe
```

---

## 🧪 Тестирование и линтер

```bash
# Запуск 44 юнит-тестов (Bus, Conductor, Vision, Initiative, Memory, STT, TTS, Tags, CLI)
uv run pytest

# Проверка кода линтером ruff
uv run ruff check .
```
