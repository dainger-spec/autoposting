"""Расписание: разбор времени и дней, поиск постов, которые пора отправить."""
from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta

ALL_DAYS = [1, 2, 3, 4, 5, 6, 7]  # 1 — понедельник, 7 — воскресенье
WEEKDAYS = [1, 2, 3, 4, 5]
WEEKEND = [6, 7]
DAY_NAMES = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]

# Чтобы в posts.json дни можно было писать и словами: ["пн", "ср", "пт"]
DAY_ALIASES = {
    **{name.lower(): i for i, name in enumerate(DAY_NAMES, 1)},
    **{name: i for i, name in enumerate(["mon", "tue", "wed", "thu", "fri", "sat", "sun"], 1)},
}

_TIME_RE = re.compile(r"(\d{1,2})[:.](\d{2})")


def parse_time(value: str) -> str:
    """'9:00', '09.00' -> '09:00'. Бросает ValueError при неверном формате."""
    match = _TIME_RE.fullmatch(value.strip())
    if not match:
        raise ValueError(value)
    hours, minutes = int(match[1]), int(match[2])
    if hours > 23 or minutes > 59:
        raise ValueError(value)
    return f"{hours:02d}:{minutes:02d}"


def parse_times(text: str) -> list[str]:
    """'9:00, 18:30 21:00' -> ['09:00', '18:30', '21:00']."""
    items = [item for item in re.split(r"[\s,;]+", text.strip()) if item]
    if not items:
        raise ValueError("пусто")
    result, bad = set(), []
    for item in items:
        try:
            result.add(parse_time(item))
        except ValueError:
            bad.append(item)
    if bad:
        raise ValueError(", ".join(bad))
    return sorted(result)


def parse_day(value) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if 1 <= value <= 7 else None
    value = str(value).strip().lower()
    if value.isdigit():
        return parse_day(int(value))
    return DAY_ALIASES.get(value[:3]) or DAY_ALIASES.get(value[:2])


def format_days(days: list[int]) -> str:
    days = sorted(set(days))
    if days == ALL_DAYS:
        return "каждый день"
    if days == WEEKDAYS:
        return "по будням"
    if days == WEEKEND:
        return "по выходным"
    if not days:
        return "не выбраны"
    return ", ".join(DAY_NAMES[d - 1] for d in days)


def _slot(day: date, hhmm: str, tz) -> datetime:
    hours, minutes = map(int, hhmm.split(":"))
    return datetime.combine(day, time(hours, minutes), tzinfo=tz)


def due_slots(post: dict, now: datetime, late_minutes: int) -> list[tuple[str, str]]:
    """Слоты (дата, время), которые наступили не больше late_minutes назад.

    Вчерашний день тоже проверяется, чтобы пост на 23:58 не потерялся,
    если проверка случилась уже после полуночи.
    """
    late = timedelta(minutes=late_minutes)
    result = []
    for day in (now.date() - timedelta(days=1), now.date()):
        if day.isoweekday() not in post["days"]:
            continue
        for hhmm in post["times"]:
            slot = _slot(day, hhmm, now.tzinfo)
            if slot <= now < slot + late:
                result.append((day.isoformat(), hhmm))
    return result


def next_run(post: dict, now: datetime) -> datetime | None:
    if not post["enabled"] or not post["times"] or not post["days"]:
        return None
    for offset in range(8):
        day = now.date() + timedelta(days=offset)
        if day.isoweekday() not in post["days"]:
            continue
        for hhmm in post["times"]:
            slot = _slot(day, hhmm, now.tzinfo)
            if slot > now:
                return slot
    return None


def format_when(moment: datetime, now: datetime) -> str:
    delta = (moment.date() - now.date()).days
    hhmm = moment.strftime("%H:%M")
    if delta == 0:
        return f"сегодня в {hhmm}"
    if delta == 1:
        return f"завтра в {hhmm}"
    return f"{DAY_NAMES[moment.isoweekday() - 1]}, {moment:%d.%m} в {hhmm}"
