"""Отправка постов и работа с чатами через ваш аккаунт."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time

from telethon import Button, TelegramClient, errors, types, utils
from telethon.extensions import html as tl_html

from .config import parse_chat
from .letters import mix_letters
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


class ChannelPostMissing(Exception):
    """Сообщение поста в канале удалили — нужно опубликовать заново."""


def content_key(post: dict) -> str:
    """Отпечаток содержимого: изменился — значит, в канале лежит устаревшая версия."""
    src = post.get("source") or {}
    data = [post["text"], [m["file"] for m in post["media"]], src.get("text_id"), src.get("media_ids"),
            post["mix_letters"], post["buttons"]]
    return hashlib.sha1(json.dumps(data, ensure_ascii=False).encode()).hexdigest()[:12]


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
    if isinstance(exc, errors.ChatForwardsRestrictedError):
        return "в канале запрещена пересылка (включено «Запретить копирование»)"
    if isinstance(exc, errors.MessageTooLongError):
        return "текст слишком длинный (лимит — 4096 символов)"
    if isinstance(exc, ValueError):
        return f"чат не найден ({exc})"
    if isinstance(exc, RETRYABLE):
        return f"нет связи с Telegram ({type(exc).__name__})"
    return f"{type(exc).__name__}: {exc}"


def _file_kind(msg) -> str | None:
    if isinstance(msg.media, types.MessageMediaPhoto):
        return "photo"
    if isinstance(msg.media, types.MessageMediaDocument):
        return "document"
    return None


async def _input_entities(client: TelegramClient, entities: list) -> list:
    """Упоминания пользователей по ID нужно передавать в Telegram в «input»-виде."""
    result = []
    for entity in entities:
        if isinstance(entity, types.MessageEntityMentionName):
            try:
                user = await client.get_input_entity(entity.user_id)
            except (ValueError, TypeError):
                continue  # не знаем такого пользователя — оставляем просто текст
            entity = types.InputMessageEntityMentionName(entity.offset, entity.length, user)
        result.append(entity)
    return result


def url_buttons(post: dict):
    return [[Button.url(b["text"], b["url"]) for b in row] for row in post["buttons"]] or None


async def send_post(client: TelegramClient, entity, post: dict, source: dict | None = None, *,
                    as_bot: bool = False):
    """Отправляет пост в entity. Используется и аккаунтом (публикация), и ботом (превью, канал).

    source — исходные сообщения из чата с ботом (см. Sender.load_source): если они есть,
    текст с разметкой и вложения копируются из них как есть, без повторной загрузки.
    as_bot — отправляет бот: только он может добавить кнопки-ссылки, но не умеет
    премиум-эмодзи (они станут обычными).
    """
    source = source or {}
    if source.get("text") is not None:
        text, entities = source["text"].message or "", list(source["text"].entities or [])
    else:
        text, entities = tl_html.parse(post["text"])
    if post["mix_letters"]:
        text = mix_letters(text, entities)
    if as_bot:
        entities = [e for e in entities if not isinstance(e, types.MessageEntityCustomEmoji)]
    entities = await _input_entities(client, entities)
    common = {"silent": post["silent"]}
    # Кнопки может добавить только бот, и Telegram не показывает их под альбомами
    if as_bot and post["buttons"] and len(post["media"]) <= 1:
        common["buttons"] = url_buttons(post)

    if source.get("media"):
        media = source["media"]
        return await client.send_file(entity, media[0] if len(media) == 1 else media, caption=text,
                                      formatting_entities=entities, parse_mode=None, **common)

    files = [media_path(m["file"]) for m in post["media"]]
    for path in files:
        if not path.is_file():
            raise SendError(f"файл не найден: {path}")

    if not files:
        if not text.strip():
            raise SendError("пост пустой — нет ни текста, ни вложений")
        return await client.send_message(entity, text, formatting_entities=entities,
                                         link_preview=post["link_preview"], **common)

    options = {"caption": text, "formatting_entities": entities, "parse_mode": None, **common}
    if len(files) == 1:
        options.update(_KIND_OPTIONS.get(post["media"][0]["type"], {}))
        return await client.send_file(entity, str(files[0]), **options)

    # альбом (Telegram группирует до 10 вложений, остальные уйдут следующим альбомом)
    options["force_document"] = all(m["type"] == "document" for m in post["media"])
    options["supports_streaming"] = True
    return await client.send_file(entity, [str(f) for f in files], **options)


class Sender:
    def __init__(self, client: TelegramClient, default_chat: str | int | None, store=None, settings=None):
        self.client = client
        self.default_chat = default_chat
        self.store = store
        self.settings = settings
        self._titles: dict = {}
        self._dialogs_loaded = False
        self.me_id: int | None = None
        self.bot_id: int | None = None
        self.bot_client: TelegramClient | None = None
        self.bot_username: str | None = None
        self._bot_peer = None

    async def attach_bot(self, me, bot_me, bot_client: TelegramClient | None = None) -> None:
        """Запоминает бота управления: посты копируются из ваших сообщений в чате с ним,
        а посты с кнопками бот сам публикует в канал."""
        self.me_id, self.bot_id = me.id, bot_me.id
        self.bot_client, self.bot_username = bot_client, bot_me.username
        try:
            self._bot_peer = await self.client.get_input_entity(bot_me.username)
        except Exception as exc:
            log.warning("Аккаунт не видит бота @%s: %s", bot_me.username, exc)

    async def find_source(self, bot_messages: list) -> list[int] | None:
        """ID тех же сообщений, но со стороны вашего аккаунта (в личке у каждого свои ID)."""
        if self._bot_peer is None:
            return None
        try:
            candidates = await self.client.get_messages(self._bot_peer, limit=50)
        except Exception as exc:
            log.warning("Не удалось прочитать чат с ботом: %s", exc)
            return None
        candidates = sorted((c for c in candidates if c.out), key=lambda c: c.id)
        ids: list[int] = []
        for msg in bot_messages:
            match = next((c for c in candidates
                          if c.id not in ids and c.date == msg.date
                          and (c.message or "") == (msg.message or "")
                          and _file_kind(c) == _file_kind(msg)), None)
            if match is None:
                return None
            ids.append(match.id)
        return ids

    async def delete_source(self, posts: list[dict], keep: list[dict]) -> None:
        """Удаляет исходные сообщения удалённых постов из чата с ботом (кроме нужных другим постам)."""
        if self._bot_peer is None:
            return

        def ids_of(post):
            src = post.get("source")
            if not src or src["peer"] != self.bot_id:
                return set()
            return {i for i in [src["text_id"], *src["media_ids"]] if i}

        in_use = set().union(*map(ids_of, keep))
        ids = set().union(*map(ids_of, posts)) - in_use
        if not ids:
            return
        try:
            await self.client.delete_messages(self._bot_peer, sorted(ids))
        except Exception as exc:
            log.warning("Не удалось удалить исходные сообщения из чата с ботом: %s", exc)

    async def load_source(self, post: dict) -> dict | None:
        """Исходные сообщения поста, если они ещё есть в чате с ботом и не устарели."""
        src = post.get("source")
        if not src or src["peer"] != self.bot_id or self._bot_peer is None:
            return None
        use_text = src["text_id"] is not None
        # вложения поменяли вручную в posts.json — тогда берём файлы
        use_media = bool(src["media_ids"]) and src["media_files"] == [m["file"] for m in post["media"]]
        ids = ([src["text_id"]] if use_text else []) + (src["media_ids"] if use_media else [])
        if not ids:
            return None
        try:
            found = {m.id: m for m in await self.client.get_messages(self._bot_peer, ids=ids) if m}
        except Exception as exc:
            log.warning("Не удалось получить исходное сообщение поста #%s: %s", post["id"], exc)
            return None
        result = {}
        text_msg = found.get(src["text_id"]) if use_text else None
        # текст поменяли вручную в posts.json — тогда берём его оттуда
        if text_msg is not None and tl_html.unparse(text_msg.message or "", text_msg.entities or []) == post["text"]:
            result["text"] = text_msg
        if use_media and all(found.get(i) and _file_kind(found[i]) for i in src["media_ids"]):
            result["media"] = [found[i].media for i in src["media_ids"]]
        return result

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

    @property
    def channel(self):
        return self.settings.channel if self.settings else None

    def uses_channel(self, post: dict) -> bool:
        return self.channel is not None and post["via_channel"]

    def channel_post_ready(self, post: dict) -> bool:
        info = post.get("channel_post")
        return bool(info) and info["chat"] == self.channel and info["key"] == content_key(post)

    async def publish(self, post: dict):
        """Публикация по расписанию: напрямую в чат или пересылкой из канала."""
        if not self.uses_channel(post):
            return await self.send(post)
        chat = self.target(post)
        if chat is None:
            raise SendError("не указан чат: выберите его в посте или задайте DEFAULT_CHAT в .env")
        target = await self._input_entity(chat)
        info = await self.ensure_channel_post(post)
        try:
            return await self._forward(target, info, post)
        except (ChannelPostMissing, errors.MessageIdInvalidError):
            log.info("Пост #%s: сообщения в канале больше нет — публикую заново", post["id"])
            info = await self.ensure_channel_post(post, force=True)
            return await self._forward(target, info, post)

    async def ensure_channel_post(self, post: dict, *, force: bool = False) -> dict:
        """Публикует пост в канал один раз; дальше он только пересылается оттуда."""
        if not force and self.channel_post_ready(post):
            return post["channel_post"]
        channel = self.channel
        source = await self.load_source(post)
        if post["buttons"] and self.bot_client is not None:
            # Кнопки умеет добавлять только бот: он публикует пост в канал, аккаунт потом пересылает.
            # Вложения бот берёт из сохранённых файлов, текст с разметкой — из вашего сообщения.
            bot_source = {"text": source["text"]} if source and source.get("text") else None
            try:
                entity = await self.bot_client.get_input_entity(channel)
                result = await send_post(self.bot_client, entity, post, bot_source, as_bot=True)
            except (ValueError, errors.ChatAdminRequiredError, errors.ChatWriteForbiddenError,
                    errors.ChannelPrivateError) as exc:
                raise SendError(f"чтобы у поста были кнопки, добавьте бота @{self.bot_username} в канал "
                                f"админом с правом публикации ({describe_error(exc)})") from exc
        else:
            entity = await self._input_entity(channel)
            result = await send_post(self.client, entity, post, source)
        messages = result if isinstance(result, list) else [result]
        info = {"chat": channel, "ids": [m.id for m in messages], "key": content_key(post)}
        if self.store is not None:
            self.store.update(post["id"], channel_post=info)
        post["channel_post"] = info
        log.info("Пост #%s опубликован в канале %s", post["id"], channel)
        return info

    async def _forward(self, target, info: dict, post: dict):
        channel = await self._input_entity(info["chat"])
        # Есть ли ещё сообщение в канале, проверяем отдельным запросом. По ответу на пересылку
        # этого не понять: Telethon иногда не может сопоставить ответ (например, при пересылке
        # в «Избранное» или при сбоях на стороне Telegram) и возвращает None, хотя пересылка
        # прошла. Раньше это принималось за удалённый пост, и в канале появлялась копия.
        existing = await self.client.get_messages(channel, ids=info["ids"])
        if any(m is None for m in existing):
            raise ChannelPostMissing()
        try:
            return await self.client.forward_messages(target, info["ids"], from_peer=channel,
                                                      silent=post["silent"])
        except errors.FloodWaitError as exc:
            if exc.seconds > 120:
                raise
            await asyncio.sleep(exc.seconds + 1)
            return await self.client.forward_messages(target, info["ids"], from_peer=channel,
                                                      silent=post["silent"])

    async def send(self, post: dict):
        chat = self.target(post)
        if chat is None:
            raise SendError("не указан чат: выберите его в посте или задайте DEFAULT_CHAT в .env")
        entity = await self._input_entity(chat)
        source = await self.load_source(post)
        try:
            return await send_post(self.client, entity, post, source)
        except errors.FloodWaitError as exc:
            if exc.seconds > 120:
                raise
            log.warning("FloodWait %s с, жду и повторяю", exc.seconds)
            await asyncio.sleep(exc.seconds + 1)
            return await send_post(self.client, entity, post, source)

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

    async def writable_dialogs(self, limit: int = 40, *, channels_only: bool = False) -> list[tuple[int, str]]:
        """Группы и каналы, куда аккаунт может публиковать."""
        result = []
        async for dialog in self.client.iter_dialogs(limit=300):
            entity = dialog.entity
            if dialog.is_channel and not dialog.is_group:
                rights = getattr(entity, "admin_rights", None)
                if not (getattr(entity, "creator", False) or (rights and rights.post_messages)):
                    continue
            elif not dialog.is_group or channels_only:
                continue
            title = dialog.name or "Без названия"
            self._titles[dialog.id] = (title, None)
            result.append((dialog.id, title))
            if len(result) >= limit:
                break
        return result
