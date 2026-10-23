"""Приём документов и синхронизация архива на настоящем PostgreSQL."""

import asyncio
import os
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from config_data.config import RagConfig
from database.database import (
    create_engine,
    create_session_factory,
    ensure_vector_extension,
    init_db,
)
from errors.errors import (
    DuplicateDocument,
    EmptyDocument,
    FileTooLarge,
    UnreadableDocument,
    UnsupportedFormat,
)
from models.methods import (
    NewChunk,
    add_chunks,
    delete_user_document,
    mark_processing_as_failed,
    search_chunks,
)
from models.models import Chunk, DocStatus, Document, SourceType
from services.archive_sync import scan_archive, sync_archive
from services.ingestion import DocumentSource, IngestionService
from tests.fake_embedder import HashEmbedder
from tests.pdf_factory import blank_pdf, docx_bytes

pytestmark = pytest.mark.integration


def _rag(**overrides) -> RagConfig:
    values = {
        "embedding_model": "hash",
        "embedding_dim": 64,
        "embedding_query_prefix": "",
        "embedding_passage_prefix": "",
        "embedding_batch_size": 16,
        "top_k": 5,
        "relevance_threshold": 0.9,
        "relevance_margin": 0.0,
        "chunk_size": 200,
        "chunk_overlap": 40,
        "archive_dir": Path("archive"),
        "max_file_mb": 1,
        "max_pdf_pages": 50,
        "max_zip_entries": 500,
        "max_unpacked_mb": 50,
        "candidate_factor": 4,
        "max_context_chars": 6000,
        "processing_notice_after_seconds": 5.0,
        "large_document_chars": 20000,
        "answer_language": "uk",
        "language_profiles_dir": Path("data/languages"),
        "prompts_dir": Path("data/prompts"),
        "language_marker_min_chars": 40,
        "cache_ttl_seconds": 3600.0,
        "cache_max_entries": 500,
    }
    values.update(overrides)
    return RagConfig(**values)


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


@pytest.fixture
def embedder():
    return HashEmbedder()


@pytest.fixture
def ingestion(session_factory, embedder):
    return IngestionService(session_factory, embedder, _rag())


def _user(filename="tips.txt", owner=1, name="Їжак", username="hedgehog"):
    return DocumentSource(
        source_type=SourceType.USER,
        filename=filename,
        uploader_tg_id=owner,
        uploader_name=name,
        uploader_username=username,
    )


async def _documents(session_factory) -> list[Document]:
    async with session_factory() as session:
        return list((await session.scalars(select(Document).order_by(Document.id))).all())


# ---------------------------------------------------------------- приём документа


async def test_ingest_makes_document_searchable(session_factory, ingestion, embedder):
    text_ = "Їжак ховає горіхи у кількох місцях. Білка перевіряє запаси щотижня."

    await ingestion.ingest(text_.encode(), _user())

    (doc,) = await _documents(session_factory)
    assert (doc.title, doc.format, doc.status) == ("tips", "txt", DocStatus.READY)
    assert doc.chunk_count >= 1
    async with session_factory() as session:
        found = await search_chunks(session, await embedder.embed_query("горіхи їжак"), limit=3)
    assert found
    assert found[0].title == "tips"
    assert found[0].uploader_username == "hedgehog"
    assert found[0].distance < 0.9


async def test_submit_registers_processing_before_index(session_factory, ingestion):
    submission = await ingestion.submit("Порада про зиму.".encode(), _user())

    (doc,) = await _documents(session_factory)
    assert doc.status is DocStatus.PROCESSING
    assert submission.document_id == doc.id

    await ingestion.index(submission)

    (doc,) = await _documents(session_factory)
    assert doc.status is DocStatus.READY


async def test_processing_documents_are_not_searchable(session_factory, ingestion, embedder):
    await ingestion.submit("Порада про зиму.".encode(), _user())

    async with session_factory() as session:
        found = await search_chunks(session, await embedder.embed_query("зиму"), limit=5)

    assert found == []


