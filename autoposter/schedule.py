"""Расписание: разбор времени и дней, поиск постов, которые пора отправить."""
from __future__ import annotations

import calendar
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
_DATE_RE = re.compile(r"(\d{1,2})[./-](\d{1,2})(?:[./-](\d{2}|\d{4}))?")

# Сроки публикации: код -> (подпись, месяцев, дней)
PERIODS = {
    "w1": ("1 неделя", 0, 7),
    "w2": ("2 недели", 0, 14),
    "m1": ("1 месяц", 1, 0),
    "m2": ("2 месяца", 2, 0),
    "m3": ("3 месяца", 3, 0),
    "m6": ("6 месяцев", 6, 0),
    "y1": ("1 год", 12, 0),
}


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


def add_months(day: date, months: int) -> date:
    year, month = divmod(day.month - 1 + months, 12)
    year, month = day.year + year, month + 1
    return day.replace(year=year, month=month, day=min(day.day, calendar.monthrange(year, month)[1]))


def period_end(start: date, code: str) -> date:
    """Последний день публикации (включительно), если начать сегодня: 1 неделя = 7 дней."""
    _, months, days = PERIODS[code]
    return add_months(start, months) + timedelta(days=days - 1)


def parse_date(text: str, today: date) -> date:
    """'31.12.2026', '31.12.26', '31.12' (ближайшее такое число). Бросает ValueError."""
    match = _DATE_RE.fullmatch(text.strip())
    if not match:
        raise ValueError(text)
    day, month = int(match[1]), int(match[2])
    if match[3]:
        year = int(match[3]) + (2000 if len(match[3]) == 2 else 0)
        return date(year, month, day)
    result = date(today.year, month, day)
    return result if result >= today else date(today.year + 1, month, day)


def until_date(post: dict) -> date | None:
    return date.fromisoformat(post["until"]) if post.get("until") else None


def is_expired(post: dict, today: date) -> bool:
    until = until_date(post)
    return until is not None and today > until


def format_until(post: dict, today: date) -> str:
    until = until_date(post)
    if until is None:
        return "♾ пока не выключу"
    if until < today:
        return f"⌛️ закончился {until:%d.%m.%Y}"
    left = (until - today).days + 1
    return f"до {until:%d.%m.%Y} включительно (осталось дней: {left})"


def _slot(day: date, hhmm: str, tz) -> datetime:
    hours, minutes = map(int, hhmm.split(":"))
    return datetime.combine(day, time(hours, minutes), tzinfo=tz)


def due_slots(post: dict, now: datetime, late_minutes: int) -> list[tuple[str, str]]:
    """Слоты (дата, время), которые наступили не больше late_minutes назад.

    Вчерашний день тоже проверяется, чтобы пост на 23:58 не потерялся,
    если проверка случилась уже после полуночи.
    """
    late = timedelta(minutes=late_minutes)
    until = until_date(post)
    result = []
    for day in (now.date() - timedelta(days=1), now.date()):
        if day.isoweekday() not in post["days"] or (until and day > until):
            continue
        for hhmm in post["times"]:
            slot = _slot(day, hhmm, now.tzinfo)
            if slot <= now < slot + late:
                result.append((day.isoformat(), hhmm))
    return result


def next_run(post: dict, now: datetime) -> datetime | None:
    if not post["enabled"] or not post["times"] or not post["days"]:
        return None
    until = until_date(post)
    for offset in range(8):
        day = now.date() + timedelta(days=offset)
        if until and day > until:
            return None
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
