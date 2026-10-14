"""Загрузка настроек из файла .env."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Config:
    api_id: int
    api_hash: str
    bot_token: str
    phone: str | None
    admin_ids: frozenset[int]
    default_chat: str | int | None
    tz: ZoneInfo
    late_send_minutes: int
    notify_success: bool
    posts_file: Path
    data_dir: Path = DATA_DIR

    @property
    def state_file(self) -> Path:
        return self.data_dir / "state.json"

    @property
    def media_dir(self) -> Path:
        return self.data_dir / "media"


def parse_chat(value: str) -> str | int | None:
    """'@chat', 'https://t.me/chat', 'me' остаются строкой, '-100123' становится числом."""
    value = value.strip()
    if not value:
        return None
    if re.fullmatch(r"-?\d+", value):
        return int(value)
    return value


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ConfigError(f"не задан параметр {name} в файле .env (пример — в .env.example)")
    return value


def _int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        raise ConfigError(f"{name} должен быть числом, а не {raw!r}") from None


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on", "да")


def load_config() -> Config:
    load_dotenv(BASE_DIR / ".env")

    api_id = _required("API_ID")
    if not api_id.isdigit():
        raise ConfigError("API_ID должен быть числом")

    admin_ids = set()
    for item in re.split(r"[\s,;]+", os.getenv("ADMIN_IDS", "")):
        if not item:
            continue
        if not re.fullmatch(r"\d+", item):
            raise ConfigError(f"ADMIN_IDS: {item!r} не похоже на Telegram ID")
        admin_ids.add(int(item))

    tz_name = os.getenv("TIMEZONE", "").strip() or "Europe/Moscow"
    try:
        tz = ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError):
        raise ConfigError(f"неизвестный часовой пояс TIMEZONE={tz_name!r}") from None

    posts_file = Path(os.getenv("POSTS_FILE", "").strip() or "posts.json")
    if not posts_file.is_absolute():
        posts_file = BASE_DIR / posts_file

    return Config(
        api_id=int(api_id),
        api_hash=_required("API_HASH"),
        bot_token=_required("BOT_TOKEN"),
        phone=os.getenv("PHONE", "").strip() or None,
        admin_ids=frozenset(admin_ids),
        default_chat=parse_chat(os.getenv("DEFAULT_CHAT", "")),
        tz=tz,
        late_send_minutes=max(1, _int("LATE_SEND_MINUTES", 10)),
        notify_success=_bool("NOTIFY_ON_SUCCESS", True),
        posts_file=posts_file,
    )
