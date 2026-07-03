# database/database.py

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from config_data.config import config

# Асинхронный движок для PostgreSQL (asyncpg)
engine = create_async_engine(
    config.db.dsn,
    echo=False,        # True — для отладки SQL-запросов
    pool_size=5,
    max_overflow=10,
)

# Фабрика сессий
async_session_maker = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


async def get_session() -> AsyncSession:
    """
    Возвращает новую асинхронную сессию.
    Использовать как контекстный менеджер:

        async with async_session_maker() as session:
            ...
    """
    return async_session_maker()