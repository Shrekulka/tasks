"""Детерминированный эмбеддер для тестов: мешок слов, свёрнутый хэшем, затем нормализация.

Тексты с общими словами получают близкие векторы, поэтому поиск и порог в тестах ведут себя
правдоподобно, а тяжёлую модель загружать не нужно.
"""

import hashlib
import math
import re
from collections.abc import Sequence

_WORD_RE = re.compile(r"\w+", re.UNICODE)


class HashEmbedder:
    def __init__(self, dim: int = 64, model_name: str = "hash-embedder") -> None:
        self._dim = dim
        self.model_name = model_name
        self.passage_calls: list[list[str]] = []
        self.query_calls: list[str] = []
        self.fail_passages = False

    @property
    def dim(self) -> int:
        return self._dim

    def vector(self, text: str) -> list[float]:
        values = [0.0] * self._dim
        for word in _WORD_RE.findall(text.lower()):
            digest = hashlib.sha256(word.encode()).digest()
            values[int.from_bytes(digest[:4], "big") % self._dim] += 1.0
        norm = math.sqrt(sum(v * v for v in values)) or 1.0
        return [v / norm for v in values]

    async def embed_passages(self, texts: Sequence[str]) -> list[list[float]]:
        self.passage_calls.append(list(texts))
        if self.fail_passages:
            raise RuntimeError("embedder is broken")
        return [self.vector(t) for t in texts]

    async def embed_query(self, text: str) -> list[float]:
        self.query_calls.append(text)
        return self.vector(text)
