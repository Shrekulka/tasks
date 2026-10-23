"""Подключение к PostgreSQL: движок, сессии, расширение pgvector.

Порядок старта (его важно не перепутать):
  1. wait_for_database: дождаться, пока база начнёт принимать подключения;
  2. ensure_vector_extension: СОЗДАТЬ расширение vector (отдельным коротким подключением);
  3. create_engine: рабочий движок;
  4. init_db: создать таблицы (колонка типа vector требует, чтобы расширение уже было).

Тип vector кодирует и декодирует сам pgvector.sqlalchemy.Vector (через текстовый формат).
Регистрировать кодек pgvector.asyncpg.register_vector для движка SQLAlchemy НЕЛЬЗЯ: кодек ждёт
list или ndarray, а Vector отдаёт строку, и вставка падает с DataError.
"""

import asyncio
import logging

from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from errors.errors import DatabaseUnavailable

log = logging.getLogger(__name__)


class Base(DeclarativeBase):
    """Общий родитель всех ORM-моделей."""


async def wait_for_database(url: str, attempts: int = 30, delay: float = 1.0) -> None:
    """Ждёт, пока база начнёт принимать подключения.

    `depends_on: service_healthy` в docker-compose уже снижает риск, но не убирает его
    полностью (например, при перезапуске базы). Поэтому бот ещё и сам повторяет попытки.
    """
    engine = create_async_engine(url)
    try:
        for attempt in range(1, attempts + 1):
            try:
                async with engine.connect() as connection:
                    await connection.execute(text("SELECT 1"))
                log.info("База данных доступна (попытка %d)", attempt)
                return
            except (OSError, SQLAlchemyError) as exc:
                log.warning(
                    "База данных пока недоступна (попытка %d из %d): %s",
                    attempt,
                    attempts,
                    type(exc).__name__,
                )
                await asyncio.sleep(delay)
    finally:
        await engine.dispose()
    raise DatabaseUnavailable(f"База данных не ответила за {attempts} попыток")


async def ensure_vector_extension(url: str) -> None:
    """CREATE EXTENSION IF NOT EXISTS vector. Безопасно вызывать сколько угодно раз.

    Нужны права на создание расширений. В официальном образе PostgreSQL пользователь
    POSTGRES_USER является суперпользователем, поэтому в нашем compose это работает.
    """
    engine = create_async_engine(url, isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as connection:
            await connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
    finally:
        await engine.dispose()
    log.info("Расширение pgvector готово")


def create_engine(url: str) -> AsyncEngine:
    """Рабочий движок.

    pool_pre_ping: перед выдачей подключения из пула проверяет, что оно живое. Без этого после
    перезапуска базы первый запрос упал бы на «мёртвом» подключении.
    """
    return create_async_engine(url, pool_pre_ping=True)


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    # expire_on_commit=False: после commit объекты остаются читаемыми, а не требуют нового
    # запроса к базе (в async-коде такое ленивое обновление вызвало бы ошибку).
    return async_sessionmaker(engine, expire_on_commit=False)


async def init_db(engine: AsyncEngine) -> None:
    """Создаёт таблицы, которых ещё нет."""
    import models.models  # noqa: F401  регистрирует таблицы в Base.metadata

    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    log.info("Таблицы базы данных готовы")


class EmbeddingSchemaMismatch(RuntimeError):
    """Модель или размерность эмбеддингов изменились: нужна переиндексация."""


async def ensure_embedding_schema(session: AsyncSession, model_name: str, dim: int) -> None:
    """Первый запуск запоминает модель и размерность, следующие сверяются с ними."""
    from models.models import AppMeta  # импорт внутри: models.models импортирует этот модуль

    rows = await session.scalars(
        select(AppMeta).where(AppMeta.key.in_(("embedding_model", "embedding_dim")))
    )
    stored = {row.key: row.value for row in rows}

    if not stored:
        legacy_dim = await session.scalar(text("SELECT vector_dims(embedding) FROM chunks LIMIT 1"))
        if legacy_dim is not None and legacy_dim != dim:
            raise EmbeddingSchemaMismatch(
                f"В БД векторы размерности {legacy_dim}, а модель выдаёт {dim}."
            )
        session.add_all(
            [
                AppMeta(key="embedding_model", value=model_name),
                AppMeta(key="embedding_dim", value=str(dim)),
            ]
        )
        await session.commit()
        return

    if stored.get("embedding_model") != model_name or stored.get("embedding_dim") != str(dim):
        raise EmbeddingSchemaMismatch(
            f"БД проиндексирована моделью {stored.get('embedding_model')} "
            f"(размерность {stored.get('embedding_dim')}), а сейчас {model_name} ({dim}). "
            "Очистите коллекцию и переиндексируйте (для разработки: docker compose down -v)."
        )
