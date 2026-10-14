"""Отправка постов и работа с чатами через ваш аккаунт."""
from __future__ import annotations

import asyncio
import logging
import time

from telethon import TelegramClient, errors, types, utils

from .config import parse_chat
from .storage import media_path

log = logging.getLogger(__name__)

# Временные сбои: отправку имеет смысл повторить
RETRYABLE = (ConnectionError, asyncio.TimeoutError, errors.ServerError, errors.TimedOutError)

_KIND_OPTIONS = {
    "video": {"supports_streaming": True},
    "animation": {"attributes": [types.DocumentAttributeAnimated()]},
    "voice": {"voice_note": True},
    "video_note": {"video_note": True},
    "document": {"force_document": True},
}


class SendError(Exception):
    pass


def describe_error(exc: BaseException) -> str:
    """Понятное описание частых ошибок Telegram."""
    if isinstance(exc, SendError):
        return str(exc)
    if isinstance(exc, errors.FloodWaitError):
        return f"лимит Telegram, нужно подождать {exc.seconds} с"
    if isinstance(exc, errors.SlowModeWaitError):
        return f"в чате медленный режим, следующее сообщение можно через {exc.seconds} с"
    if isinstance(exc, errors.ChatWriteForbiddenError):
        return "нет прав писать в этот чат"
    if isinstance(exc, errors.ChatAdminRequiredError):
        return "нужны права администратора, чтобы публиковать здесь"
    if isinstance(exc, errors.UserBannedInChannelError):
        return "вам запрещено писать в этот чат"
    if isinstance(exc, errors.ChannelPrivateError):
        return "чат недоступен: вы не участник или вас удалили"
    if isinstance(exc, errors.MediaCaptionTooLongError):
        return "подпись к медиа слишком длинная (без Premium лимит — 1024 символа)"
    if isinstance(exc, errors.MessageTooLongError):
        return "текст слишком длинный (лимит — 4096 символов)"
    if isinstance(exc, ValueError):
        return f"чат не найден ({exc})"
    if isinstance(exc, RETRYABLE):
        return f"нет связи с Telegram ({type(exc).__name__})"
    return f"{type(exc).__name__}: {exc}"


async def send_post(client: TelegramClient, entity, post: dict):
    """Отправляет пост в entity. Используется и аккаунтом (публикация), и ботом (превью)."""
    text = post["text"]
    files = [media_path(m["file"]) for m in post["media"]]
    for path in files:
        if not path.is_file():
            raise SendError(f"файл не найден: {path}")

    if not files:
        if not text.strip():
            raise SendError("пост пустой — нет ни текста, ни вложений")
        return await client.send_message(
            entity, text, parse_mode="html",
            link_preview=post["link_preview"], silent=post["silent"],
        )

    options = {"caption": text, "parse_mode": "html", "silent": post["silent"]}
    if len(files) == 1:
        options.update(_KIND_OPTIONS.get(post["media"][0]["type"], {}))
        return await client.send_file(entity, str(files[0]), **options)

    # альбом (Telegram группирует до 10 вложений, остальные уйдут следующим альбомом)
    options["force_document"] = all(m["type"] == "document" for m in post["media"])
    options["supports_streaming"] = True
    return await client.send_file(entity, [str(f) for f in files], **options)


class Sender:
    def __init__(self, client: TelegramClient, default_chat: str | int | None):
        self.client = client
        self.default_chat = default_chat
        self._titles: dict = {}
        self._dialogs_loaded = False

    def target(self, post: dict):
        return post["chat"] if post["chat"] is not None else self.default_chat

    async def _input_entity(self, chat):
        try:
            return await self.client.get_input_entity(chat)
        except ValueError:
            # Чат по числовому ID может быть ещё не известен сессии —
            # один раз загружаем список диалогов и пробуем снова.
            if self._dialogs_loaded or not isinstance(chat, int):
                raise
            self._dialogs_loaded = True
            await self.client.get_dialogs()
            return await self.client.get_input_entity(chat)

    async def send(self, post: dict):
        chat = self.target(post)
        if chat is None:
            raise SendError("не указан чат: выберите его в посте или задайте DEFAULT_CHAT в .env")
        entity = await self._input_entity(chat)
        try:
            return await send_post(self.client, entity, post)
        except errors.FloodWaitError as exc:
            if exc.seconds > 120:
                raise
            log.warning("FloodWait %s с, жду и повторяю", exc.seconds)
            await asyncio.sleep(exc.seconds + 1)
            return await send_post(self.client, entity, post)

    async def lookup(self, text: str) -> tuple[str | int, str]:
        """Находит чат по тому, что прислал пользователь. Возвращает (chat, название)."""
        text = text.strip()
        if text.lower() in ("me", "self", "избранное"):
            return "me", "Избранное"
        chat = parse_chat(text)
        if chat is None:
            raise ValueError("пустой ввод")
        entity = await self.client.get_entity(await self._input_entity(chat))
        peer_id = utils.get_peer_id(entity)
        title = utils.get_display_name(entity) or str(peer_id)
        self._titles[peer_id] = (title, None)
        return peer_id, title

    async def title(self, chat) -> str | None:
        """Название чата для отображения (None — если чат не найден)."""
        if chat is None:
            return None
        if chat == "me":
            return "Избранное"
        cached = self._titles.get(chat)
        if cached and (cached[0] is not None or time.monotonic() - cached[1] < 300):
            return cached[0]
        try:
            entity = await self.client.get_entity(await self._input_entity(chat))
            title = utils.get_display_name(entity) or str(chat)
            self._titles[chat] = (title, None)
        except Exception as exc:
            log.debug("Не удалось получить чат %r: %s", chat, exc)
            title = None
            self._titles[chat] = (None, time.monotonic())
        return title

    async def writable_dialogs(self, limit: int = 40) -> list[tuple[int, str]]:
        """Группы и каналы, куда аккаунт может публиковать."""
        result = []
        async for dialog in self.client.iter_dialogs(limit=300):
            entity = dialog.entity
            if dialog.is_channel and not dialog.is_group:
                rights = getattr(entity, "admin_rights", None)
                if not (getattr(entity, "creator", False) or (rights and rights.post_messages)):
                    continue
            elif not dialog.is_group:
                continue
            title = dialog.name or "Без названия"
            self._titles[dialog.id] = (title, None)
            result.append((dialog.id, title))
            if len(result) >= limit:
                break
        return result
