"""Вход в аккаунт через чат с ботом — для хостингов без консоли.

Код подтверждения вводится кнопками: если отправить его обычным сообщением,
Telegram решит, что код кому-то переслали, и заблокирует вход.
"""
from __future__ import annotations

import asyncio
import html
import logging
import re

from telethon import Button, TelegramClient, errors, events, utils

from .sender import describe_error

log = logging.getLogger(__name__)

PHONE_RE = re.compile(r"\+?\d[\d\s\-()]{8,}")

PHONE_PROMPT = (
    "👋 Чтобы публиковать посты от вашего имени, нужно один раз войти в ваш аккаунт Telegram.\n\n"
    "Нажмите кнопку <b>«📱 Отправить мой номер»</b> внизу "
    "или пришлите номер сообщением в формате <code>+79991234567</code>."
)


def keypad():
    def key(digit):
        return Button.inline(digit, f"lc:{digit}")
    return [
        [key("1"), key("2"), key("3")],
        [key("4"), key("5"), key("6")],
        [key("7"), key("8"), key("9")],
        [Button.inline("⌫", b"lc:del"), key("0"), Button.inline("✅ Войти", b"lc:ok")],
        [Button.inline("🔁 Прислать код заново", b"lc:resend")],
    ]


def code_text(code: str, note: str = "") -> str:
    # цифры не показываем: так код точно не окажется в переписке
    dots = " ".join(["●"] * len(code) + ["○"] * max(0, 5 - len(code)))
    return (
        (f"{note}\n\n" if note else "")
        + "🔐 Telegram прислал код в приложение (чат «Telegram»).\n"
        "Введите его <b>кнопками ниже</b> — обычным сообщением код отправлять нельзя, "
        "Telegram его заблокирует.\n\n"
        f"Код: <b>{dots}</b>"
    )


