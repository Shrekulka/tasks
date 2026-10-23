"""Интеграционные тесты репозитория на настоящем PostgreSQL с pgvector."""

import os

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from database.database import (
    EmbeddingSchemaMismatch,
    create_engine,
    create_session_factory,
    ensure_embedding_schema,
    ensure_vector_extension,
    init_db,
)
from models.methods import (
    delete_archive_documents,
    delete_user_document,
    load_sync_state,
    promote_to_archive,
    register_document,
)
from models.models import Chunk, DocStatus, Document, SourceType

pytestmark = pytest.mark.integration


@pytest.fixture
async def session_factory():
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("Переменная TEST_DATABASE_URL не задана")
    await ensure_vector_extension(url)
    engine = create_engine(url)
    await init_db(engine)
    async with engine.begin() as connection:
        await connection.execute(
            text("TRUNCATE chunks, documents, app_meta RESTART IDENTITY CASCADE")
        )
    try:
        yield create_session_factory(engine)
    finally:
        await engine.dispose()


def _doc(sha="a" * 64, kind=SourceType.USER, owner=1, path=None, status=DocStatus.PROCESSING):
    return Document(
        sha256=sha,
        title="Doc",
        filename="doc.txt",
        source_type=kind,
        source_path=path,
        uploader_tg_id=owner if kind is SourceType.USER else None,
        uploader_name="Їжак" if kind is SourceType.USER else None,
        size_bytes=10,
        format="txt",
        status=status,
    )


async def test_second_upload_of_same_hash_is_duplicate(session_factory):
    async with session_factory() as session:
        first, created_first = await register_document(session, _doc())
        await session.commit()
    async with session_factory() as session:
        second, created_second = await register_document(session, _doc(owner=2))
        await session.commit()

    assert created_first is True
    assert created_second is False
    assert second.id == first.id
    assert second.uploader_tg_id == 1


async def test_register_document_does_not_hide_other_integrity_errors(
    session_factory,
    monkeypatch,
):
    """Ошибка другого ограничения не должна маскироваться под дубликат SHA-256."""
    async with session_factory() as session:
        winner, created = await register_document(session, _doc())
        assert created is True
        await session.commit()

    async with session_factory() as session:
        original_scalar = session.scalar
        scalar_calls = 0

        async def simulate_race(statement, *args, **kwargs):
            nonlocal scalar_calls
            scalar_calls += 1

            # Имитируем ситуацию, когда первоначальная проверка не увидела
            # запись, созданную конкурентной транзакцией.
            if scalar_calls == 1:
                return None

            return await original_scalar(statement, *args, **kwargs)

        monkeypatch.setattr(session, "scalar", simulate_race)

        invalid_doc = _doc()
        invalid_doc.title = None

        with pytest.raises(IntegrityError):
            await register_document(session, invalid_doc)


async def test_failed_user_record_is_reused_by_user(session_factory):
    async with session_factory() as session:
        await register_document(session, _doc(status=DocStatus.FAILED))
        await session.commit()
    async with session_factory() as session:
        doc, created = await register_document(session, _doc(owner=2))
        await session.commit()

    assert created is True
    assert doc.status is DocStatus.PROCESSING
    assert doc.uploader_tg_id == 2


async def test_user_cannot_take_over_failed_archive_record(session_factory):
    async with session_factory() as session:
        await register_document(
            session, _doc(kind=SourceType.ARCHIVE, path="a.txt", status=DocStatus.FAILED)
        )
        await session.commit()
    async with session_factory() as session:
        doc, created = await register_document(session, _doc(owner=7))
        await session.commit()

    assert created is False
    assert doc.source_type is SourceType.ARCHIVE
    assert doc.uploader_tg_id is None


async def test_archive_can_take_over_failed_user_record(session_factory):
    async with session_factory() as session:
        await register_document(session, _doc(status=DocStatus.FAILED))
        await session.commit()
    async with session_factory() as session:
        doc, created = await register_document(session, _doc(kind=SourceType.ARCHIVE, path="a.txt"))
        await session.commit()

    assert created is True
    assert doc.source_type is SourceType.ARCHIVE
    assert doc.uploader_tg_id is None


async def test_promoted_doc_cannot_be_deleted_by_former_owner(session_factory):
    async with session_factory() as session:
        doc, _ = await register_document(session, _doc(owner=5))
        await session.commit()
        doc_id = doc.id
    async with session_factory() as session:
        assert await promote_to_archive(session, "a" * 64, "tips.txt") is True
        await session.commit()
    async with session_factory() as session:
        assert await delete_user_document(session, doc_id, owner_tg_id=5) is False
        await session.commit()
        saved = await session.get(Document, doc_id)

    assert saved is not None
    assert saved.source_type is SourceType.ARCHIVE
    assert saved.uploader_tg_id is None


async def test_user_cannot_delete_foreign_document(session_factory):
    async with session_factory() as session:
        doc, _ = await register_document(session, _doc(owner=1))
        await session.commit()
        doc_id = doc.id
    async with session_factory() as session:
        assert await delete_user_document(session, doc_id, owner_tg_id=2) is False
        assert await delete_user_document(session, doc_id, owner_tg_id=1) is True
        await session.commit()


async def test_delete_cascades_to_chunks(session_factory):
    async with session_factory() as session:
        doc, _ = await register_document(session, _doc())
        session.add(
            Chunk(document_id=doc.id, chunk_index=0, content="x", embedding=[0.1, 0.2, 0.3])
        )
        await session.commit()
        doc_id = doc.id
    async with session_factory() as session:
        await delete_user_document(session, doc_id, owner_tg_id=1)
        await session.commit()
        left = await session.scalar(select(func.count()).select_from(Chunk))

    assert left == 0


async def test_sync_state_skips_failed_and_separates_sources(session_factory):
    async with session_factory() as session:
        await register_document(session, _doc("1" * 64, SourceType.ARCHIVE, path="a.txt"))
        await register_document(session, _doc("2" * 64, SourceType.USER))
        await register_document(session, _doc("3" * 64, SourceType.USER, status=DocStatus.FAILED))
        await session.commit()
        archive_known, user_known = await load_sync_state(session)

    assert archive_known == {"1" * 64: "a.txt"}
    assert user_known == frozenset({"2" * 64})


async def test_delete_archive_documents_leaves_user_docs(session_factory):
    async with session_factory() as session:
        await register_document(session, _doc("1" * 64, SourceType.ARCHIVE, path="a.txt"))
        await register_document(session, _doc("2" * 64, SourceType.USER))
        await session.commit()
        removed = await delete_archive_documents(session, frozenset({"1" * 64, "2" * 64}))
        await session.commit()

    assert removed == 1


async def test_embedding_schema_is_recorded_then_checked(session_factory):
    async with session_factory() as session:
        await ensure_embedding_schema(session, "model-a", 3)
    async with session_factory() as session:
        await ensure_embedding_schema(session, "model-a", 3)  # то же самое: без ошибки
    async with session_factory() as session:
        with pytest.raises(EmbeddingSchemaMismatch):
            await ensure_embedding_schema(session, "model-b", 3)
    async with session_factory() as session:
        with pytest.raises(EmbeddingSchemaMismatch):
            await ensure_embedding_schema(session, "model-a", 4)
