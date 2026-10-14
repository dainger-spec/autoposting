"""Вход в аккаунт Telegram."""
from __future__ import annotations

import getpass
import sys

from telethon import TelegramClient

from .config import ConfigError


def has_console() -> bool:
    return sys.stdin is not None and sys.stdin.isatty()


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