async def test_duplicate_reports_who_and_when(ingestion):
    data = "Однакова порада.".encode()
    await ingestion.ingest(data, _user(owner=1, name="Їжак", username="hedgehog"))

    with pytest.raises(DuplicateDocument) as caught:
        await ingestion.submit(data, _user("other-name.txt", owner=2))

    assert caught.value.title == "tips"
    assert caught.value.is_archive is False
    assert caught.value.uploader_name == "Їжак"
    assert caught.value.uploader_username == "hedgehog"
    assert caught.value.uploaded_at is not None


async def test_two_simultaneous_uploads_of_one_file_leave_one_document(session_factory, ingestion):
    """T18: двое присылают один файл одновременно: в базе один документ, второй получает дубль."""
    import asyncio

    data = "Одночасна порада про воду.".encode()

    results = await asyncio.gather(
        ingestion.submit(data, _user(owner=1, name="Перший", username="first")),
        ingestion.submit(data, _user(owner=2, name="Другий", username="second")),
        ingestion.submit(data, _user(owner=3, name="Третій", username="third")),
        return_exceptions=True,
    )

    accepted = [r for r in results if not isinstance(r, BaseException)]
    duplicates = [r for r in results if isinstance(r, DuplicateDocument)]
    assert len(accepted) == 1 and len(duplicates) == 2, results
    stored = await _documents(session_factory)
    assert len(stored) == 1
    assert accepted[0].document_id == stored[0].id
    # проигравшие называют автора того, кто успел первым
    assert {d.uploader_name for d in duplicates} == {stored[0].uploader_name}


async def test_duplicate_of_archive_document_is_marked_as_archive(ingestion):
    data = "Порада архіву.".encode()
    await ingestion.ingest(data, DocumentSource(SourceType.ARCHIVE, "a.txt", source_path="a.txt"))

    with pytest.raises(DuplicateDocument) as caught:
        await ingestion.submit(data, _user())

    assert caught.value.is_archive is True


@pytest.mark.parametrize(
    ("filename", "data", "error"),
    [
        ("a.exe", b"x", UnsupportedFormat),
        ("renamed.txt", blank_pdf(), UnsupportedFormat),
        ("empty.txt", b"  \n", EmptyDocument),
        ("scan.pdf", blank_pdf(), EmptyDocument),
        ("secret.pdf", blank_pdf(password="pw"), UnreadableDocument),
        ("empty.docx", docx_bytes(lambda d: d.add_paragraph(" ")), EmptyDocument),
    ],
)
async def test_bad_files_are_rejected_and_nothing_is_saved(
    session_factory, ingestion, filename, data, error
):
    with pytest.raises(error):
        await ingestion.submit(data, _user(filename))

    assert await _documents(session_factory) == []


async def test_too_large_file_is_rejected(session_factory, ingestion):
    with pytest.raises(FileTooLarge):
        await ingestion.submit(b"x" * (1024 * 1024 + 1), _user())

    assert await _documents(session_factory) == []


def test_validate_upload_checks_extension_and_size_before_download(ingestion):
    ingestion.validate_upload("ok.PDF", 100)
    ingestion.validate_upload("ok.txt", None)
    with pytest.raises(UnsupportedFormat):
        ingestion.validate_upload("virus.exe", 100)
    with pytest.raises(FileTooLarge):
        ingestion.validate_upload("big.pdf", 2 * 1024 * 1024)


async def test_indexing_failure_marks_document_failed_and_allows_retry(
    session_factory, ingestion, embedder
):
    data = "Порада, яку не вдалося проіндексувати.".encode()
    embedder.fail_passages = True
    submission = await ingestion.submit(data, _user())

    with pytest.raises(RuntimeError):
        await ingestion.index(submission)

    (doc,) = await _documents(session_factory)
    assert doc.status is DocStatus.FAILED

    embedder.fail_passages = False
    await ingestion.ingest(data, _user(owner=2, name="Борсук", username=None))  # повторная загрузка

    (doc,) = await _documents(session_factory)
    assert doc.status is DocStatus.READY
    assert doc.uploader_tg_id == 2


