"""Подмена части букв на одинаково выглядящие из другого алфавита.

«benji» -> «bеnji» (е — русская), «vaer» -> «vаеr» (а и е — русские).
В русских словах наоборот: часть букв меняется на латинские двойники.
Ссылки, @юзернеймы, #хештеги, почта, код и т.п. не трогаются; форматирование
сохраняется, потому что каждая буква меняется ровно на одну другую.
"""
from __future__ import annotations

import random
import re

from telethon import types
from telethon.helpers import add_surrogate, del_surrogate

LAT_TO_CYR = {
    "a": "а", "c": "с", "e": "е", "o": "о", "p": "р", "x": "х", "y": "у",
    "A": "А", "B": "В", "C": "С", "E": "Е", "H": "Н", "K": "К", "M": "М",
    "O": "О", "P": "Р", "T": "Т", "X": "Х", "Y": "У",
}
CYR_TO_LAT = {cyr: lat for lat, cyr in LAT_TO_CYR.items()}

# Части текста, которые менять нельзя
PROTECTED_ENTITIES = (
    types.MessageEntityUrl, types.MessageEntityTextUrl, types.MessageEntityMention,
    types.MessageEntityMentionName, types.InputMessageEntityMentionName,
    types.MessageEntityHashtag, types.MessageEntityCashtag, types.MessageEntityBotCommand,
    types.MessageEntityEmail, types.MessageEntityPhone, types.MessageEntityCode,
    types.MessageEntityPre, types.MessageEntityBankCard, types.MessageEntityCustomEmoji,
)
# То же для текста без разметки: ссылки, домены, @юзернеймы, #хештеги, /команды, почта
PROTECTED_RE = re.compile(
    r"(?:https?://|www\.)\S+"
    r"|\b[\w-]+(?:\.[\w-]+)*\.[A-Za-z]{2,}(?:/\S*)?"
    r"|[@#$/]\w+"
    r"|\S+@\S+"
)
WORD_RE = re.compile(r"[A-Za-zА-Яа-яЁё]+")

WORD_CHANCE = 0.6  # доля слов, в которых меняются буквы
MIN_WORD_LENGTH = 3


def mix_letters(text: str, entities=None, rng: random.Random | None = None) -> str:
    rng = rng or random.Random()
    # Смещения разметки Telegram считаются в UTF-16 — работаем в тех же единицах
    chars = list(add_surrogate(text))
    protected = [False] * len(chars)

    def protect(start: int, end: int) -> None:
        for i in range(max(start, 0), min(end, len(chars))):
            protected[i] = True

    for entity in entities or ():
        if isinstance(entity, PROTECTED_ENTITIES):
            protect(entity.offset, entity.offset + entity.length)
    joined = "".join(chars)
    for match in PROTECTED_RE.finditer(joined):
        protect(*match.span())

    for match in WORD_RE.finditer(joined):
        start, end = match.span()
        word = match.group()
        if len(word) < MIN_WORD_LENGTH or any(protected[start:end]):
            continue
        latin = sum(ch.isascii() for ch in word)
        if latin == len(word):
            table = LAT_TO_CYR
        elif latin == 0:
            table = CYR_TO_LAT
        else:
            continue  # слово уже смешанное
        spots = [i for i, ch in enumerate(word) if ch in table]
        if not spots or rng.random() > WORD_CHANCE:
            continue
        for i in rng.sample(spots, rng.randint(1, len(spots))):
            chars[start + i] = table[word[i]]
    return del_surrogate("".join(chars))
