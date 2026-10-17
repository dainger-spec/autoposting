"""Хранение постов в posts.json и отметок об отправке в data/state.json.

posts.json можно править вручную прямо во время работы скрипта:
файл перечитывается, как только меняется.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
from datetime import date, timedelta
from pathlib import Path

from .config import BASE_DIR, parse_chat
from .schedule import ALL_DAYS, parse_date, parse_day, parse_time

log = logging.getLogger(__name__)

MEDIA_TYPES = {"photo", "video", "animation", "document", "audio", "voice", "video_note", "auto"}


def media_path(file: str) -> Path:
    path = Path(file)
    return path if path.is_absolute() else BASE_DIR / path


def _write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def normalize_post(raw: dict, post_id: int) -> dict:
    """Приводит пост (в т.ч. написанный вручную) к единому виду."""
    raw_times = raw.get("times")
    if raw_times is None and raw.get("time"):
        raw_times = [raw["time"]]
    if isinstance(raw_times, str):
        raw_times = re.split(r"[\s,;]+", raw_times)
    times = set()
    for item in raw_times or []:
        try:
            times.add(parse_time(str(item)))
        except ValueError:
            log.warning("Пост #%s: неверное время %r — пропущено", post_id, item)

    raw_days = raw.get("days")
    if raw_days is None:
        raw_days = ALL_DAYS
    elif isinstance(raw_days, (str, int)):
        raw_days = re.split(r"[\s,;]+", str(raw_days))
    days = {d for d in map(parse_day, raw_days) if d}

    raw_media = raw.get("media") or []
    if isinstance(raw_media, (str, dict)):
        raw_media = [raw_media]
    media = []
    for item in raw_media:
        if isinstance(item, str):
            item = {"file": item}
        if not isinstance(item, dict) or not item.get("file"):
            continue
        kind = item.get("type") if item.get("type") in MEDIA_TYPES else "auto"
        media.append({"file": str(item["file"]), "type": kind})

    # Чаты публикации: "chats": [...]; старый формат "chat": одно значение. Пусто — DEFAULT_CHAT.
    raw_chats = raw.get("chats")
    if raw_chats is None:
        raw_chats = [raw.get("chat")]
    elif not isinstance(raw_chats, list):
        raw_chats = [raw_chats]
    chats = []
    for chat in raw_chats:
        if isinstance(chat, str):
            chat = parse_chat(chat)
        elif isinstance(chat, bool) or not isinstance(chat, int):
            chat = None
        if chat is not None and chat not in chats:
            chats.append(chat)

    return {
        "id": post_id,
        "text": str(raw.get("text") or ""),
        "media": media,
        "chats": chats,
        "times": sorted(times),
        "days": sorted(days),
        "enabled": bool(raw.get("enabled", True)),
        "silent": bool(raw.get("silent", False)),
        "link_preview": bool(raw.get("link_preview", True)),
        "mix_letters": bool(raw.get("mix_letters", True)),
        "until": _normalize_until(raw.get("until"), post_id),
        "source": _normalize_source(raw.get("source")),
        "via_channel": bool(raw.get("via_channel", True)),
        "buttons": _normalize_buttons(raw.get("buttons")),
        "channel_post": _normalize_channel_post(raw.get("channel_post")),
    }


def _normalize_buttons(raw) -> list[list[dict]]:
    """Кнопки-ссылки под постом: [[{"text": "Сайт", "url": "https://..."}], ...] — ряды кнопок."""
    rows = []
    for row in raw or []:
        if isinstance(row, dict):
            row = [row]
        if not isinstance(row, list):
            continue
        buttons = [{"text": str(b["text"]), "url": str(b["url"])}
                   for b in row if isinstance(b, dict) and b.get("text") and b.get("url")]
        if buttons:
            rows.append(buttons)
    return rows


def _normalize_channel_post(raw) -> dict | None:
    """Сообщение поста в канале, откуда его пересылают: {"chat", "ids", "key"}."""
    if not isinstance(raw, dict) or not isinstance(raw.get("chat"), (int, str)):
        return None
    ids = [i for i in raw.get("ids") or [] if _int_or_none(i) is not None]
    return {"chat": raw["chat"], "ids": ids, "key": str(raw.get("key") or "")} if ids else None


def _normalize_until(raw, post_id: int) -> str | None:
    """Последний день публикации: '2026-12-31' или '31.12.2026'; пусто — бессрочно."""
    if not raw:
        return None
    try:
        return date.fromisoformat(str(raw)).isoformat()
    except ValueError:
        pass
    try:
        return parse_date(str(raw), date.today()).isoformat()
    except ValueError:
        log.warning("Пост #%s: неверная дата окончания %r — пост будет бессрочным", post_id, raw)
        return None


def _int_or_none(value) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _normalize_source(raw) -> dict | None:
    """Ссылка на исходное сообщение в вашем чате с ботом — из него пост копируется как есть."""
    if not isinstance(raw, dict) or _int_or_none(raw.get("peer")) is None:
        return None
    media_ids = [i for i in raw.get("media_ids") or [] if _int_or_none(i) is not None]
    return {
        "peer": raw["peer"],
        "text_id": _int_or_none(raw.get("text_id")),
        "media_ids": media_ids,
        "media_files": [str(f) for f in raw.get("media_files") or []],
    }


class PostStore:
    def __init__(self, posts_file: Path, state_file: Path, media_dir: Path):
        self.posts_file = posts_file
        self.state_file = state_file
        self.media_dir = media_dir
        self._posts: dict[int, dict] = {}
        self._mtime: int | None = None
        self._sent: dict[str, list[str]] = self._load_state()
        self.reload(force=True)

    # ── посты ──────────────────────────────────────────────────────

    def reload(self, force: bool = False) -> None:
        """Перечитывает posts.json, если он изменился с прошлого раза."""
        try:
            mtime = self.posts_file.stat().st_mtime_ns
        except FileNotFoundError:
            log.info("Файл %s не найден — создаю пустой", self.posts_file.name)
            self._posts = {}
            self._save()
            return
        if mtime == self._mtime and not force:
            return
        self._mtime = mtime  # даже при ошибке, чтобы не повторять её каждые 15 секунд

        try:
            data = json.loads(self.posts_file.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            log.error("Не удалось прочитать %s: %s. Работаю с прежней версией постов.", self.posts_file.name, exc)
            return
        raw_posts = data.get("posts") if isinstance(data, dict) else data
        if not isinstance(raw_posts, list):
            log.error("В %s нет списка posts. Работаю с прежней версией постов.", self.posts_file.name)
            return

        raw_posts = [p for p in raw_posts if isinstance(p, dict)]
        ids = [p.get("id") for p in raw_posts]
        next_id = max((i for i in ids if isinstance(i, int) and not isinstance(i, bool)), default=0) + 1
        posts, need_save = {}, False
        for raw in raw_posts:
            post_id = raw.get("id")
            if not isinstance(post_id, int) or isinstance(post_id, bool) or post_id < 1 or post_id in posts:
                post_id, next_id, need_save = next_id, next_id + 1, True
            posts[post_id] = normalize_post(raw, post_id)

        self._posts = posts
        log.info("Загружено постов: %d", len(posts))
        if need_save:  # присвоили id постам, добавленным вручную
            self._save()

    def _save(self) -> None:
        _write_json(self.posts_file, {"posts": self.all()})
        self._mtime = self.posts_file.stat().st_mtime_ns

    def all(self) -> list[dict]:
        return [self._posts[i] for i in sorted(self._posts)]

    def get(self, post_id: int) -> dict | None:
        self.reload()
        return self._posts.get(post_id)

    def add(self, **fields) -> dict:
        self.reload()
        post_id = max(self._posts, default=0) + 1
        if self._sent.pop(str(post_id), None) is not None:  # остались от удалённого поста
            self._save_state()
        post = normalize_post(fields, post_id)
        self._posts[post_id] = post
        self._save()
        return post

    def update(self, post_id: int, **fields) -> dict | None:
        self.reload()
        if post_id not in self._posts:
            return None
        post = normalize_post({**self._posts[post_id], **fields}, post_id)
        self._posts[post_id] = post
        self._save()
        return post

    def delete(self, post_id: int) -> dict | None:
        self.reload()
        post = self._posts.pop(post_id, None)
        if post:
            self._save()
            self._sent.pop(str(post_id), None)
            self._save_state()
            self.release_media(post["media"])
        return post

    def release_media(self, items: list[dict]) -> None:
        """Удаляет загруженные ботом файлы, если они больше не нужны ни одному посту.

        Файлы вне папки data/media (указанные вручную) никогда не трогаются.
        """
        in_use = {media_path(m["file"]).resolve() for p in self._posts.values() for m in p["media"]}
        media_dir = self.media_dir.resolve()
        for item in items:
            path = media_path(item["file"]).resolve()
            if path in in_use or not path.is_relative_to(media_dir):
                continue
            if path.parent != media_dir:
                shutil.rmtree(path.parent, ignore_errors=True)
            else:
                path.unlink(missing_ok=True)

    # ── отметки об отправке ────────────────────────────────────────

    def _load_state(self) -> dict[str, list[str]]:
        try:
            data = json.loads(self.state_file.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            log.warning("Не удалось прочитать %s: %s", self.state_file, exc)
            return {}
        sent = data.get("sent", {}) if isinstance(data, dict) else {}
        return {str(k): list(v) for k, v in sent.items() if isinstance(v, list)}

    def _save_state(self) -> None:
        _write_json(self.state_file, {"sent": self._sent})

    def was_sent(self, post_id: int, day: str, hhmm: str, chat) -> bool:
        return f"{day} {hhmm} {chat}" in self._sent.get(str(post_id), ())

    def mark_sent(self, post_id: int, day: str, hhmm: str, chat) -> None:
        """Отметка ставится отдельно для каждого чата: сбой в одном не мешает остальным."""
        self._sent.setdefault(str(post_id), []).append(f"{day} {hhmm} {chat}")
        # старые отметки не нужны: хранится только пара последних дней
        cutoff = (date.today() - timedelta(days=3)).isoformat()
        self._sent = {
            pid: [s for s in slots if s >= cutoff]
            for pid, slots in self._sent.items()
            if pid.isdigit() and int(pid) in self._posts
        }
        self._save_state()
