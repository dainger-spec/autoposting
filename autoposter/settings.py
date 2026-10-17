"""Настройки, которые меняются прямо в боте (хранятся в data/settings.json)."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)


class Settings:
    def __init__(self, path: Path, default_channel: str | int | None = None):
        self.path = path
        self.default_channel = default_channel
        try:
            self._data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            self._data = {}
        except (OSError, ValueError) as exc:
            log.warning("Не удалось прочитать %s: %s", path, exc)
            self._data = {}

    @property
    def channel(self) -> str | int | None:
        """Канал, через который пересылаются посты. None — посты идут в чат напрямую."""
        if "channel" in self._data:  # задан в боте — главнее, чем CHANNEL в .env
            return self._data["channel"]
        return self.default_channel

    @channel.setter
    def channel(self, value: str | int | None) -> None:
        self._data["channel"] = value
        self._save()

    @property
    def paused_until(self) -> datetime | None:
        """До какого момента рассылка на паузе (None — работает)."""
        raw = self._data.get("paused_until")
        try:
            return datetime.fromisoformat(raw) if raw else None
        except ValueError:
            return None

    def pause(self, until: datetime | None, reason: str = "") -> None:
        self._data["paused_until"] = until.astimezone(timezone.utc).isoformat() if until else None
        self._data["pause_reason"] = reason if until else ""
        self._save()

    @property
    def pause_reason(self) -> str:
        return self._data.get("pause_reason") or ""

    def is_paused(self, now: datetime) -> bool:
        until = self.paused_until
        return until is not None and now < until

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8")
