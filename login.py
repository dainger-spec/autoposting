"""Создаёт строку сессии SESSION_STRING для хостингов без консоли (например, bothost.ru).

Запустите на своём компьютере:  python login.py
Номер и код вводятся здесь, а полученную строку вы указываете на хостинге
в переменной окружения SESSION_STRING.
"""
from __future__ import annotations

import asyncio
import os
import sys

from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.sessions import StringSession

from autoposter.auth import login_user
from autoposter.config import BASE_DIR, ConfigError


async def main() -> None:
    load_dotenv(BASE_DIR / ".env")
    api_id = (os.getenv("API_ID") or os.getenv("TELEGRAM_API_ID") or input("API_ID: ")).strip()
    api_hash = (os.getenv("API_HASH") or os.getenv("TELEGRAM_API_HASH") or input("API_HASH: ")).strip()
    if not api_id.isdigit():
        raise ConfigError("API_ID должен быть числом")

    client = TelegramClient(StringSession(), int(api_id), api_hash)
    try:
        me = await login_user(client, os.getenv("PHONE", "").strip() or None)
        session = client.session.save()
    finally:
        await client.disconnect()

    print(f"\nГотово! Вы вошли как {me.first_name}.")
    print("Ваша строка сессии — скопируйте её целиком в переменную SESSION_STRING на хостинге:\n")
    print(session)
    print("\n⚠️  Эта строка даёт полный доступ к вашему аккаунту. Никому её не показывайте и не публикуйте.")
    print("    Если она утекла — завершите сеанс в Telegram: Настройки → Устройства.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except ConfigError as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        pass
