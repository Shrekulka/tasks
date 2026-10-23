# forest-advice-bot/services/chunker.py

"""Деление сегментов на фрагменты для индекса. Чистые функции без сети и БД.

Границы проходят по абзацам, затем по предложениям, затем по словам. Перекрытие состоит из
целых предложений, поэтому фрагмент не начинается посреди слова. Фрагмент не пересекает
границу «страница, раздел»: адрес для цитирования (page, section) остаётся точным.
"""

import re
from collections.abc import Iterable
from dataclasses import dataclass

from services.parsers import Segment

_PARAGRAPH_RE = re.compile(r"\n{2,}")
_SENTENCE_RE = re.compile(r"(?<=[.!?…])\s+")


@dataclass(frozen=True, slots=True)
class TextChunk:
    index: int
    text: str
    page: int | None
    section: str | None


def chunk_segments(segments: Iterable[Segment], *, size: int, overlap: int) -> list[TextChunk]:
    """size: максимум символов во фрагменте. overlap: желаемое перекрытие, меньше size."""
    if size < 1 or overlap < 0 or overlap >= size:
        raise ValueError("нужно 0 <= overlap < size и size >= 1")

    chunks: list[TextChunk] = []
    for (page, section), text in _group(segments):
        for piece in split_text(text, size=size, overlap=overlap):
            chunks.append(TextChunk(index=len(chunks), text=piece, page=page, section=section))
    return chunks


def split_text(text: str, *, size: int, overlap: int) -> list[str]:
    """Делит один текст на фрагменты не длиннее size."""
    chunks: list[str] = []
    current: list[str] = []
    length = 0

    for unit in _units(text, size):
        extra = len(unit) + (1 if current else 0)
        if current and length + extra > size:
            chunks.append(" ".join(current))
            current = _overlap_tail(current, overlap, room=size - len(unit) - 1)
            length = len(" ".join(current))
            extra = len(unit) + (1 if current else 0)
        current.append(unit)
        length += extra

    if current:
        chunks.append(" ".join(current))
    return chunks


# ---------------------------------------------------------------------- внутреннее


def _group(segments: Iterable[Segment]) -> list[tuple[tuple[int | None, str | None], str]]:
    """Склеивает подряд идущие сегменты с одним адресом (page, section)."""
    groups: list[tuple[tuple[int | None, str | None], list[str]]] = []
    for segment in segments:
        if not segment.text.strip():
            continue
        address = (segment.page, segment.section)
        if groups and groups[-1][0] == address:
            groups[-1][1].append(segment.text)
        else:
            groups.append((address, [segment.text]))
    return [(address, "\n\n".join(parts)) for address, parts in groups]


def _units(text: str, size: int) -> list[str]:
    """Неделимые единицы: абзацы, а слишком длинные абзацы по предложениям и словам."""
    units: list[str] = []
    for paragraph in _PARAGRAPH_RE.split(text):
        paragraph = " ".join(paragraph.split())
        if not paragraph:
            continue
        if len(paragraph) <= size:
            units.append(paragraph)
            continue
        for sentence in _SENTENCE_RE.split(paragraph):
            if len(sentence) <= size:
                units.append(sentence)
            else:
                units.extend(_split_words(sentence, size))
    return units


def _split_words(sentence: str, size: int) -> list[str]:
    """Последняя линия обороны: предложение длиннее size режется по словам, затем по символам."""
    parts: list[str] = []
    current = ""
    for word in sentence.split():
        while len(word) > size:  # слово длиннее фрагмента (например, ссылка)
            if current:
                parts.append(current)
                current = ""
            parts.append(word[:size])
            word = word[size:]
        candidate = f"{current} {word}" if current else word
        if len(candidate) <= size:
            current = candidate
        else:
            parts.append(current)
            current = word
    if current:
        parts.append(current)
    return parts


def _overlap_tail(units: list[str], overlap: int, *, room: int) -> list[str]:
    """Хвост предыдущего фрагмента (целые единицы), который войдёт в начало следующего."""
    budget = min(overlap, room)
    tail: list[str] = []
    total = 0
    for unit in reversed(units):
        add = len(unit) + (1 if tail else 0)
        if total + add > budget:
            break
        tail.insert(0, unit)
        total += add
    return tail