async def test_document_deleted_during_indexing_is_not_an_error(
    session_factory,
    ingestion,
    embedder,
    monkeypatch,
):
    submission = await ingestion.submit("Порада.".encode(), _user())

    embedding_started = asyncio.Event()
    continue_embedding = asyncio.Event()
    original_embed_passages = embedder.embed_passages

    async def blocked_embed_passages(
        texts: Sequence[str],
    ) -> list[list[float]]:
        embedding_started.set()
        await continue_embedding.wait()
        return await original_embed_passages(texts)

    monkeypatch.setattr(
        embedder,
        "embed_passages",
        blocked_embed_passages,
    )

    task = asyncio.create_task(ingestion.index(submission))

    try:
        await asyncio.wait_for(embedding_started.wait(), timeout=5)

        async with session_factory() as session:
            await session.execute(
                Document.__table__.delete().where(Document.id == submission.document_id)
            )
            await session.commit()
    finally:
        continue_embedding.set()

    result = await asyncio.wait_for(task, timeout=5)
    assert result is False


async def test_delete_waits_until_indexing_transaction_finishes(
    session_factory,
    ingestion,
    monkeypatch,
):
    """Удаление ждёт снятия FOR UPDATE, затем удаляет документ и его фрагменты."""
    submission = await ingestion.submit(
        "Порада про безпечну зимівлю їжака.".encode(),
        _user(),
    )

    chunks_write_started = asyncio.Event()
    allow_chunk_write = asyncio.Event()
    original_add_chunks = add_chunks

    async def blocked_add_chunks(
        session: AsyncSession,
        document_id: int,
        chunks: Sequence[NewChunk],
        *,
        expected_dim: int,
    ) -> None:
        # index() уже получил SELECT ... FOR UPDATE для строки документа.
        chunks_write_started.set()
        await allow_chunk_write.wait()

        await original_add_chunks(
            session,
            document_id,
            chunks,
            expected_dim=expected_dim,
        )

    monkeypatch.setattr(
        "services.ingestion.add_chunks",
        blocked_add_chunks,
    )

    loop = asyncio.get_running_loop()
    delete_pid_future: asyncio.Future[int] = loop.create_future()

    async def delete_document() -> None:
        async with session_factory() as session:
            pid = await session.scalar(text("SELECT pg_backend_pid()"))
            assert pid is not None
            delete_pid_future.set_result(pid)

            deleted = await delete_user_document(
                session,
                submission.document_id,
                owner_tg_id=1,
            )
            assert deleted is True
            await session.commit()

    index_task = asyncio.create_task(ingestion.index(submission))
    delete_task: asyncio.Task[None] | None = None

    try:
        await asyncio.wait_for(chunks_write_started.wait(), timeout=5)

        delete_task = asyncio.create_task(delete_document())
        delete_pid = await asyncio.wait_for(delete_pid_future, timeout=5)

        # Ждём подтверждения от PostgreSQL, что DELETE действительно заблокирован.
        # Одной проверки "задача пока не завершилась" недостаточно.
        deadline = loop.time() + 5

        async with session_factory() as observer:
            while loop.time() < deadline:
                if delete_task.done():
                    await delete_task
                    pytest.fail(
                        "DELETE завершился, хотя транзакция индексации "
                        "должна удерживать блокировку строки"
                    )

                result = await observer.execute(
                    text(
                        """
                        SELECT wait_event_type
                        FROM pg_stat_activity
                        WHERE pid = :pid
                          AND state = 'active'
                        """
                    ),
                    {"pid": delete_pid},
                )
                wait_event_type = result.scalar_one_or_none()

                if wait_event_type == "Lock":
                    break

                await asyncio.sleep(0.02)
            else:
                pytest.fail("PostgreSQL не показал ожидание блокировки для DELETE")
    finally:
        # Разрешаем индексации продолжить работу, чтобы не оставить
        # фоновые задачи заблокированными даже при ошибке теста.
        allow_chunk_write.set()

        tasks = [index_task]
        if delete_task is not None:
            tasks.append(delete_task)

        results = await asyncio.wait_for(
            asyncio.gather(*tasks, return_exceptions=True),
            timeout=10,
        )

    # Проверяем исключения фоновых задач, если основная часть теста
    # не завершилась через pytest.fail().
    for result in results:
        if isinstance(result, BaseException):
            raise result

    assert results[0] is True

    async with session_factory() as session:
        document = await session.get(Document, submission.document_id)
        chunk_count = await session.scalar(
            select(func.count())
            .select_from(Chunk)
            .where(Chunk.document_id == submission.document_id)
        )

    assert document is None
    assert chunk_count == 0


