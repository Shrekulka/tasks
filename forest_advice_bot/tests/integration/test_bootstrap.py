"""Сборка сервисов при старте: схема эмбеддингов, зависшие документы, синхронизация архива."""

import dataclasses
import os

import aiohttp
import pytest
from sqlalchemy import select, text

import bot as bot_module
from config_data.config import load_config
from database.database import (
    EmbeddingSchemaMismatch,
    create_engine,
    create_session_factory,
    ensure_vector_extension,
    init_db,
)
from models.models import DocStatus, Document, SourceType
from services.answering import AnswerService
from services.ingestion import DocumentSource, IngestionService
from tests.fake_embedder import HashEmbedder

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


@pytest.fixture
def config(valid_env, no_dotenv, tmp_path):
    base = load_config(no_dotenv)
    rag = dataclasses.replace(base.rag, archive_dir=tmp_path, embedding_dim=64)
    return dataclasses.replace(base, rag=rag)


async def test_build_services_syncs_archive_and_returns_services(session_factory, config, tmp_path):
    (tmp_path / "zyma.txt").write_text("Взимку тримайтеся разом.", encoding="utf-8")

    async with aiohttp.ClientSession() as http:
        services = await bot_module.build_services(
            config, session_factory, http, embedder=HashEmbedder()
        )

    assert isinstance(services["answers"], AnswerService)
    assert isinstance(services["ingestion"], IngestionService)
    async with session_factory() as session:
        docs = list((await session.scalars(select(Document))).all())
    assert [(d.source_path, d.source_type, d.status) for d in docs] == [
        ("zyma.txt", SourceType.ARCHIVE, DocStatus.READY)
    ]


async def test_restart_marks_stuck_documents_failed_and_is_idempotent(session_factory, config):
    embedder = HashEmbedder()
    ingestion = IngestionService(session_factory, embedder, config.rag)
    await ingestion.submit(
        "Порада.".encode(), DocumentSource(SourceType.USER, "a.txt", None, 1, "Їжак", None)
    )

    async with aiohttp.ClientSession() as http:
        await bot_module.build_services(config, session_factory, http, embedder=embedder)
        await bot_module.build_services(config, session_factory, http, embedder=embedder)

    async with session_factory() as session:
        (doc,) = (await session.scalars(select(Document))).all()
    assert doc.status is DocStatus.FAILED


async def test_changed_embedding_model_is_refused(session_factory, config):
    async with aiohttp.ClientSession() as http:
        await bot_module.build_services(
            config, session_factory, http, embedder=HashEmbedder(model_name="model-a")
        )
        with pytest.raises(EmbeddingSchemaMismatch):
            await bot_module.build_services(
                config, session_factory, http, embedder=HashEmbedder(model_name="model-b")
            )


async def test_missing_archive_folder_does_not_stop_startup(session_factory, config, tmp_path):
    rag = dataclasses.replace(config.rag, archive_dir=tmp_path / "missing")
    config = dataclasses.replace(config, rag=rag)

    async with aiohttp.ClientSession() as http:
        services = await bot_module.build_services(
            config, session_factory, http, embedder=HashEmbedder()
        )

    assert "answers" in services
