"""Планировщик: каждые 15 секунд проверяет, не пора ли что-нибудь опубликовать."""
from __future__ import annotations

import asyncio
import html
import logging
import time
from datetime import datetime, timedelta
from typing import Awaitable, Callable

from telethon import errors

from .config import Config
from .schedule import due_slots, is_expired, until_date
from .sender import FATAL, RETRYABLE, Sender, describe_error
from .storage import PostStore

log = logging.getLogger(__name__)

TICK_SECONDS = 15
MAX_ATTEMPTS = 3


class FatalSessionError(Exception):
    """Аккаунт больше не может работать — скрипт завершается, чтобы войти заново."""


class Scheduler:
    def __init__(self, store: PostStore, sender: Sender, notify: Callable[[str], Awaitable[None]], cfg: Config):
        self.store = store
        self.sender = sender
        self.notify = notify
        self.cfg = cfg
        self._attempts: dict[tuple, int] = {}
        self._was_paused = False

    async def run(self) -> None:
        log.info("Планировщик запущен, часовой пояс %s", self.cfg.tz.key)
        while True:
            try:
                await self.tick()
            except FatalSessionError:
                raise
            except Exception:
                log.exception("Ошибка в планировщике")
            await asyncio.sleep(TICK_SECONDS - time.time() % TICK_SECONDS)

    def _paused(self, now: datetime) -> bool:
        settings = self.sender.settings
        return settings is not None and settings.is_paused(now)

    async def tick(self) -> None:
        self.store.reload()
        now = datetime.now(self.cfg.tz)
        paused = self._paused(now)
        if self._was_paused and not paused:
            await self.notify("▶️ Пауза закончилась — рассылка снова работает по расписанию.")
        self._was_paused = paused

        for post in self.store.all():
            if not post["enabled"]:
                continue
            # выключаем, когда прошли и последний день, и запас на опоздание после полуночи
            if is_expired(post, (now - timedelta(minutes=self.cfg.late_send_minutes)).date()):
                await self._expire(post)
                continue
            if self._paused(datetime.now(self.cfg.tz)):
                continue  # на паузе выпуски пропускаются, а не копятся
            for day, hhmm in due_slots(post, now, self.cfg.late_send_minutes):
                await self._publish(post, day, hhmm)

    async def _expire(self, post: dict) -> None:
        self.store.update(post["id"], enabled=False)
        until = until_date(post)
        log.info("Пост #%s: срок публикации закончился (%s) — выключен", post["id"], until)
        await self.notify(
            f"⏹ Срок публикации поста #{post['id']} закончился ({until:%d.%m.%Y}) — пост выключен.\n"
            "Продлить можно в карточке поста кнопкой «⏳ Срок»."
        )

    async def _title(self, chat) -> str:
        return html.escape(await self.sender.title(chat) or str(chat))

    async def _publish(self, post: dict, day: str, hhmm: str) -> None:
        """Один выпуск поста: отправка в каждый его чат, у каждого чата своя отметка."""
        pending = [c for c in self.sender.targets(post) if not self.store.was_sent(post["id"], day, hhmm, c)]
        if not pending:
            if not self.sender.targets(post) and not self.store.was_sent(post["id"], day, hhmm, None):
                self.store.mark_sent(post["id"], day, hhmm, None)
                await self.notify(f"❌ Пост #{post['id']} ({hhmm}) не отправлен: не выбран ни один чат")
            return
        done, failed, stop = [], [], None
        for chat in pending:
            key = (post["id"], day, hhmm, chat)
            try:
                await self.sender.publish_to(post, chat)
            except FATAL as exc:
                await self.notify(f"⛔️ Рассылка остановлена: {html.escape(describe_error(exc))}.\n"
                                  "Скрипт перезапустится — войдите в аккаунт заново через /start.")
                raise FatalSessionError(describe_error(exc)) from exc
            except RETRYABLE as exc:
                self._attempts[key] = self._attempts.get(key, 0) + 1
                if self._attempts[key] < MAX_ATTEMPTS:
                    log.warning("Пост #%s → %s: %s — повторю через %s с",
                                post["id"], chat, describe_error(exc), TICK_SECONDS)
                    continue
                failed.append((chat, exc))
            except (errors.PeerFloodError, errors.FloodWaitError) as exc:
                # Telegram ограничил аккаунт — остальные чаты не трогаем, чтобы не сделать хуже
                failed.append((chat, exc))
                stop = exc
            except Exception as exc:
                failed.append((chat, exc))
            else:
                done.append(chat)
            # Ошибку не повторяем бесконечно: отмечаем и сообщаем владельцу
            self.store.mark_sent(*key)
            self._attempts.pop(key, None)
            if stop:
                break

        if done:
            names = ", ".join([f"«{await self._title(c)}»" for c in done])
            log.info("Пост #%s опубликован (%s): %s", post["id"], hhmm, names)
            if self.cfg.notify_success:
                await self.notify(f"✅ Пост #{post['id']} опубликован ({hhmm}): {names}")
        for chat, exc in failed:
            log.error("Пост #%s → %s не отправлен: %s", post["id"], chat, describe_error(exc))
            if exc is not stop:
                await self.notify(f"❌ Не удалось опубликовать пост #{post['id']} в «{await self._title(chat)}» "
                                  f"({hhmm}):\n{html.escape(describe_error(exc))}")
        if stop:
            await self._on_limited(stop)

    async def _on_limited(self, exc: Exception) -> None:
        """Telegram ограничил аккаунт: ставим всю рассылку на паузу и объясняем, что делать."""
        settings = self.sender.settings
        if isinstance(exc, errors.PeerFloodError):
            hours = 24
            reason = "Telegram ограничил аккаунт за спам (PEER_FLOOD)"
            advice = ("Откройте @SpamBot и проверьте статус аккаунта. Пока ограничение действует, "
                      "аккаунт не может писать в чаты, где его не знают. Если рассылку не остановить, "
                      "ограничение продлевается, а аккаунт могут заблокировать.")
        else:
            hours = max(1, round(exc.seconds / 3600 + 0.49))
            reason = f"Telegram попросил подождать {exc.seconds} с (FLOOD_WAIT)"
            advice = "Это значит, что сообщений отправляется слишком много. Уменьшите частоту или число чатов."
        until = datetime.now(self.cfg.tz) + timedelta(hours=hours)
        if settings is not None:
            settings.pause(until, reason)
        self._was_paused = True
        await self.notify(
            f"⛔️ <b>Рассылка на паузе до {until:%d.%m %H:%M}</b>\n{html.escape(reason)}.\n\n{advice}\n\n"
            "Снять паузу раньше можно в «⚙️ Настройках»."
        )