async def test_mark_processing_as_failed_on_startup(session_factory, ingestion):
    await ingestion.submit("Порада.".encode(), _user())
    async with session_factory() as session:
        assert await mark_processing_as_failed(session) == 1
        await session.commit()

    (doc,) = await _documents(session_factory)
    assert doc.status is DocStatus.FAILED


async def test_chunks_keep_page_and_section(session_factory, ingestion):
    def build(document):
        document.add_heading("Зима", level=1)
        document.add_paragraph("Тримайтеся разом у холодні дні.")

    await ingestion.ingest(docx_bytes(build), _user("guide.docx"))

    async with session_factory() as session:
        chunks = list((await session.scalars(select(Chunk))).all())
    assert {c.section for c in chunks} == {"Зима"}


# ---------------------------------------------------------------- синхронизація архіву


def _write(directory: Path, name: str, content: str) -> None:
    path = directory / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


async def _archive_rows(session_factory) -> dict[str, str]:
    docs = await _documents(session_factory)
    return {d.source_path: d.status.value for d in docs if d.source_type is SourceType.ARCHIVE}


async def test_scan_ignores_hidden_and_unsupported_files(tmp_path):
    _write(tmp_path, "a.txt", "текст")
    _write(tmp_path, "sub/b.txt", "інший текст")
    _write(tmp_path, ".DS_Store", "junk")
    _write(tmp_path, ".hidden/c.txt", "прихований")
    _write(tmp_path, "notes.md", "не підтримується")

    assert sorted(scan_archive(tmp_path)) == ["a.txt", "sub/b.txt"]


async def test_sync_adds_then_is_idempotent(session_factory, ingestion, tmp_path):
    _write(tmp_path, "a.txt", "Перша порада про зиму.")
    _write(tmp_path, "b.txt", "Друга порада про літо.")

    first = await sync_archive(session_factory, ingestion, tmp_path)
    second = await sync_archive(session_factory, ingestion, tmp_path)

    assert first.added == 2
    assert (second.added, second.removed, second.promoted, second.relinked) == (0, 0, 0, 0)
    assert await _archive_rows(session_factory) == {"a.txt": "ready", "b.txt": "ready"}


async def test_sync_handles_rename_change_and_delete(
    session_factory, ingestion, embedder, tmp_path
):
    _write(tmp_path, "a.txt", "Порада про зиму.")
    _write(tmp_path, "b.txt", "Порада про літо.")
    _write(tmp_path, "c.txt", "Порада про осінь.")
    await sync_archive(session_factory, ingestion, tmp_path)
    embedder.passage_calls.clear()

    (tmp_path / "a.txt").rename(tmp_path / "renamed.txt")  # переименование
    _write(tmp_path, "b.txt", "Нова порада про літо.")  # изменение содержимого
    (tmp_path / "c.txt").unlink()  # удаление
    report = await sync_archive(session_factory, ingestion, tmp_path)

    assert (report.relinked, report.added, report.removed) == (1, 1, 2)  # удалены c и старый b
    assert await _archive_rows(session_factory) == {"renamed.txt": "ready", "b.txt": "ready"}
    assert len(embedder.passage_calls) == 1  # переиндексирован только изменённый файл


async def test_sync_skips_duplicate_files_inside_archive(session_factory, ingestion, tmp_path):
    _write(tmp_path, "a.txt", "Однакова порада.")
    _write(tmp_path, "copy.txt", "Однакова порада.")

    report = await sync_archive(session_factory, ingestion, tmp_path)

    assert (report.added, report.skipped_duplicates) == (1, 1)
    assert list(await _archive_rows(session_factory)) == ["a.txt"]


async def test_deleting_canonical_path_keeps_document_when_copy_exists(
    session_factory, ingestion, tmp_path
):
    _write(tmp_path, "a.txt", "Однакова порада.")
    _write(tmp_path, "copy.txt", "Однакова порада.")
    await sync_archive(session_factory, ingestion, tmp_path)

    (tmp_path / "a.txt").unlink()
    report = await sync_archive(session_factory, ingestion, tmp_path)

    assert (report.relinked, report.removed) == (1, 0)
    assert await _archive_rows(session_factory) == {"copy.txt": "ready"}


