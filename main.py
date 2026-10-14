"""Автопостинг в Telegram с вашего аккаунта по расписанию, управление — через бота.

Запуск:  python main.py
При первом запуске попросит номер телефона и код из Telegram (один раз).
"""
from __future__ import annotations

import asyncio
import getpass
import logging
import sys

from telethon import TelegramClient

from autoposter.bot import ControlBot
from autoposter.config import Config, ConfigError, load_config
from autoposter.scheduler import Scheduler
from autoposter.sender import Sender
from autoposter.storage import PostStore

log = logging.getLogger("autoposter")


async def run(cfg: Config) -> None:
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    store = PostStore(cfg.posts_file, cfg.state_file, cfg.media_dir)

    # Две сессии: ваш аккаунт (от него идут посты) и бот (через него управление)
    user = TelegramClient(str(cfg.data_dir / "user"), cfg.api_id, cfg.api_hash)
    # имя сессии бота привязано к его ID, чтобы смена BOT_TOKEN не подхватила старого бота
    bot_id = cfg.bot_token.split(":", 1)[0]
    bot = TelegramClient(str(cfg.data_dir / f"bot_{bot_id}"), cfg.api_id, cfg.api_hash)

    await user.start(
        phone=cfg.phone or (lambda: input("Номер телефона вашего аккаунта (например +79991234567): ")),
        code_callback=lambda: input("Код из Telegram: "),
        password=lambda: getpass.getpass("Облачный пароль (двухэтапная аутентификация): "),
    )
    me = await user.get_me()
    if me.bot:
        raise ConfigError("в сессию аккаунта вошли ботом — удалите data/user.session и войдите по номеру телефона")

    await bot.start(bot_token=cfg.bot_token)
    bot_me = await bot.get_me()

    try:
        sender = Sender(user, cfg.default_chat)
        control = ControlBot(bot, cfg, store, sender, admins={me.id, *cfg.admin_ids})
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
