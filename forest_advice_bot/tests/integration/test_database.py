"""Интеграционные тесты: нужен настоящий PostgreSQL с pgvector.

Запуск:
TEST_DATABASE_URL=postgresql+asyncpg://user:pass@127.0.0.1:5432/db pytest -m integration

Без переменной эти тесты пропускаются.
"""

import os

import pytest
from sqlalchemy import inspect, select, text
from sqlalchemy.engine import Connection

from database.database import (
    create_engine,
    create_session_factory,
    ensure_vector_extension,
    init_db,
    wait_for_database,
)
from models.models import Chunk, DocStatus, Document, SourceType

pytestmark = pytest.mark.integration


@pytest.fixture
def db_url() -> str:
    url = os.environ.get("TEST_DATABASE_URL")

    if not url:
        pytest.skip("Переменная TEST_DATABASE_URL не задана")

    return url


def _get_table_names(connection: Connection) -> set[str]:
    """Возвращает имена таблиц через синхронное соединение SQLAlchemy."""
    inspector = inspect(connection)

    if inspector is None:
        raise RuntimeError("Не удалось создать SQLAlchemy Inspector")

    return set(inspector.get_table_names())


async def test_wait_for_database_succeeds(db_url: str) -> None:
    await wait_for_database(db_url, attempts=3, delay=0.1)


async def test_extension_creation_is_idempotent(db_url: str) -> None:
    await ensure_vector_extension(db_url)
    await ensure_vector_extension(db_url)

    engine = create_engine(db_url)

    try:
        async with engine.connect() as connection:
            found = await connection.scalar(
                text("SELECT count(*) FROM pg_extension WHERE extname = 'vector'")
            )
    finally:
        await engine.dispose()

    assert found == 1


async def test_vector_roundtrip_and_cosine_search(db_url: str) -> None:
    """Вставка вектора, чтение списком и поиск по косинусной близости через ORM."""
    await ensure_vector_extension(db_url)

    engine = create_engine(db_url)
    factory = create_session_factory(engine)

    try:
        await init_db(engine)
        async with engine.begin() as connection:
            await connection.execute(text("TRUNCATE chunks, documents RESTART IDENTITY CASCADE"))

        async with factory() as session:
            doc = Document(
                sha256="c" * 64,
                title="t",
                filename="f.txt",
                source_type=SourceType.USER,
                uploader_tg_id=1,
                size_bytes=1,
                format="txt",
                status=DocStatus.READY,
            )
            session.add(doc)
            await session.flush()
            session.add_all(
                [
                    Chunk(
                        document_id=doc.id, chunk_index=0, content="near", embedding=[0.1, 0.2, 0.3]
                    ),
                    Chunk(
                        document_id=doc.id, chunk_index=1, content="far", embedding=[0.9, 0.1, 0.0]
                    ),
                ]
            )
            await session.commit()

        async with factory() as session:
            distance = Chunk.embedding.cosine_distance([0.1, 0.2, 0.3]).label("distance")
            rows = (
                await session.execute(
                    select(Chunk.content, Chunk.embedding, distance).order_by(distance).limit(2)
                )
            ).all()
    finally:
        await engine.dispose()

    assert [row.content for row in rows] == ["near", "far"]
    assert rows[0].embedding == pytest.approx([0.1, 0.2, 0.3])
    assert rows[0].distance == pytest.approx(0.0, abs=1e-6)


async def test_init_db_creates_schema(db_url: str) -> None:
    await ensure_vector_extension(db_url)

    engine = create_engine(db_url)

    try:
        await init_db(engine)

        async with engine.connect() as connection:
            tables = await connection.run_sync(_get_table_names)
    finally:
        await engine.dispose()

    assert {"documents", "chunks"} <= tables
