# forest-advice-bot/models/methods.py

"""Запросы к БД. Хендлеры и сервисы не пишут SQL сами, а вызывают эти функции."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from models.models import Chunk, DocStatus, Document, SourceType

# Поля, которые обновляются, когда запись failed берётся в работу повторно.
_REUSABLE = (
    "title",
    "filename",
    "source_type",
    "source_path",
    "uploader_tg_id",
    "uploader_name",
    "uploader_username",
    "size_bytes",
    "format",
)

_SHA256_UNIQUE_CONSTRAINT = "documents_sha256_key"
_POSTGRES_UNIQUE_VIOLATION = "23505"


def _is_sha256_unique_violation(error: IntegrityError) -> bool:
    """Определяет, вызван ли IntegrityError конфликтом уникальности SHA-256."""
    pending: list[BaseException] = [error.orig]
    visited: set[int] = set()

    while pending:
        current = pending.pop()

        if id(current) in visited:
            continue
        visited.add(id(current))

        sqlstate = getattr(current, "sqlstate", None) or getattr(current, "pgcode", None)
        diagnostic = getattr(current, "diag", None)
        constraint_name = getattr(current, "constraint_name", None) or getattr(
            diagnostic, "constraint_name", None
        )

        if sqlstate == _POSTGRES_UNIQUE_VIOLATION and constraint_name == _SHA256_UNIQUE_CONSTRAINT:
            return True

        for attribute in ("__cause__", "__context__"):
            chained = getattr(current, attribute, None)
            if isinstance(chained, BaseException):
                pending.append(chained)

    return False


def _may_reuse(existing: Document, incoming: Document) -> bool:
    """Правило D34: failed-запись можно взять в работу повторно.

    Звери перехватывать запись архива не могут. Архив может забрать failed-запись зверя.
    """
    if existing.status is not DocStatus.FAILED:
        return False
    return (
        existing.source_type is incoming.source_type or incoming.source_type is SourceType.ARCHIVE
    )


async def register_document(session: AsyncSession, doc: Document) -> tuple[Document, bool]:
    """Возвращает (документ, создан_или_взят_в_работу). False означает дубль или отказ.

    Гонку двух одновременных загрузок закрывает уникальный индекс sha256:
    проигравший при конфликте уникальности читает победителя и получает «дубль».
    Остальные ошибки целостности передаются вызывающему коду.
    """
    existing = await session.scalar(
        select(Document).where(Document.sha256 == doc.sha256).with_for_update()
    )
    if existing is not None:
        if not _may_reuse(existing, doc):
            return existing, False

        await session.execute(delete(Chunk).where(Chunk.document_id == existing.id))
        for name in _REUSABLE:
            setattr(existing, name, getattr(doc, name))
        existing.uploaded_at = func.now()
        existing.chunk_count = 0
        existing.status = DocStatus.PROCESSING
        await session.flush()
        return existing, True

    try:
        async with session.begin_nested():
            session.add(doc)
            await session.flush()
    except IntegrityError as exc:
        if not _is_sha256_unique_violation(exc):
            raise

        winner = await session.scalar(select(Document).where(Document.sha256 == doc.sha256))
        if winner is None:
            raise

        return winner, False

    return doc, True


async def promote_to_archive(session: AsyncSession, sha256: str, source_path: str) -> bool:
    """D33: документ зверя становится стандартным. Фрагменты и векторы остаются как есть."""
    result = await session.execute(
        update(Document)
        .where(Document.sha256 == sha256, Document.source_type == SourceType.USER)
        .values(
            source_type=SourceType.ARCHIVE,
            source_path=source_path,
            uploader_tg_id=None,
            uploader_name=None,
            uploader_username=None,
        )
    )
    return result.rowcount > 0


async def relink_archive_document(session: AsyncSession, sha256: str, source_path: str) -> bool:
    """Архивный документ сменил путь (переименование). Переиндексация не нужна."""
    result = await session.execute(
        update(Document)
        .where(Document.sha256 == sha256, Document.source_type == SourceType.ARCHIVE)
        .values(source_path=source_path)
    )
    return result.rowcount > 0


async def delete_archive_documents(session: AsyncSession, sha256s: frozenset[str]) -> int:
    """Удаляет архивные документы по хэшу. Документы зверей затронуть невозможно."""
    if not sha256s:
        return 0
    result = await session.execute(
        delete(Document).where(
            Document.sha256.in_(sha256s),
            Document.source_type == SourceType.ARCHIVE,
        )
    )
    return result.rowcount


async def delete_user_document(session: AsyncSession, doc_id: int, owner_tg_id: int) -> bool:
    """Удаляет только документ зверя и только его владельцу. Проверка прямо в запросе."""
    result = await session.execute(
        delete(Document).where(
            Document.id == doc_id,
            Document.source_type == SourceType.USER,
            Document.uploader_tg_id == owner_tg_id,
        )
    )
    return result.rowcount > 0


async def load_sync_state(session: AsyncSession) -> tuple[dict[str, str], frozenset[str]]:
    """Вход для plan_archive_sync: (archive_known, user_known).

    Записи failed не учитываются: они попадут в план как новые и будут проиндексированы заново.
    """
    rows = (
        await session.execute(
            select(Document.sha256, Document.source_type, Document.source_path).where(
                Document.status != DocStatus.FAILED
            )
        )
    ).all()
    archive_known = {
        sha: path for sha, kind, path in rows if kind is SourceType.ARCHIVE and path is not None
    }
    user_known = frozenset(sha for sha, kind, _ in rows if kind is SourceType.USER)
    return archive_known, user_known


@dataclass(frozen=True, slots=True)
class NewChunk:
    chunk_index: int
    content: str
    page: int | None
    section: str | None
    embedding: Sequence[float]


@dataclass(frozen=True, slots=True)
class FoundChunk:
    """Найденный фрагмент вместе со сведениями о документе (для источников в ответе)."""

    chunk_id: int
    document_id: int
    content: str
    page: int | None
    section: str | None
    distance: float
    title: str
    source_type: SourceType
    uploader_name: str | None
    uploader_username: str | None
    uploaded_at: datetime


async def find_by_sha(session: AsyncSession, sha256: str) -> Document | None:
    return await session.scalar(select(Document).where(Document.sha256 == sha256))


async def add_chunks(
    session: AsyncSession,
    document_id: int,
    chunks: Sequence[NewChunk],
    *,
    expected_dim: int,
) -> None:
    """Размерность каждого вектора проверяется до записи: векторы разных размеров не смешиваются."""
    for chunk in chunks:
        if len(chunk.embedding) != expected_dim:
            raise ValueError(f"Вектор размерности {len(chunk.embedding)}, ожидалось {expected_dim}")
    session.add_all(
        Chunk(
            document_id=document_id,
            chunk_index=chunk.chunk_index,
            content=chunk.content,
            page=chunk.page,
            section=chunk.section,
            embedding=list(chunk.embedding),
        )
        for chunk in chunks
    )
    await session.flush()


async def finish_document(session: AsyncSession, document_id: int, chunk_count: int) -> None:
    await session.execute(
        update(Document)
        .where(Document.id == document_id)
        .values(status=DocStatus.READY, chunk_count=chunk_count)
    )


async def fail_document(session: AsyncSession, document_id: int) -> None:
    """Помечает failed только документ, который ещё в обработке: готовый не трогаем."""
    await session.execute(
        update(Document)
        .where(Document.id == document_id, Document.status == DocStatus.PROCESSING)
        .values(status=DocStatus.FAILED)
    )


async def mark_processing_as_failed(session: AsyncSession) -> int:
    """При старте: документы, застрявшие в processing после падения, становятся failed."""
    result = await session.execute(
        update(Document)
        .where(Document.status == DocStatus.PROCESSING)
        .values(status=DocStatus.FAILED)
    )
    return result.rowcount


async def search_chunks(
    session: AsyncSession, query_vector: Sequence[float], limit: int
) -> list[FoundChunk]:
    """Ближайшие фрагменты по косинусному расстоянию, только из документов в статусе ready."""
    distance = Chunk.embedding.cosine_distance(list(query_vector)).label("distance")
    rows = await session.execute(
        select(
            Chunk.id,
            Chunk.document_id,
            Chunk.content,
            Chunk.page,
            Chunk.section,
            distance,
            Document.title,
            Document.source_type,
            Document.uploader_name,
            Document.uploader_username,
            Document.uploaded_at,
        )
        .join(Document, Document.id == Chunk.document_id)
        .where(Document.status == DocStatus.READY)
        .order_by(distance, Chunk.id)
        .limit(limit)
    )
    return [FoundChunk(*row) for row in rows.all()]


async def list_user_documents(
    session: AsyncSession, owner_tg_id: int, *, offset: int, limit: int
) -> list[Document]:
    """Документы зверя, новые первыми. Документы архива сюда не попадают."""
    rows = await session.scalars(
        select(Document)
        .where(Document.source_type == SourceType.USER, Document.uploader_tg_id == owner_tg_id)
        .order_by(Document.uploaded_at.desc(), Document.id.desc())
        .offset(offset)
        .limit(limit)
    )
    return list(rows.all())


async def count_user_documents(session: AsyncSession, owner_tg_id: int) -> int:
    total = await session.scalar(
        select(func.count())
        .select_from(Document)
        .where(Document.source_type == SourceType.USER, Document.uploader_tg_id == owner_tg_id)
    )
    return int(total or 0)


async def get_user_document(
    session: AsyncSession, doc_id: int, owner_tg_id: int
) -> Document | None:
    """Документ по id, только если он принадлежит зверю (проверка прямо в запросе)."""
    return await session.scalar(
        select(Document).where(
            Document.id == doc_id,
            Document.source_type == SourceType.USER,
            Document.uploader_tg_id == owner_tg_id,
        )
    )
