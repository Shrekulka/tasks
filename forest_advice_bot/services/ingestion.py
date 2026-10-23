# forest-advice-bot/services/ingestion.py

"""Приём документа: проверки, дубль, разбор, запись в реестр, затем индексация.

Два этапа, как в ТЗ: submit() быстро проверяет файл и ставит документ в статус processing
(после него бот отвечает «Прийняв документ…»), а index() считает эмбеддинги и открывает
документ для поиска. Для архива используется ingest(), который выполняет оба этапа подряд.
"""

import asyncio
import hashlib
import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import PurePath

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from config_data.config import RagConfig
from errors.errors import (
    DuplicateDocument,
    EmptyDocument,
    FileTooLarge,
    UnsupportedFormat,
)
from models.methods import (
    NewChunk,
    add_chunks,
    fail_document,
    find_by_sha,
    finish_document,
    register_document,
)
from models.models import DocStatus, Document, SourceType
from services.chunker import chunk_segments
from services.embeddings import Embedder
from services.parsers import ParseLimits, Segment, detect_format, supported_extensions

log = logging.getLogger(__name__)

_MB = 1024 * 1024


@dataclass(frozen=True, slots=True)
class DocumentSource:
    """Откуда документ: архив или зверь. Для архива задаётся путь, для зверя автор."""

    source_type: SourceType
    filename: str
    source_path: str | None = None
    uploader_tg_id: int | None = None
    uploader_name: str | None = None
    uploader_username: str | None = None


@dataclass(frozen=True, slots=True)
class Submission:
    """Результат первого этапа: документ принят и ждёт индексации."""

    document_id: int
    title: str
    segments: list[Segment]


def title_from(filename: str, sha256: str) -> str:
    """D13: название документа это имя файла без расширения."""
    stem = PurePath(filename).stem.strip()
    return stem or f"document_{sha256[:8]}"


def _duplicate(existing: Document) -> DuplicateDocument:
    return DuplicateDocument(
        title=existing.title,
        is_archive=existing.source_type is SourceType.ARCHIVE,
        uploader_name=existing.uploader_name,
        uploader_username=existing.uploader_username,
        uploaded_at=existing.uploaded_at,
    )


class IngestionService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        embedder: Embedder,
        rag: RagConfig,
        *,
        on_change: Callable[[], None] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._embedder = embedder
        self._rag = rag
        self._on_change = on_change  # вызывается, когда коллекция изменилась (сброс кэша)
        self._limits = ParseLimits.from_config(rag)
        self._max_bytes = rag.max_file_mb * _MB

    # ------------------------------------------------------------------ до скачивания

    def validate_upload(self, filename: str, size_bytes: int | None) -> None:
        """Дешёвая проверка по имени и размеру из метаданных Telegram, до скачивания файла."""
        extension = PurePath(filename).suffix.lower()
        if extension not in supported_extensions():
            raise UnsupportedFormat("extension", extension or "без расширения")
        if size_bytes is not None and size_bytes > self._max_bytes:
            raise FileTooLarge(size_bytes, self._max_bytes)

    # ------------------------------------------------------------------ этап 1

    async def submit(self, data: bytes, source: DocumentSource) -> Submission:
        """Проверяет файл, ищет дубль, разбирает текст и регистрирует документ (processing).

        Бросает FileTooLarge, UnsupportedFormat, DuplicateDocument, EmptyDocument,
        UnreadableDocument.
        """
        if len(data) > self._max_bytes:
            raise FileTooLarge(len(data), self._max_bytes)
        spec = detect_format(source.filename, data)
        sha256 = hashlib.sha256(data).hexdigest()

        async with self._session_factory() as session:
            existing = await find_by_sha(session, sha256)
        if existing is not None and existing.status is not DocStatus.FAILED:
            raise _duplicate(existing)

        segments = await asyncio.to_thread(spec.parse, data, self._limits)

        document = Document(
            sha256=sha256,
            title=title_from(source.filename, sha256),
            filename=source.filename,
            source_type=source.source_type,
            source_path=source.source_path,
            uploader_tg_id=source.uploader_tg_id,
            uploader_name=source.uploader_name,
            uploader_username=source.uploader_username,
            size_bytes=len(data),
            format=spec.name,
        )
        async with self._session_factory() as session:
            saved, created = await register_document(session, document)
            await session.commit()
        if not created:
            raise _duplicate(saved)
        return Submission(document_id=saved.id, title=saved.title, segments=segments)

    # ------------------------------------------------------------------ этап 2

    async def index(self, submission: Submission) -> bool:
        """Фрагменты, эмбеддинги, запись, статус ready. True, если документ готов к поиску.

        При сбое документ получает статус failed, исключение передаётся вызывающему коду.
        Если документ удалили во время индексации, возвращается False без ошибки.
        """
        try:
            chunks = chunk_segments(
                submission.segments,
                size=self._rag.chunk_size,
                overlap=self._rag.chunk_overlap,
            )
            if not chunks:
                raise EmptyDocument("после разбиения не осталось фрагментов")
            vectors = await self._embedder.embed_passages([c.text for c in chunks])
            rows = [
                NewChunk(c.index, c.text, c.page, c.section, vector)
                for c, vector in zip(chunks, vectors, strict=True)
            ]
            async with self._session_factory() as session:
                document = await session.get(
                    Document,
                    submission.document_id,
                    with_for_update=True,
                )

                if document is None:
                    log.info(
                        "Документ %d удалён до сохранения фрагментов",
                        submission.document_id,
                    )
                    return False

                await add_chunks(
                    session,
                    submission.document_id,
                    rows,
                    expected_dim=self._embedder.dim,
                )
                await finish_document(
                    session,
                    submission.document_id,
                    len(rows),
                )
                await session.commit()
            if self._on_change is not None:
                self._on_change()
        except IntegrityError:
            async with self._session_factory() as session:
                document_exists = await session.get(Document, submission.document_id) is not None
            if not document_exists:
                log.info(
                    "Документ %d удалён во время индексации",
                    submission.document_id,
                )
                return False
            log.exception(
                "Нарушение целостности БД при индексации документа %d",
                submission.document_id,
            )
            async with self._session_factory() as session:
                await fail_document(session, submission.document_id)
                await session.commit()
            raise

        except Exception:
            log.exception(
                "Индексация документа %d не удалась",
                submission.document_id,
            )
            async with self._session_factory() as session:
                await fail_document(session, submission.document_id)
                await session.commit()
            raise
        return True

    async def ingest(self, data: bytes, source: DocumentSource) -> Submission:
        """Оба этапа подряд (для архива)."""
        submission = await self.submit(data, source)
        await self.index(submission)
        return submission
