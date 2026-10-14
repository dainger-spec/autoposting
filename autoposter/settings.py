"""Настройки, которые меняются прямо в боте (хранятся в data/settings.json)."""
from __future__ import annotations

import json
import logging
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
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8")
