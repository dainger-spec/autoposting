"""Бот для управления постами: создание, редактирование, расписание, отправка."""
from __future__ import annotations

import functools
import html
import logging
import math
import shutil
import uuid
from collections import Counter
from datetime import datetime
from pathlib import Path

from telethon import Button, TelegramClient, errors, events, functions, types, utils
from telethon.extensions import html as tl_html

from .config import BASE_DIR, Config
from .schedule import (ALL_DAYS, DAY_NAMES, WEEKDAYS, WEEKEND, format_days, format_when,
                       next_run, parse_times)
from .sender import Sender, SendError, describe_error, send_post
from .storage import PostStore

log = logging.getLogger(__name__)

PAGE_SIZE = 8
BODY_PREVIEW_LIMIT = 3000

KIND_NAMES = {
    "photo": "фото", "video": "видео", "animation": "GIF", "document": "файл", "auto": "файл",
    "audio": "аудио", "voice": "голосовое", "video_note": "кружок",
}

COMMANDS = [
    ("menu", "Главное меню"),
    ("list", "Мои посты"),
    ("new", "Новый пост"),
    ("status", "Статус и ближайшие публикации"),
    ("cancel", "Отменить текущее действие"),
    ("help", "Как пользоваться"),
]

CONTENT_PROMPT_NEW = (
    "<b>➕ Новый пост</b>\n\n"
    "Пришлите содержимое поста одним сообщением:\n"
    "• текст — форматирование (жирный, ссылки и т.д.) сохранится;\n"
    "• фото, видео, GIF, файл или голосовое — с подписью или без;\n"
    "• альбом из нескольких фото или видео.\n\n"
    "Можно просто переслать сюда готовый пост из любого канала."
)
CONTENT_PROMPT_EDIT = (
    "<b>✏️ Изменение поста #{id}</b>\n\n"
    "Пришлите новое содержимое:\n"
    "• только текст — заменится текст, вложения останутся;\n"
    "• медиа с подписью — заменится всё;\n"
    "• медиа без подписи — заменятся только вложения."
)
TIMES_PROMPT = (
    "⏰ Во сколько публиковать? Пришлите время в формате <b>ЧЧ:ММ</b> "
    "(часовой пояс {tz}).\n"
    "Можно несколько раз в день — через запятую: <code>09:00, 14:30, 21:00</code>"
)
CHAT_PROMPT = (
    "💬 <b>Куда публиковать?</b>\n\n"
    "Выберите кнопкой или пришлите сообщением:\n"
    "• @username группы или канала;\n"
    "• ссылку <code>https://t.me/...</code>;\n"
    "• ID чата, например <code>-1001234567890</code>.\n\n"
    "Ваш аккаунт должен состоять в чате и иметь право писать в него."
)
HELP = (
    "<b>❓ Как пользоваться</b>\n\n"
    "1. «➕ Новый пост» → пришлите текст или медиа → укажите время → выберите чат.\n"
    "2. В карточке поста его можно выключить, изменить текст, время, дни недели и чат, "
    "отправить прямо сейчас или посмотреть превью.\n"
    "3. Посты хранятся в файле <code>posts.json</code> — его можно править и вручную, "
    "бот сам подхватит изменения.\n\n"
    "Публикации идут с <b>вашего аккаунта</b>, бот нужен только для управления.\n"
    "Время указывается по часовому поясу <b>{tz}</b>.\n\n"
    "Команды: /menu /list /new /status /cancel"
)


def plain(text_html: str) -> str:
    return tl_html.parse(text_html)[0]


