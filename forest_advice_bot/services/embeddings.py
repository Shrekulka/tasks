# forest-advice-bot/services/embeddings.py

"""Эмбеддинги: интерфейс и локальная реализация на sentence-transformers.

Тяжёлые вычисления идут вне цикла событий (asyncio.to_thread), один за другим: параллельные
запуски модели на CPU только мешают друг другу. Библиотека sentence-transformers (и torch)
импортируется лениво, поэтому тесты и скрипты без поиска её не требуют.
"""

import asyncio
import logging
from collections.abc import Sequence
from typing import Any, Protocol

from config_data.config import RagConfig

log = logging.getLogger(__name__)


class EmbeddingDimMismatch(RuntimeError):
    """Размерность модели не совпадает с EMBEDDING_DIM из конфигурации."""


class Embedder(Protocol):
    model_name: str

    @property
    def dim(self) -> int: ...

    async def embed_passages(self, texts: Sequence[str]) -> list[list[float]]:
        """Векторы фрагментов документов."""

    async def embed_query(self, text: str) -> list[float]:
        """Вектор вопроса."""


def with_prefix(prefix: str, text: str) -> str:
    """Префикс e5 («query:», «passage:») и пробел после него. Пустой префикс ничего не меняет."""
    return f"{prefix} {text}" if prefix else text


class SentenceTransformerEmbedder:
    """Векторы нормализованы, поэтому косинусное расстояние в БД корректно."""

    def __init__(
        self,
        model: Any,
        *,
        model_name: str,
        query_prefix: str,
        passage_prefix: str,
        batch_size: int,
    ) -> None:
        self._model = model
        self.model_name = model_name
        self._query_prefix = query_prefix
        self._passage_prefix = passage_prefix
        self._batch_size = batch_size
        # В sentence-transformers 6 метод переименован; старое имя оставлено для совместимости.
        get_dimension = getattr(model, "get_embedding_dimension", None) or (
            model.get_sentence_embedding_dimension
        )
        self._dim = int(get_dimension())
        self._lock = asyncio.Lock()
        self.truncated_total = 0  # сколько текстов не поместилось в окно модели (для диагностики)

    @property
    def dim(self) -> int:
        return self._dim

    @classmethod
    def load(cls, rag: RagConfig) -> "SentenceTransformerEmbedder":
        """Загружает модель. Блокирующая операция: вызывайте через asyncio.to_thread."""
        from sentence_transformers import SentenceTransformer  # ленивый импорт: тяжёлая библиотека

        log.info("Загрузка модели эмбеддингов %s", rag.embedding_model)
        embedder = cls(
            SentenceTransformer(rag.embedding_model),
            model_name=rag.embedding_model,
            query_prefix=rag.embedding_query_prefix,
            passage_prefix=rag.embedding_passage_prefix,
            batch_size=rag.embedding_batch_size,
        )
        if embedder.dim != rag.embedding_dim:
            raise EmbeddingDimMismatch(
                f"Модель {rag.embedding_model} выдаёт векторы размерности {embedder.dim}, "
                f"а EMBEDDING_DIM={rag.embedding_dim}. Исправьте EMBEDDING_DIM в .env."
            )
        return embedder

    async def embed_passages(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        prepared = [with_prefix(self._passage_prefix, text) for text in texts]
        # Пачками, а не одним вызовом: lock отпускается между пачками, и вопрос зверя
        # (embed_query) встаёт в очередь сразу за текущей пачкой, а не за всем документом.
        step = self._batch_size
        vectors: list[list[float]] = []
        for start in range(0, len(prepared), step):
            vectors.extend(await self._encode(prepared[start : start + step]))
        return vectors

    async def embed_query(self, text: str) -> list[float]:
        (vector,) = await self._encode([with_prefix(self._query_prefix, text)])
        return vector

    async def _encode(self, texts: list[str]) -> list[list[float]]:
        async with self._lock:
            return await asyncio.to_thread(self._encode_sync, texts)

    def _count_truncated(self, texts: list[str]) -> int:
        """Сколько текстов длиннее окна модели (у e5 это 512 токенов с префиксом).

        Модель молча обрезает хвост, и поиск по нему перестаёт работать. Проверка идёт только
        если у модели есть токенайзер и max_seq_length, иначе возвращает 0.
        """
        tokenizer = getattr(self._model, "tokenizer", None)
        limit = getattr(self._model, "max_seq_length", None)
        if tokenizer is None or not limit:
            return 0
        ids = tokenizer(texts, add_special_tokens=True, truncation=False)["input_ids"]
        return sum(1 for item in ids if len(item) > limit)

    def _encode_sync(self, texts: list[str]) -> list[list[float]]:
        truncated = self._count_truncated(texts)
        if truncated:
            self.truncated_total += truncated
            log.warning(
                "Эмбеддинги: %d из %d текстов длиннее окна модели и будут обрезаны. "
                "Уменьшите CHUNK_SIZE.",
                truncated,
                len(texts),
            )
        vectors = self._model.encode(
            texts,
            batch_size=self._batch_size,
            normalize_embeddings=True,
            show_progress_bar=False,
        )
        return [[float(x) for x in vector] for vector in vectors]
