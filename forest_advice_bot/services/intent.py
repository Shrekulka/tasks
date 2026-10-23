# forest-advice-bot/services/intent.py

"""Распознавание реплик, на которые не нужны ни поиск, ни LLM (приветствие, благодарность).

Фразы приходят снаружи (из lexicon), код не знает языка. Сравнение точное, по всему
сообщению: «привіт» распознаётся, а «привіт, де ховати горіхи?» нет и идёт как вопрос.
"""

import re
from collections.abc import Iterable

_NON_WORD_RE = re.compile(r"[^\w\s]|_", re.UNICODE)


def normalize_phrase(text: str) -> str:
    return " ".join(_NON_WORD_RE.sub(" ", text.casefold()).split())


class SmallTalkDetector:
    def __init__(self, phrases: Iterable[str]) -> None:
        self._phrases = frozenset(normalize_phrase(p) for p in phrases if p.strip())

    def matches(self, text: str) -> bool:
        return normalize_phrase(text) in self._phrases
