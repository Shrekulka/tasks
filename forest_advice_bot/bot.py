"""Точка входа. Единственное место, где создаются объекты приложения.

Запуск: `python bot.py` (в Docker это делает команда CMD из Dockerfile).
"""

import asyncio
import logging
import sys
from collections.abc import Mapping
from typing import Any

import aiohttp
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramNetworkError, TelegramUnauthorizedError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from config_data.config import Config, ConfigError, load_config
from database.database import (
    create_engine,
    create_session_factory,
    ensure_embedding_schema,
    ensure_vector_extension,
    init_db,
    wait_for_database,
)
from errors.handlers import on_error
from external_services.llm.llm_client import LLMClient
from external_services.llm.registry import create_provider
from handlers import common, documents, fallback, groups, mydocs, questions
from keyboards.set_menu import set_main_menu
from logger_config import log_asyncio_exception, setup_logging
from middlewares.throttling import ThrottlingMiddleware, rules_from_config
from models.methods import mark_processing_as_failed
from services.answer_cache import AnswerCache
from services.answering import Answer, AnswerService
from services.archive_sync import sync_archive
from services.background import BackgroundTasks
from services.embeddings import Embedder, SentenceTransformerEmbedder
from services.ingestion import IngestionService
from services.retrieval import Retriever
from utils.utils import InFlightGuard

log = logging.getLogger(__name__)


def build_dispatcher(
    config: Config,
    session_factory: async_sessionmaker[AsyncSession],
    services: Mapping[str, Any] | None = None,
) -> Dispatcher:
    """Собирает диспетчер. Всё, что передано как именованный аргумент, aiogram будет
    подставлять в хендлеры по имени (так работает внедрение зависимостей).

    services: готовые сервисы приложения (ingestion, answers, ...). Хендлеры получают их
    по имени аргумента.
    """
    dispatcher = Dispatcher(config=config, session_factory=session_factory, **(services or {}))

    # Ограничение частоты вешается на роутеры с помеченными хендлерами (flags={"throttle": ...}).
    throttle = ThrottlingMiddleware(rules_from_config(config.limits))
    questions_router = questions.create_router()
    documents_router = documents.create_router()
    questions_router.message.middleware(throttle)
    documents_router.message.middleware(throttle)

    # Порядок важен: fallback подключается последним и ловит только то, что не взяли другие.
    dispatcher.include_routers(
        common.create_router(),
        mydocs.create_router(),
        documents_router,
        questions_router,
        groups.create_router(),
        fallback.create_router(),
    )
    dispatcher.errors.register(on_error)
    return dispatcher


async def build_services(
    config: Config,
    session_factory: async_sessionmaker[AsyncSession],
    http_session: aiohttp.ClientSession,
    *,
    embedder: Embedder | None = None,
) -> dict[str, Any]:
    """Создаёт сервисы, проверяет схему эмбеддингов и синхронизирует архив.

    embedder можно передать готовый (в тестах); иначе грузится модель из конфигурации.
    Загрузка модели блокирующая, поэтому идёт в отдельном потоке.
    """
    if embedder is None:
        embedder = await asyncio.to_thread(SentenceTransformerEmbedder.load, config.rag)

    async with session_factory() as session:
        # Размерность и имя модели сверяются с записанными в БД: смена модели без
        # переиндексации закончилась бы смешением несовместимых векторов.
        await ensure_embedding_schema(session, embedder.model_name, embedder.dim)
        stuck = await mark_processing_as_failed(session)
        await session.commit()
    if stuck:
        log.warning("Документов, застрявших в обработке после сбоя: %d (помечены failed)", stuck)

    # Кэш ответов привязан к версии коллекции: любое изменение документов сбрасывает его.
    cache: AnswerCache[Answer] = AnswerCache(
        ttl_seconds=config.rag.cache_ttl_seconds, max_entries=config.rag.cache_max_entries
    )
    llm = LLMClient(create_provider(config.llm, http_session), config.llm)
    ingestion = IngestionService(session_factory, embedder, config.rag, on_change=cache.bump)
    retriever = Retriever(session_factory, embedder, config.rag)
    answers = AnswerService(
        retriever,
        llm,
        rag=config.rag,
        llm_config=config.llm,
        timezone=config.tg_bot.display_timezone,
        cache=cache,
    )

    await sync_archive(session_factory, ingestion, config.rag.archive_dir, on_change=cache.bump)
    return {
        "ingestion": ingestion,
        "answers": answers,
        "retriever": retriever,
        "cache": cache,
        "tasks": BackgroundTasks(),
        "question_guard": InFlightGuard[int](),
    }


async def main() -> None:
    config = load_config()
    setup_logging(
        level=config.log.level,
        color=config.log.color,
        log_to_file=config.log.to_file,
        log_dir=config.log.dir,
    )
    log.info("Конфигурация загружена: %s", config.summary())
    asyncio.get_running_loop().set_exception_handler(log_asyncio_exception)

    await wait_for_database(config.db.url)
    await ensure_vector_extension(config.db.url)
    engine = create_engine(config.db.url)
    bot = Bot(token=config.tg_bot.token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    http_session = aiohttp.ClientSession()
    services: dict[str, Any] = {}
    try:
        # Токен проверяем первым: неверный токен не должен ждать загрузки модели и архива.
        me = await bot.get_me()
        await init_db(engine)
        session_factory = create_session_factory(engine)
        services = await build_services(config, session_factory, http_session)
        dispatcher = build_dispatcher(config, session_factory, services)
        await set_main_menu(bot)
        log.info("Бот запущен: @%s", me.username)
        await dispatcher.start_polling(bot)
    finally:
        # Что открыли, то закрываем: иначе остаются висящие подключения и сессии.
        tasks = services.get("tasks")
        if tasks is not None:  # дожидаемся индексации, не бросая документы в processing
            await tasks.shutdown(config.limits.shutdown_timeout_seconds)
        await http_session.close()
        await bot.session.close()
        await engine.dispose()
        log.info("Бот остановлен")


def run() -> None:
    try:
        asyncio.run(main())
    except ConfigError as exc:
        sys.exit(str(exc))  # понятное сообщение в stderr и код выхода 1, без traceback
    except TelegramUnauthorizedError:
        sys.exit("Telegram отклонил токен бота. Проверьте TELEGRAM_BOT_TOKEN в .env.")
    except TelegramNetworkError as exc:
        # Детали ошибки не печатаем: в них может оказаться адрес запроса.
        sys.exit(
            f"Не удалось связаться с Telegram ({type(exc).__name__}). "
            "Проверьте интернет; контейнер перезапустится и повторит попытку."
        )
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    run()
