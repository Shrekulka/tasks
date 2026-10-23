# forest-advice-bot/services/retrieval.py

"""Поиск фрагментов: вектор вопроса, ближайшие фрагменты, порог релевантности, разнообразие."""

import logging
from collections.abc import Sequence
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from config_data.config import RagConfig
from models.methods import FoundChunk, search_chunks
from services.embeddings import Embedder

log = logging.getLogger(__name__)


class SupportsRetrieve(Protocol):
    async def retrieve(self, question: str) -> list[FoundChunk]: ...


def filter_relevant(
    found: Sequence[FoundChunk], *, threshold: float, margin: float = 0.0
) -> list[FoundChunk]:
    """Оставляет фрагменты не дальше порога и (если margin > 0) не дальше лучшего + margin.

    found отсортирован по расстоянию. Абсолютный порог отвечает на вопрос «есть ли вообще
    что-то подходящее», margin убирает «шлейф» слабо связанных фрагментов за лучшим.
    """
    if not found:
        return []
    limit = threshold
    if margin > 0:
        limit = min(threshold, found[0].distance + margin)
    return [chunk for chunk in found if chunk.distance <= limit]


def select_diverse(relevant: Sequence[FoundChunk], top_k: int) -> list[FoundChunk]:
    """Сначала лучший фрагмент каждого документа, затем добор лучшими из оставшихся.

    Так в контекст попадают разные документы: без этого два противоречащих совета
    часто теряются, потому что один документ занимает все top_k мест. Гарантии, что бот
    найдёт любое противоречие, это не даёт. Результат отсортирован по расстоянию.
    """
    chosen: list[FoundChunk] = []
    seen_documents: set[int] = set()
    for chunk in relevant:
        if len(chosen) >= top_k:
            break
        if chunk.document_id not in seen_documents:
            seen_documents.add(chunk.document_id)
            chosen.append(chunk)

    chosen_ids = {chunk.chunk_id for chunk in chosen}
    for chunk in relevant:
        if len(chosen) >= top_k:
            break
        if chunk.chunk_id not in chosen_ids:
            chosen.append(chunk)

    return sorted(chosen, key=lambda chunk: (chunk.distance, chunk.chunk_id))


class Retriever:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        embedder: Embedder,
        rag: RagConfig,
    ) -> None:
        self._session_factory = session_factory
        self._embedder = embedder
        self._rag = rag

    async def retrieve(self, question: str) -> list[FoundChunk]:
        """Пусто, если ни один фрагмент не ближе порога: тогда LLM вызывать не нужно."""
        vector = await self._embedder.embed_query(question)
        async with self._session_factory() as session:
            found = await search_chunks(
                session, vector, limit=self._rag.top_k * self._rag.candidate_factor
            )
        relevant = filter_relevant(
            found,
            threshold=self._rag.relevance_threshold,
            margin=self._rag.relevance_margin,
        )
        selected = select_diverse(relevant, self._rag.top_k)
        # Текст вопроса в лог не пишем. Расстояние до лучшего фрагмента показывает, насколько
        # близко к порогу решился вопрос: без него «не знайшов» не отличить от плохого порога.
        best = f"{found[0].distance:.3f}" if found else "нет"
        log.info(
            "Поиск: лучшее расстояние=%s, порог=%.3f, margin=%.3f, фрагментов=%d (документов %d)",
            best,
            self._rag.relevance_threshold,
            self._rag.relevance_margin,
            len(selected),
            len({chunk.document_id for chunk in selected}),
        )
        return selected
