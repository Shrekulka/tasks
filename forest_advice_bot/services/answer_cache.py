# forest-advice-bot/services/answer_cache.py

"""Кэш ответов. Ключ включает версию коллекции документов, поэтому после добавления или
удаления документа старые ответы не возвращаются."""

import hashlib
import time
from collections import OrderedDict

from utils.utils import Clock


def normalize_question(question: str) -> str:
    """Регистр, пробелы и знаки в конце не должны давать разные ключи."""
    return " ".join(question.casefold().split()).strip(" ?!.…")


class AnswerCache[V]:
    def __init__(
        self,
        *,
        ttl_seconds: float,
        max_entries: int,
        clock: Clock = time.monotonic,
    ) -> None:
        self._ttl = ttl_seconds
        self._max = max_entries
        self._clock = clock
        self._entries: OrderedDict[str, tuple[float, V]] = OrderedDict()
        self._version = 0

    @property
    def enabled(self) -> bool:
        return self._ttl > 0

    @property
    def version(self) -> int:
        return self._version

    def bump(self) -> None:
        """Коллекция изменилась: все прежние ответы устарели."""
        self._version += 1
        self._entries.clear()

    def make_key(self, question: str, *parts: object) -> str:
        """parts: всё, от чего зависит ответ (версия промпта, модель эмбеддингов, top_k, ...)."""
        raw = "|".join([str(self._version), *map(str, parts), normalize_question(question)])
        return hashlib.sha256(raw.encode()).hexdigest()

    def get(self, key: str) -> V | None:
        entry = self._entries.get(key)
        if entry is None:
            return None
        stored_at, value = entry
        if self._clock() - stored_at >= self._ttl:
            del self._entries[key]
            return None
        self._entries.move_to_end(key)
        return value

    def put(self, key: str, value: V) -> None:
        if not self.enabled:
            return
        self._entries[key] = (self._clock(), value)
        self._entries.move_to_end(key)
        while len(self._entries) > self._max:
            self._entries.popitem(last=False)