class BotLogin:
    def __init__(self, bot: TelegramClient, user: TelegramClient, allowed: frozenset[int]):
        self.bot = bot
        self.user = user
        self.allowed = allowed  # пусто — войти может тот, кто первым напишет боту
        self.states: dict[int, dict] = {}
        self._done: asyncio.Future | None = None

    async def run(self):
        """Ждёт, пока кто-то войдёт в аккаунт. Возвращает (аккаунт, id того, кто входил)."""
        self._done = asyncio.get_running_loop().create_future()
        handlers = [
            (self.on_message, events.NewMessage(incoming=True, func=lambda e: e.is_private)),
            (self.on_callback, events.CallbackQuery(func=lambda e: e.is_private)),
        ]
        for callback, event in handlers:
            self.bot.add_event_handler(callback, event)
        try:
            return await self._done
        finally:
            for callback, _ in handlers:
                self.bot.remove_event_handler(callback)

    def _allowed(self, user_id: int) -> bool:
        return not self.allowed or user_id in self.allowed

    async def on_message(self, event) -> None:
        if not self._allowed(event.sender_id):
            await event.respond(f"⛔️ Это приватный бот.\nВаш Telegram ID: <code>{event.sender_id}</code>")
            return
        msg = event.message
        state = self.states.get(event.sender_id, {})
        try:
            if state.get("step") == "password":
                password = msg.raw_text or ""
                try:
                    await msg.delete()  # пароль не должен оставаться в переписке
                except errors.RPCError:
                    pass
                await self.check_password(event, password)
            elif msg.contact or PHONE_RE.fullmatch((msg.raw_text or "").strip()):
                await self.send_code(event, msg.contact.phone_number if msg.contact else msg.raw_text)
            else:
                await event.respond(PHONE_PROMPT, buttons=[[
                    Button.request_phone("📱 Отправить мой номер", resize=True, single_use=True)]])
        except Exception as exc:
            log.exception("Ошибка входа через бота")
            await event.respond(f"⚠️ Ошибка: {html.escape(describe_error(exc))}\nПопробуйте ещё раз: /start")

    async def send_code(self, event, phone: str) -> None:
        phone = "+" + re.sub(r"\D", "", phone)
        await event.respond(f"📨 Запрашиваю код для {phone}…", buttons=Button.clear())
        try:
            await self.user.send_code_request(phone)
        except errors.ApiIdInvalidError:
            await event.respond("❌ API_ID или API_HASH неверные — проверьте настройки скрипта.")
            return
        except errors.PhoneNumberInvalidError:
            await event.respond("❌ Неверный номер. Пришлите его в формате <code>+79991234567</code>.")
            return
        except errors.PhoneNumberBannedError:
            await event.respond("❌ Этот номер заблокирован в Telegram.")
            return
        except (errors.FloodWaitError, errors.PhoneNumberFloodError) as exc:
            await event.respond(f"❌ Слишком много попыток входа: {html.escape(describe_error(exc))}")
            return
        self.states[event.sender_id] = {"step": "code", "phone": phone, "code": ""}
        await event.respond(code_text(""), buttons=keypad())

    async def on_callback(self, event) -> None:
        if not self._allowed(event.sender_id):
            await event.answer("Нет доступа", alert=True)
            return
        state = self.states.get(event.sender_id)
        if not state or state["step"] != "code":
            await event.answer("Сначала пришлите номер телефона — /start", alert=True)
            return
        key = event.data.decode(errors="ignore").removeprefix("lc:")
        note = ""
        try:
            if key.isdigit() and len(key) == 1:
                state["code"] = (state["code"] + key)[:8]
            elif key == "del":
                state["code"] = state["code"][:-1]
            elif key == "resend":
                await self.user.send_code_request(state["phone"])
                state["code"] = ""
                note = "🔁 Код отправлен заново."
            elif key == "ok":
                if len(state["code"]) < 4:
                    await event.answer("Введите код полностью", alert=True)
                    return
                await event.answer("Проверяю…")
                await self.check_code(event, state)
                return
            await event.edit(code_text(state["code"], note), buttons=keypad())
        except errors.MessageNotModifiedError:
            pass
        except Exception as exc:
            log.exception("Ошибка входа через бота")
            await event.respond(f"⚠️ Ошибка: {html.escape(describe_error(exc))}\nПопробуйте ещё раз: /start")
        finally:
            try:
                await event.answer()
            except errors.RPCError:
                pass

    async def check_code(self, event, state: dict) -> None:
        try:
            await self.user.sign_in(phone=state["phone"], code=state["code"])
        except errors.SessionPasswordNeededError:
            state["step"] = "password"
            await event.edit("🔑 На аккаунте включён облачный пароль (двухэтапная аутентификация).\n"
                             "Пришлите его обычным сообщением — я сразу удалю его из чата.")
            return
        except errors.PhoneCodeInvalidError:
            state["code"] = ""
            await event.edit(code_text("", "❌ Неверный код, попробуйте ещё раз."), buttons=keypad())
            return
        except errors.PhoneCodeExpiredError:
            state["code"] = ""
            await event.edit(code_text("", "⌛️ Код устарел — нажмите «Прислать код заново»."), buttons=keypad())
            return
        except errors.PhoneNumberUnoccupiedError:
            self.states.pop(event.sender_id, None)
            await event.edit("❌ На этот номер нет аккаунта Telegram. Пришлите другой номер: /start")
            return
        await self.finish(event)

    async def check_password(self, event, password: str) -> None:
        try:
            await self.user.sign_in(password=password)
        except errors.PasswordHashInvalidError:
            await event.respond("❌ Неверный пароль. Пришлите его ещё раз.")
            return
        await self.finish(event)

    async def finish(self, event) -> None:
        me = await self.user.get_me()
        self.states.clear()
        await event.respond(
            f"✅ Готово! Вы вошли как <b>{html.escape(utils.get_display_name(me))}</b>.\n"
            "Отправьте /menu — и можно создавать посты."
        )
        if self._done and not self._done.done():
            self._done.set_result((me, event.sender_id))
