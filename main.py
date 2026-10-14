"""Автопостинг в Telegram с вашего аккаунта по расписанию, управление — через бота.

Запуск:  python main.py
При первом запуске попросит номер телефона и код из Telegram (один раз).
На хостинге без консоли вход в аккаунт проходит прямо в чате с ботом.
"""
from __future__ import annotations

import asyncio
import logging
import sys

from telethon import TelegramClient
from telethon.sessions import StringSession

from autoposter.auth import has_console, load_owner, login_user, save_owner
from autoposter.bot import ControlBot
from autoposter.botlogin import BotLogin
from autoposter.config import Config, ConfigError, load_config
from autoposter.scheduler import Scheduler
from autoposter.sender import Sender
from autoposter.storage import PostStore

log = logging.getLogger("autoposter")


async def run(cfg: Config) -> None:
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    store = PostStore(cfg.posts_file, cfg.state_file, cfg.media_dir)

    # Две сессии: ваш аккаунт (от него идут посты) и бот (через него управление).
    # На хостинге без консоли вход в аккаунт задаётся строкой SESSION_STRING (см. login.py).
    user_session = str(cfg.data_dir / "user")
    if cfg.session_string:
        try:
            user_session = StringSession(cfg.session_string)
        except Exception:
            raise ConfigError("SESSION_STRING повреждена — скопируйте строку из python login.py целиком") from None
    user = TelegramClient(user_session, cfg.api_id, cfg.api_hash)
    # имя сессии бота привязано к его ID, чтобы смена BOT_TOKEN не подхватила старого бота
    bot_id = cfg.bot_token.split(":", 1)[0]
    bot = TelegramClient(str(cfg.data_dir / f"bot_{bot_id}"), cfg.api_id, cfg.api_hash)

    await bot.start(bot_token=cfg.bot_token)
    bot.parse_mode = "html"
    bot_me = await bot.get_me()

    owner_file = cfg.data_dir / "owner.json"
    owner_id = load_owner(owner_file)
    await user.connect()
    if cfg.session_string or has_console() or await user.is_user_authorized():
        me = await login_user(user, cfg.phone, from_string=bool(cfg.session_string))
    else:
        # Консоли нет (хостинг) — входим в аккаунт прямо в чате с ботом
        log.info("Нужно войти в аккаунт: откройте @%s в Telegram и отправьте /start", bot_me.username)
        me, owner_id = await BotLogin(bot, user, cfg.admin_ids).run()
        save_owner(owner_file, owner_id)

    try:
        sender = Sender(user, cfg.default_chat)
        await sender.attach_bot(me, bot_me)
        admins = {me.id, *cfg.admin_ids} | ({owner_id} if owner_id else set())
        control = ControlBot(bot, cfg, store, sender, admins=admins)
        await control.setup()
        scheduler = Scheduler(store, sender, control.notify, cfg)

        log.info("Аккаунт: %s (id %s). Бот управления: @%s", me.first_name, me.id, bot_me.username)
        log.info("Откройте @%s в Telegram и отправьте /start", bot_me.username)
        await asyncio.gather(
            scheduler.run(),
            user.run_until_disconnected(),
            bot.run_until_disconnected(),
        )
    finally:
        await bot.disconnect()
        await user.disconnect()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    logging.getLogger("telethon").setLevel(logging.WARNING)
    try:
        cfg = load_config()
        asyncio.run(run(cfg))
    except ConfigError as exc:
        print(f"Ошибка настроек: {exc}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("Остановлено.")


if __name__ == "__main__":
    main()
