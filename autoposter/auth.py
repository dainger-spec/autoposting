"""Вход в аккаунт Telegram."""
from __future__ import annotations

import getpass
import json
import sys
from pathlib import Path

from telethon import TelegramClient

from .config import ConfigError


def has_console() -> bool:
    return sys.stdin is not None and sys.stdin.isatty()


def load_owner(path: Path) -> int | None:
    """ID того, кто входил в аккаунт через бота (ему разрешено управлять ботом)."""
    try:
        return int(json.loads(path.read_text(encoding="utf-8"))["owner_id"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def save_owner(path: Path, owner_id: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"owner_id": owner_id}), encoding="utf-8")


async def login_user(client: TelegramClient, phone: str | None, *, from_string: bool = False):
    """Входит в аккаунт; при первом входе спрашивает номер, код и облачный пароль."""
    await client.connect()
    if not await client.is_user_authorized() and not has_console():
        if from_string:
            raise ConfigError("SESSION_STRING недействительна (возможно, сессию завершили в настройках "
                              "Telegram) — создайте новую командой python login.py на своём компьютере")
        raise ConfigError("аккаунт не авторизован, а ввести код входа здесь негде (нет консоли). "
                          "Выполните на своём компьютере python login.py и укажите полученную строку "
                          "в переменной окружения SESSION_STRING")
    await client.start(
        phone=phone or (lambda: input("Номер телефона вашего аккаунта (например +79991234567): ")),
        code_callback=lambda: input("Код из Telegram: "),
        password=lambda: getpass.getpass("Облачный пароль (двухэтапная аутентификация): "),
    )
    me = await client.get_me()
    if me.bot:
        raise ConfigError("в сессию аккаунта вошли ботом — удалите data/user.session "
                          "и войдите по номеру телефона")
    return me