async def test_user_document_is_promoted_and_survives_owner_deleting(
    session_factory, ingestion, tmp_path
):
    data = "Порада від зайця.".encode()
    await ingestion.ingest(data, _user("hare.txt", owner=7))
    _write(tmp_path, "hare.txt", "Порада від зайця.")

    report = await sync_archive(session_factory, ingestion, tmp_path)

    assert (report.promoted, report.added) == (1, 0)
    (doc,) = await _documents(session_factory)
    assert doc.source_type is SourceType.ARCHIVE
    assert doc.source_path == "hare.txt"
    assert doc.uploader_tg_id is None


async def test_sync_never_removes_user_documents(session_factory, ingestion, tmp_path):
    await ingestion.ingest("Порада зайця.".encode(), _user())
    _write(tmp_path, "a.txt", "Порада архіву.")

    await sync_archive(session_factory, ingestion, tmp_path)
    (tmp_path / "a.txt").unlink()
    _write(tmp_path, "b.txt", "Інша порада архіву.")
    await sync_archive(session_factory, ingestion, tmp_path)

    kinds = sorted(d.source_type.value for d in await _documents(session_factory))
    assert kinds == ["archive", "user"]


async def test_missing_or_empty_folder_does_not_wipe_archive(session_factory, ingestion, tmp_path):
    _write(tmp_path, "a.txt", "Порада про зиму.")
    await sync_archive(session_factory, ingestion, tmp_path)

    await sync_archive(session_factory, ingestion, tmp_path / "does-not-exist")
    for path in tmp_path.iterdir():
        path.unlink()
    await sync_archive(session_factory, ingestion, tmp_path)

    assert await _archive_rows(session_factory) == {"a.txt": "ready"}


async def test_bad_archive_file_does_not_stop_others(session_factory, ingestion, tmp_path):
    (tmp_path / "scan.pdf").write_bytes(blank_pdf())
    _write(tmp_path, "good.txt", "Хороша порада.")

    report = await sync_archive(session_factory, ingestion, tmp_path)

    assert (report.added, report.failed) == (1, 1)
    assert await _archive_rows(session_factory) == {"good.txt": "ready"}


async def test_failed_archive_file_is_retried_on_next_start(
    session_factory, ingestion, embedder, tmp_path
):
    _write(tmp_path, "a.txt", "Порада про зиму.")
    embedder.fail_passages = True
    first = await sync_archive(session_factory, ingestion, tmp_path)
    embedder.fail_passages = False

    second = await sync_archive(session_factory, ingestion, tmp_path)

    assert (first.failed, second.added) == (1, 1)
    assert await _archive_rows(session_factory) == {"a.txt": "ready"}


async def test_search_orders_by_distance_and_counts_chunks(session_factory, ingestion, embedder):
    await ingestion.ingest("Їжак ховає горіхи на зиму.".encode(), _user("a.txt", owner=1))
    await ingestion.ingest("Вовк виє на місяць уночі.".encode(), _user("b.txt", owner=2))

    async with session_factory() as session:
        found = await search_chunks(session, await embedder.embed_query("горіхи їжак"), limit=5)
        total = await session.scalar(select(func.count()).select_from(Chunk))

    assert [f.title for f in found] == ["a", "b"]
    assert found[0].distance < found[1].distance
    assert total == 2
    assert isinstance(found[0].uploaded_at, datetime) and found[0].uploaded_at.tzinfo is not None
    assert datetime.now(UTC) >= found[0].uploaded_at


async def test_unexpected_integrity_error_marks_document_failed(
    session_factory,
    ingestion,
    monkeypatch,
):
    submission = await ingestion.submit(
        "Порада, яка спричинить помилку БД.".encode(),
        _user(),
    )

    async def fail_add_chunks(*args: object, **kwargs: object) -> None:
        raise IntegrityError(
            "INSERT INTO chunks",
            {},
            RuntimeError("simulated integrity violation"),
        )

    monkeypatch.setattr(
        "services.ingestion.add_chunks",
        fail_add_chunks,
    )

    with pytest.raises(IntegrityError):
        await ingestion.index(submission)

    (document,) = await _documents(session_factory)
    assert document.status is DocStatus.FAILED