def short(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def media_summary(media: list[dict]) -> str:
    counts = Counter(KIND_NAMES.get(m["type"], "файл") for m in media)
    return ", ".join(name if n == 1 else f"{name} ×{n}" for name, n in counts.items())


def media_kind(msg) -> str | None:
    media = msg.media
    if isinstance(media, types.MessageMediaPhoto):
        return "photo"
    if not isinstance(media, types.MessageMediaDocument) or msg.sticker:
        return None
    if msg.gif:
        return "animation"
    if msg.video_note:
        return "video_note"
    if msg.voice:
        return "voice"
    if msg.video:
        return "video"
    if msg.audio:
        return "audio"
    return "document"


def is_unsupported(msg) -> bool:
    """Стикеры, опросы, геолокации и т.п. — то, что нельзя сохранить как пост."""
    return (msg.media is not None
            and not isinstance(msg.media, types.MessageMediaWebPage)
            and media_kind(msg) is None)


def cancel_buttons():
    return [[Button.inline("✖️ Отмена", b"cancel")]]


def guarded(handler):
    """Не даёт ошибке в обработчике остаться незамеченной: пишет её в чат с ботом."""
    @functools.wraps(handler)
    async def wrapper(self, event):
        try:
            await handler(self, event)
        except events.StopPropagation:
            raise
        except Exception as exc:
            log.exception("Ошибка в обработчике бота")
            try:
                await event.respond(f"⚠️ Что-то пошло не так: {html.escape(describe_error(exc))}")
            except Exception:
                pass
    return wrapper


class ControlBot:
    def __init__(self, bot: TelegramClient, cfg: Config, store: PostStore, sender: Sender, admins: set[int]):
        self.bot = bot
        self.cfg = cfg
        self.store = store
        self.sender = sender
        self.admins = set(admins)
        # Пошаговые диалоги: {user_id: {"step": ..., "post_id": ..., "draft": {...}}}
        self.states: dict[int, dict] = {}

    async def setup(self) -> None:
        self.bot.parse_mode = "html"

        def is_admin(event):
            return event.is_private and event.sender_id in self.admins

        def is_stranger(event):
            return event.is_private and event.sender_id not in self.admins

        self.bot.add_event_handler(self.on_album, events.Album(func=is_admin))
        self.bot.add_event_handler(self.on_message, events.NewMessage(incoming=True, func=is_admin))
        self.bot.add_event_handler(self.on_callback, events.CallbackQuery(func=is_admin))
        self.bot.add_event_handler(self.on_stranger, events.NewMessage(incoming=True, func=is_stranger))
        self.bot.add_event_handler(self.on_stranger_callback, events.CallbackQuery(func=is_stranger))
        try:
            await self.bot(functions.bots.SetBotCommandsRequest(
                scope=types.BotCommandScopeDefault(), lang_code="",
                commands=[types.BotCommand(command=c, description=d) for c, d in COMMANDS],
            ))
        except errors.RPCError as exc:
            log.warning("Не удалось установить меню команд бота: %s", exc)

    async def notify(self, text: str) -> None:
        for admin in self.admins:
            try:
                await self.bot.send_message(admin, text)
            except Exception as exc:
                log.warning("Не удалось отправить уведомление %s (он запускал бота?): %s", admin, exc)

    # ── общие помощники ────────────────────────────────────────────

    async def reply(self, event, text: str, buttons=None, *, edit: bool = False) -> None:
        """Отвечает новым сообщением или (для кнопок) редактирует текущее."""
        if edit:
            try:
                await event.edit(text, buttons=buttons, link_preview=False)
                return
            except errors.MessageNotModifiedError:
                return
            except errors.RPCError as exc:
                log.debug("Не удалось изменить сообщение, отправляю новое: %s", exc)
        await event.respond(text, buttons=buttons, link_preview=False)

    def drop_state(self, user_id: int) -> None:
        state = self.states.pop(user_id, None)
        if state and state.get("draft", {}).get("media"):
            self.store.release_media(state["draft"]["media"])

    async def chat_title_html(self, chat) -> str:
        title = await self.sender.title(chat)
        if title is None:
            return f"⚠️ {html.escape(str(chat))} (не найден)"
        return html.escape(title)

    async def chat_label(self, chat) -> str:
        if chat is not None:
            return await self.chat_title_html(chat)
        if self.cfg.default_chat is None:
            return "⚠️ не выбран"
        return f"по умолчанию — {await self.chat_title_html(self.cfg.default_chat)}"

    async def download(self, msg) -> dict:
        folder = self.cfg.media_dir / uuid.uuid4().hex
        folder.mkdir(parents=True, exist_ok=True)
        try:
            path = await msg.download_media(file=str(folder))
            if not path:
                raise SendError("файл не скачался")
        except BaseException:
            shutil.rmtree(folder, ignore_errors=True)
            raise
        path = Path(path).resolve()
        try:
            stored = path.relative_to(BASE_DIR.resolve()).as_posix()
        except ValueError:
            stored = str(path)
        return {"file": stored, "type": media_kind(msg)}

    # ── экраны ────────────────────────────────────────────────────

    async def show_menu(self, event, *, edit: bool = False, note: str | None = None) -> None:
        self.store.reload()
        posts = self.store.all()
        active = sum(p["enabled"] for p in posts)
        text = (f"{note}\n\n" if note else "") + (
            "<b>🤖 Автопостинг</b>\n\n"
            f"Постов: <b>{len(posts)}</b>, активных: <b>{active}</b>.\n"
            "Публикации идут с вашего аккаунта по расписанию."
        )
        buttons = [
            [Button.inline("📋 Мои посты", b"list:0"), Button.inline("➕ Новый пост", b"new")],
            [Button.inline("ℹ️ Статус", b"status"), Button.inline("❓ Помощь", b"help")],
        ]
        await self.reply(event, text, buttons, edit=edit)

    async def show_help(self, event, *, edit: bool = False) -> None:
        await self.reply(event, HELP.format(tz=self.cfg.tz.key), [[Button.inline("« Меню", b"menu")]], edit=edit)

    async def show_status(self, event, *, edit: bool = False) -> None:
        self.store.reload()
        now = datetime.now(self.cfg.tz)
        me = await self.sender.client.get_me()
        posts = self.store.all()
        active = [p for p in posts if p["enabled"]]
        runs = sorted(((when, p) for p in active if (when := next_run(p, now))), key=lambda x: x[0])

        name = html.escape(utils.get_display_name(me))
        username = f" (@{me.username})" if me.username else ""
        default = (await self.chat_title_html(self.cfg.default_chat)
                   if self.cfg.default_chat is not None else "не задан")
        lines = [
            "<b>ℹ️ Статус</b>",
            "",
            f"👤 Аккаунт: {name}{username}",
            f"🕒 Сейчас: {now:%H:%M} ({self.cfg.tz.key})",
            f"💬 Чат по умолчанию: {default}",
            f"📋 Постов: {len(posts)}, активных: {len(active)}",
            "",
            "<b>⏭ Ближайшие публикации:</b>",
        ]
        for when, post in runs[:7]:
            preview = short(plain(post["text"]), 30) or media_summary(post["media"])
            lines.append(f"• {format_when(when, now)} — #{post['id']} {html.escape(preview)}")
        if not runs:
            lines.append("пока ничего не запланировано")
        buttons = [[Button.inline("🔄 Обновить", b"status"), Button.inline("« Меню", b"menu")]]
        await self.reply(event, "\n".join(lines), buttons, edit=edit)

    def post_label(self, post: dict) -> str:
        icon = "🟢" if post["enabled"] else "⏸"
        times = ", ".join(post["times"]) or "без времени"
        preview = short(plain(post["text"]), 28) if post["text"].strip() else f"[{media_summary(post['media'])}]"
        return short(f"{icon} #{post['id']} · {times} · {preview}", 64)

    async def show_list(self, event, page: int = 0, *, edit: bool = False, note: str | None = None) -> None:
        self.store.reload()
        posts = self.store.all()
        prefix = f"{note}\n\n" if note else ""
        if not posts:
            buttons = [[Button.inline("➕ Новый пост", b"new"), Button.inline("« Меню", b"menu")]]
            await self.reply(event, prefix + "📋 Постов пока нет — создайте первый!", buttons, edit=edit)
            return

        pages = math.ceil(len(posts) / PAGE_SIZE)
        page = min(max(page, 0), pages - 1)
        chunk = posts[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]
        rows = [[Button.inline(self.post_label(p), f"post:{p['id']}")] for p in chunk]
        if pages > 1:
            nav = []
            if page > 0:
                nav.append(Button.inline("◀️", f"list:{page - 1}"))
            nav.append(Button.inline(f"{page + 1} / {pages}", b"noop"))
            if page < pages - 1:
                nav.append(Button.inline("▶️", f"list:{page + 1}"))
            rows.append(nav)
        rows.append([Button.inline("➕ Новый пост", b"new"), Button.inline("« Меню", b"menu")])
        active = sum(p["enabled"] for p in posts)
        text = (prefix + f"<b>📋 Посты</b> — всего {len(posts)}, активных {active}\n"
                "Нажмите на пост, чтобы открыть его настройки.")
        await self.reply(event, text, rows, edit=edit)

    async def card(self, post: dict, note: str | None = None):
        """Карточка поста: (текст, текст без разметки на случай ошибки, кнопки)."""
        now = datetime.now(self.cfg.tz)
        pid = post["id"]
        lines = [note, ""] if note else []
        lines += [
            f"<b>📝 Пост #{pid}</b> — {'🟢 включён' if post['enabled'] else '⏸ выключен'}",
            f"💬 Чат: {await self.chat_label(post['chat'])}",
            f"⏰ Время: {', '.join(post['times']) or '⚠️ не задано'}",
            f"📅 Дни: {format_days(post['days'])}",
        ]
        if post["media"]:
            lines.append(f"📎 Вложения: {media_summary(post['media'])}")
        when = next_run(post, now)
        if when:
            lines.append(f"⏭ Следующая публикация: <b>{format_when(when, now)}</b>")
        header = "\n".join(lines) + "\n──────────\n"

        if not post["text"].strip():
            body = safe_body = "<i>(без текста)</i>"
        else:
            text_plain = plain(post["text"])
            if len(text_plain) > BODY_PREVIEW_LIMIT:
                text_plain = text_plain[:BODY_PREVIEW_LIMIT] + "…"
            safe_body = html.escape(text_plain)
            body = post["text"] if len(post["text"]) <= BODY_PREVIEW_LIMIT else safe_body

        rows = [
            [Button.inline("⏸ Выключить" if post["enabled"] else "▶️ Включить", f"toggle:{pid}"),
             Button.inline("🚀 Отправить сейчас", f"send:{pid}")],
            [Button.inline("✏️ Текст / медиа", f"edit:{pid}:content"),
             Button.inline("👁 Превью", f"test:{pid}")],
            [Button.inline("⏰ Время", f"edit:{pid}:times"),
             Button.inline("📅 Дни", f"edit:{pid}:days")],
            [Button.inline("💬 Чат", f"edit:{pid}:chat"),
             Button.inline("🔕 Без звука" if post["silent"] else "🔔 Со звуком", f"silent:{pid}")],
        ]
        extra = []
        if not post["media"]:
            state = "вкл" if post["link_preview"] else "выкл"
            extra.append(Button.inline(f"🔗 Превью ссылок: {state}", f"lp:{pid}"))
        elif post["text"].strip():
            extra.append(Button.inline("🧹 Убрать вложения", f"nomedia:{pid}"))
        if extra:
            rows.append(extra)
        rows.append([Button.inline("🗑 Удалить", f"del:{pid}"), Button.inline("« К списку", b"list:0")])
        return header + body, header + safe_body, rows

    async def show_card(self, event, post_id: int, *, note: str | None = None, edit: bool = False) -> None:
        post = self.store.get(post_id)
        if post is None:
            await self.show_list(event, edit=edit, note="⚠️ Пост не найден — возможно, его удалили.")
            return
        text, safe_text, buttons = await self.card(post, note)
        try:
            await self.reply(event, text, buttons, edit=edit)
        except errors.RPCError:
            # например, в тексте разметка, которую бот не может показать
            await self.reply(event, safe_text, buttons, edit=edit)

    async def show_days(self, event, post: dict) -> None:
        pid, days = post["id"], set(post["days"])

        def day_button(d):
            return Button.inline(("✅ " if d in days else "▫️ ") + DAY_NAMES[d - 1], f"day:{pid}:{d}")

        rows = [
            [day_button(d) for d in (1, 2, 3, 4)],
            [day_button(d) for d in (5, 6, 7)],
            [Button.inline("Будни", f"days:{pid}:wd"), Button.inline("Выходные", f"days:{pid}:we"),
             Button.inline("Каждый день", f"days:{pid}:all")],
            [Button.inline("✅ Готово", f"post:{pid}")],
        ]
        text = (f"📅 <b>Дни публикации поста #{pid}</b>: {format_days(post['days'])}\n\n"
                "Нажимайте на дни, чтобы включить или выключить их.")
        await self.reply(event, text, rows, edit=True)

    async def ask_chat(self, event, *, edit: bool = False) -> None:
        rows = []
        if self.cfg.default_chat is not None:
            title = await self.sender.title(self.cfg.default_chat) or str(self.cfg.default_chat)
            rows.append([Button.inline(short(f"⭐️ По умолчанию: {title}", 60), b"chat:default")])
        rows.append([Button.inline("📂 Выбрать из моих чатов", b"pick")])
        rows.append([Button.inline("📌 Избранное (для проверки)", b"chat:me")])
        rows += cancel_buttons()
        await self.reply(event, CHAT_PROMPT, rows, edit=edit)

    # ── сообщения ─────────────────────────────────────────────────

    @guarded
    async def on_stranger(self, event) -> None:
        await event.respond(
            "⛔️ Это приватный бот для управления автопостингом.\n"
            f"Ваш Telegram ID: <code>{event.sender_id}</code>"
        )

    @guarded
    async def on_stranger_callback(self, event) -> None:
        await event.answer("Нет доступа", alert=True)

    @guarded
    async def on_album(self, event) -> None:
        state = self.states.get(event.sender_id)
        if not state or not state["step"].endswith("content"):
            await event.respond("Альбом можно прислать, когда бот попросит содержимое поста. Откройте /menu")
            return
        await self.handle_content(event, state, event.messages)

    @guarded
    async def on_message(self, event) -> None:
        msg = event.message
        if msg.grouped_id:
            return  # части альбома обрабатывает on_album
        user_id = event.sender_id
        text = (msg.raw_text or "").strip()

        if text.startswith("/"):
            command = text.split()[0][1:].split("@")[0].lower()
            if command in ("start", "menu"):
                self.drop_state(user_id)
                return await self.show_menu(event)
            if command == "list":
                self.drop_state(user_id)
                return await self.show_list(event)
            if command == "new":
                return await self.start_new(event)
            if command == "status":
                return await self.show_status(event)
            if command == "cancel":
                return await self.cancel(event)
            if command == "help":
                return await self.show_help(event)

        state = self.states.get(user_id)
        if state is None:
            return await self.show_menu(event)
        step = state["step"]
        if step.endswith("content"):
            await self.handle_content(event, state, [msg])
        elif step.endswith("times"):
            await self.handle_times(event, state, text)
        elif step.endswith("chat"):
            await self.handle_chat_text(event, state, text)

    async def start_new(self, event, *, edit: bool = False) -> None:
        self.drop_state(event.sender_id)
        self.states[event.sender_id] = {"step": "new_content"}
        await self.reply(event, CONTENT_PROMPT_NEW, cancel_buttons(), edit=edit)

    async def cancel(self, event, *, edit: bool = False) -> None:
        state = self.states.get(event.sender_id)
        post_id = state.get("post_id") if state else None
        self.drop_state(event.sender_id)
        if post_id:
            await self.show_card(event, post_id, edit=edit)
        else:
            await self.show_menu(event, edit=edit, note="✖️ Действие отменено" if state else None)

    async def handle_content(self, event, state: dict, messages: list) -> None:
        user_id = event.sender_id
        if any(map(is_unsupported, messages)):
            await event.respond(
                "⚠️ Такой тип сообщений не поддерживается. "
                "Пришлите текст, фото, видео, GIF, файл, аудио или голосовое.",
                buttons=cancel_buttons())
            return
        text = next((m.text for m in messages if m.text and m.text.strip()), "")
        media_messages = [m for m in messages if media_kind(m)]
        if not text and not media_messages:
            await event.respond("⚠️ Сообщение пустое — пришлите текст или медиа.", buttons=cancel_buttons())
            return

        media = []
        if media_messages:
            status = await event.respond("⏳ Загружаю вложения…")
            try:
                for m in media_messages:
                    media.append(await self.download(m))
            except Exception as exc:
                self.store.release_media(media)
                await status.edit(f"❌ Не удалось загрузить вложение: {html.escape(describe_error(exc))}")
                return
            await status.delete()
        if self.states.get(user_id) is not state:  # пока качали, пользователь отменил действие
            self.store.release_media(media)
            return

        if state["step"] == "new_content":
            state["draft"] = {"text": text, "media": media}
            state["step"] = "new_times"
            await event.respond(TIMES_PROMPT.format(tz=self.cfg.tz.key), buttons=cancel_buttons())
            return

        post_id = state["post_id"]
        self.states.pop(user_id, None)
        old = self.store.get(post_id)
        fields = {}
        if text:
            fields["text"] = text
        if media:
            fields["media"] = media
        if old is None or self.store.update(post_id, **fields) is None:
            self.store.release_media(media)
            return await self.show_list(event, note="⚠️ Пост не найден — возможно, его удалили.")
        if media:
            self.store.release_media(old["media"])
        await self.show_card(event, post_id, note="✅ Содержимое обновлено")

    async def handle_times(self, event, state: dict, text: str) -> None:
        try:
            times = parse_times(text)
        except ValueError as exc:
            await event.respond(
                f"⚠️ Не понял время: <code>{html.escape(str(exc))}</code>\n"
                "Формат — ЧЧ:ММ, например <code>09:00</code> или <code>09:00, 18:30</code>",
                buttons=cancel_buttons())
            return
        if state["step"] == "new_times":
            state["draft"]["times"] = times
            state["step"] = "new_chat"
            return await self.ask_chat(event)
        self.states.pop(event.sender_id, None)
        self.store.update(state["post_id"], times=times)
        await self.show_card(event, state["post_id"], note="✅ Время обновлено")

    async def handle_chat_text(self, event, state: dict, text: str) -> None:
        if not text:
            return await self.ask_chat(event)
        try:
            chat, _ = await self.sender.lookup(text)
        except Exception as exc:
            await event.respond(
                f"❌ Не получилось найти чат: {html.escape(describe_error(exc))}\n"
                "Проверьте написание и что ваш аккаунт состоит в этом чате.",
                buttons=cancel_buttons())
            return
        await self.apply_chat(event, state, chat)

    async def apply_chat(self, event, state: dict, chat, *, edit: bool = False) -> None:
        self.states.pop(event.sender_id, None)
        if state["step"] == "new_chat":
            draft = state["draft"]
            post = self.store.add(text=draft["text"], media=draft["media"], times=draft["times"], chat=chat)
            note = "✅ Пост создан и включён. Дни недели и остальное можно настроить кнопками ниже."
            return await self.show_card(event, post["id"], note=note, edit=edit)
        self.store.update(state["post_id"], chat=chat)
        await self.show_card(event, state["post_id"], note="✅ Чат обновлён", edit=edit)

    # ── кнопки ────────────────────────────────────────────────────

    @guarded
    async def on_callback(self, event) -> None:
        action, _, arg = event.data.decode(errors="ignore").partition(":")
        try:
            await self.dispatch(event, action, arg)
        finally:
            try:
                await event.answer()
            except errors.RPCError:
                pass

    async def dispatch(self, event, action: str, arg: str) -> None:
        user_id = event.sender_id
        if action == "noop":
            return
        if action == "cancel":
            return await self.cancel(event, edit=True)
        if action == "pick":
            return await self.pick_chat(event)
        if action == "chat":
            return await self.choose_chat(event, arg)

        # Любая другая кнопка — уход из пошагового диалога
        self.drop_state(user_id)
        if action == "menu":
            return await self.show_menu(event, edit=True)
        if action == "help":
            return await self.show_help(event, edit=True)
        if action == "status":
            return await self.show_status(event, edit=True)
        if action == "list":
            return await self.show_list(event, int(arg) if arg.isdigit() else 0, edit=True)
        if action == "new":
            return await self.start_new(event, edit=True)

        # Остальные действия относятся к конкретному посту: "действие:id[:параметр]"
        post_id, _, extra = arg.partition(":")
        post = self.store.get(int(post_id)) if post_id.isdigit() else None
        if post is None:
            await event.answer("Пост не найден — возможно, его удалили", alert=True)
            return await self.show_list(event, edit=True)
        pid = post["id"]

        if action == "post":
            await self.show_card(event, pid, edit=True)
        elif action == "toggle":
            post = self.store.update(pid, enabled=not post["enabled"])
            await event.answer("Пост включён" if post["enabled"] else "Пост выключен")
            await self.show_card(event, pid, edit=True)
        elif action == "silent":
            self.store.update(pid, silent=not post["silent"])
            await self.show_card(event, pid, edit=True)
        elif action == "lp":
            self.store.update(pid, link_preview=not post["link_preview"])
            await self.show_card(event, pid, edit=True)
        elif action == "send":
            if self.sender.target(post) is None:
                await event.answer("Сначала выберите чат для поста", alert=True)
                return
            label = await self.chat_label(post["chat"])
            buttons = [[Button.inline("✅ Да, отправить", f"sendok:{pid}"), Button.inline("« Назад", f"post:{pid}")]]
            await self.reply(event, f"🚀 Опубликовать пост #{pid} в {label} прямо сейчас?", buttons, edit=True)
        elif action == "sendok":
            await event.answer("Отправляю…")
            try:
                await self.sender.send(post)
            except Exception as exc:
                note = f"❌ Не удалось отправить: {html.escape(describe_error(exc))}"
            else:
                note = "✅ Пост опубликован"
            await self.show_card(event, pid, note=note, edit=True)
        elif action == "test":
            await self.preview(event, post)
        elif action == "edit":
            await self.start_edit(event, post, extra)
        elif action == "day":
            day = int(extra) if extra.isdigit() and 1 <= int(extra) <= 7 else None
            if day is None:
                return
            days = set(post["days"])
            if day in days and len(days) == 1:
                await event.answer("Должен остаться хотя бы один день. "
                                   "Чтобы остановить пост — выключите его.", alert=True)
                return
            days.symmetric_difference_update({day})
            await self.show_days(event, self.store.update(pid, days=sorted(days)))
        elif action == "days":
            presets = {"wd": WEEKDAYS, "we": WEEKEND, "all": ALL_DAYS}
            if extra in presets:
                await self.show_days(event, self.store.update(pid, days=presets[extra]))
        elif action == "nomedia":
            if not post["text"].strip():
                await event.answer("Без вложений пост останется пустым", alert=True)
                return
            self.store.update(pid, media=[])
            self.store.release_media(post["media"])
            await self.show_card(event, pid, note="🧹 Вложения удалены", edit=True)
        elif action == "del":
            buttons = [[Button.inline("✅ Да, удалить", f"delok:{pid}"), Button.inline("« Отмена", f"post:{pid}")]]
            await self.reply(event, f"🗑 Удалить пост #{pid}? Это действие нельзя отменить.", buttons, edit=True)
        elif action == "delok":
            self.store.delete(pid)
            await self.show_list(event, edit=True, note=f"🗑 Пост #{pid} удалён")

    async def start_edit(self, event, post: dict, field: str) -> None:
        user_id, pid = event.sender_id, post["id"]
        if field == "content":
            self.states[user_id] = {"step": "edit_content", "post_id": pid}
            await self.reply(event, CONTENT_PROMPT_EDIT.format(id=pid), cancel_buttons(), edit=True)
        elif field == "times":
            self.states[user_id] = {"step": "edit_times", "post_id": pid}
            current = ", ".join(post["times"]) or "не задано"
            text = f"Сейчас: <b>{current}</b>\n\n" + TIMES_PROMPT.format(tz=self.cfg.tz.key)
            await self.reply(event, text, cancel_buttons(), edit=True)
        elif field == "chat":
            self.states[user_id] = {"step": "edit_chat", "post_id": pid}
            await self.ask_chat(event, edit=True)
        elif field == "days":
            await self.show_days(event, post)

    async def preview(self, event, post: dict) -> None:
        await event.answer("Готовлю превью…")
        await self.reply(event, f"👁 Превью поста #{post['id']} — так он будет выглядеть:", edit=True)
        try:
            await send_post(self.bot, await event.get_input_chat(), post)
        except Exception as exc:
            await event.respond(f"❌ Превью не получилось: {html.escape(describe_error(exc))}")
        await self.show_card(event, post["id"])

    async def pick_chat(self, event) -> None:
        state = self.states.get(event.sender_id)
        if not state or not state["step"].endswith("chat"):
            await event.answer("Эта кнопка устарела", alert=True)
            return
        await event.answer("Загружаю ваши чаты…")
        dialogs = await self.sender.writable_dialogs()
        rows = [[Button.inline(short(title, 50), f"chat:{peer_id}")] for peer_id, title in dialogs]
        rows += cancel_buttons()
        if dialogs:
            text = ("📂 <b>Ваши группы и каналы</b>, куда можно публиковать.\n"
                    "Нужного нет? Пришлите его @username или ID сообщением.")
        else:
            text = "Не нашёл групп и каналов, куда вы можете писать. Пришлите @username или ID чата сообщением."
        await self.reply(event, text, rows, edit=True)

    async def choose_chat(self, event, arg: str) -> None:
        state = self.states.get(event.sender_id)
        if not state or not state["step"].endswith("chat"):
            await event.answer("Эта кнопка устарела", alert=True)
            return
        if arg == "default":
            chat = None
        elif arg == "me":
            chat = "me"
        else:
            try:
                chat = int(arg)
            except ValueError:
                return
        await self.apply_chat(event, state, chat, edit=True)
