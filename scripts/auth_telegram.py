"""CLI utility to authorize Telegram Telethon Userbot session for Suzu."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

# Add project root to sys.path for direct script execution
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from telethon import TelegramClient

from daemon.config import load_secrets

SECRETS_PATH = Path.home() / ".config" / "vtuber" / "secrets.env"
SESSION_DIR = Path.home() / ".config" / "vtuber"
SESSION_NAME = "suzu_userbot"


def get_credentials() -> tuple[int, str]:
    """Retrieve API ID and HASH from environment or prompt interactively."""
    load_secrets()

    api_id_env = os.getenv("TELEGRAM_API_ID", "").strip()
    api_hash_env = os.getenv("TELEGRAM_API_HASH", "").strip()

    if not api_id_env or not api_hash_env:
        print("\nAPI ID и API HASH не найдены в secrets.env.")
        print("Получить их можно на https://my.telegram.org -> API development tools.\n")
        api_id_input = input("Введи api_id: ").strip()
        api_hash_input = input("Введи api_hash: ").strip()

        if not api_id_input or not api_hash_input:
            print("Ошибка: api_id и api_hash обязательны.")
            sys.exit(1)

        api_id = int(api_id_input)
        api_hash = api_hash_input

        # Save to secrets.env
        SECRETS_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(SECRETS_PATH, "a", encoding="utf-8") as f:
            f.write(f"\nTELEGRAM_API_ID={api_id}\n")
            f.write(f"TELEGRAM_API_HASH={api_hash}\n")
        SECRETS_PATH.chmod(0o600)
        print(f"Ключи сохранены в {SECRETS_PATH}")
        return api_id, api_hash

    return int(api_id_env), api_hash_env


async def auth_session(api_id: int, api_hash: str) -> None:
    """Connect and authorize Telethon session."""
    SESSION_DIR.mkdir(parents=True, exist_ok=True)
    session_file = str(SESSION_DIR / SESSION_NAME)

    print(f"\nПодключение к Telegram (сессия: {session_file})...")
    client = TelegramClient(session_file, api_id, api_hash)
    await client.start()

    me = await client.get_me()
    first_name = getattr(me, "first_name", "User")
    username = getattr(me, "username", "без юзернейма")
    phone = getattr(me, "phone", "")

    print("\nУСПЕШНО АВТОРИЗОВАНО!")
    print(f"• Имя: {first_name}")
    print(f"• Юзернейм: @{username}")
    print(f"• Номер: +{phone}")
    print(f"• Файл сессии: {session_file}.session")
    print("\nТеперь Сузу может отправлять кружки, голосовые и сообщения прямо от твоего имени!\n")

    await client.disconnect()


def main() -> None:
    print("\n=== Авторизация Telegram-сессии для Сузу ===")
    api_id, api_hash = get_credentials()
    asyncio.run(auth_session(api_id, api_hash))


if __name__ == "__main__":
    main()
