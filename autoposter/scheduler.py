"""Планировщик: каждые 15 секунд проверяет, не пора ли что-нибудь опубликовать."""
from __future__ import annotations

import asyncio
import html
import logging
import time
from datetime import datetime, timedelta
from typing import Awaitable, Callable

from .config import Config
from .schedule import due_slots, is_expired, until_date
from .sender import RETRYABLE, Sender, describe_error
from .storage import PostStore

log = logging.getLogger(__name__)

TICK_SECONDS = 15
MAX_ATTEMPTS = 3


class Scheduler:
    def __init__(self, store: PostStore, sender: Sender, notify: Callable[[str], Awaitable[None]], cfg: Config):
        self.store = store
        self.sender = sender
        self.notify = notify
        self.cfg = cfg
        self._attempts: dict[tuple, int] = {}

    async def run(self) -> None:
        log.info("Планировщик запущен, часовой пояс %s", self.cfg.tz.key)
        while True:
            try:
                await self.tick()
            except Exception:
                log.exception("Ошибка в планировщике")
            await asyncio.sleep(TICK_SECONDS - time.time() % TICK_SECONDS)

    async def tick(self) -> None:
        self.store.reload()
        now = datetime.now(self.cfg.tz)
        for post in self.store.all():
            if not post["enabled"]:
                continue
            # выключаем, когда прошли и последний день, и запас на опоздание после полуночи
            if is_expired(post, (now - timedelta(minutes=self.cfg.late_send_minutes)).date()):
                await self._expire(post)
                continue
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
        done, failed = [], []
        for chat in pending:
            key = (post["id"], day, hhmm, chat)
            try:
                await self.sender.publish_to(post, chat)
            except RETRYABLE as exc:
                self._attempts[key] = self._attempts.get(key, 0) + 1
                if self._attempts[key] < MAX_ATTEMPTS:
                    log.warning("Пост #%s → %s: %s — повторю через %s с",
                                post["id"], chat, describe_error(exc), TICK_SECONDS)
                    continue
                failed.append((chat, exc))
            except Exception as exc:
                failed.append((chat, exc))
            else:
                done.append(chat)
            # Ошибку не повторяем бесконечно: отмечаем и сообщаем владельцу
            self.store.mark_sent(*key)
            self._attempts.pop(key, None)
            await asyncio.sleep(1)  # небольшая пауза между отправками, чтобы не упереться в лимиты

        if done:
            names = ", ".join([f"«{await self._title(c)}»" for c in done])
            log.info("Пост #%s опубликован (%s): %s", post["id"], hhmm, names)
            if self.cfg.notify_success:
                await self.notify(f"✅ Пост #{post['id']} опубликован ({hhmm}): {names}")
        for chat, exc in failed:
            log.error("Пост #%s → %s не отправлен: %s", post["id"], chat, describe_error(exc))
            await self.notify(f"❌ Не удалось опубликовать пост #{post['id']} в «{await self._title(chat)}» "
                              f"({hhmm}):\n{html.escape(describe_error(exc))}")
