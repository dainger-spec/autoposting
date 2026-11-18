"""Обслуживание диска: без него за несколько дней хостинг упирается в
«database or disk is full» и бот перестаёт отвечать.

- удаляет из data/media файлы брошенных черновиков;
- чистит кэш собеседников в .session-файлах Telethon (таблица entities растёт
  от каждого апдейта из групп) и сжимает базу (VACUUM).
"""
from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from pathlib import Path

log = logging.getLogger(__name__)

ENTITY_TTL = 14 * 24 * 3600   # сколько хранить запись о собеседнике
INTERVAL = 6 * 3600          # как часто чистить


def in_memory(path: str):
    """Сессия в памяти вместо SQLite-файла, если в файле уже есть вход.

    SQLite-сессия Telethon пишет на диск на каждый апдейт; на хостинге с забитым
    диском это валит бота («database or disk is full»). Файл читается один раз,
    дальше на диск ничего не пишется. Если входа ещё нет — возвращается путь,
    и вход сохраняется в файл как обычно.
    """
    from telethon.sessions import SQLiteSession, StringSession
    if not Path(path + ".session").is_file():
        return path
    try:
        file_session = SQLiteSession(path)
        try:
            if file_session.auth_key is None:
                return path
            return StringSession(StringSession.save(file_session))
        finally:
            file_session.close()
    except Exception as exc:
        log.warning("Не удалось прочитать сессию %s в память: %s", path, exc)
        return path


def prune_session(client) -> None:
    """Удаляет старые записи entities из SQLite-сессии клиента и сжимает файл."""
    session = client.session
    if not hasattr(session, "_cursor"):  # StringSession/MemorySession — на диске ничего нет
        return
    try:
        session.save()
        cur = session._cursor()
        conn = session._conn
        cur.execute("delete from entities where date is null or date < ?", (int(time.time()) - ENTITY_TTL,))
        deleted = cur.rowcount
        cur.close()
        conn.commit()
        conn.execute("vacuum")
        if deleted > 0:
            log.info("Сессия %s: удалено старых записей кэша: %d", client.session.filename, deleted)
    except sqlite3.Error as exc:
        log.warning("Не удалось почистить сессию %s: %s", getattr(client.session, "filename", "?"), exc)


def run_once(store, clients, keep_media=()) -> None:
    try:
        store.cleanup_media(keep_media)
    except OSError as exc:
        log.warning("Не удалось почистить медиа: %s", exc)
    for client in clients:
        prune_session(client)


async def loop(store, clients, keep_media_fn) -> None:
    while True:
        await asyncio.sleep(INTERVAL)
        run_once(store, clients, keep_media_fn())
