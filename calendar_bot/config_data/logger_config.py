# logger_config.py

"""
Конфигурация логирования для Telegram-бота.

Объединяет лучшее из двух подходов:
- dictConfig (чистая декларативная конфигурация, легко читать/переиспользовать)
- Per-level цвета из ColoredLevelFormatter
- Emoji-иконки в файловых логах из FileEmojiFormatter
- TimedRotatingFileHandler с ротацией по дате
- Уровень логирования через переменную окружения DEBUG (как в fit_trainer_bot)
- CustomFilter для корректного отображения caller info

Использование во всех модулях:
    from logger_config import logger
    logger.info("Сообщение")
    logger.error("Ошибка")
"""

import logging
import logging.config
import logging.handlers
import os
from pathlib import Path

LOG_DIR = Path("logs")
LOG_DIR.mkdir(exist_ok=True)

# Уровень логирования: DEBUG в разработке, INFO в продакшене.
# Управляется через .env: DEBUG=True → уровень DEBUG, иначе INFO.
# Это позволяет не менять код при деплое — только .env.
_IS_DEBUG = os.getenv("DEBUG", "False").lower() == "true"
_APP_LEVEL = "DEBUG" if _IS_DEBUG else "INFO"


class _CallerFilter(logging.Filter):
    """
    Фильтр, который корректирует поля pathname/funcName/lineno в LogRecord.

    Проблема без этого фильтра: если вызвать logger.info() из handlers/user_handlers.py,
    %(pathname)s и %(lineno)d покажут logger_config.py (где вызван adapter.log()),
    а не реальный файл-источник.

    Решение: фильтр заменяет стандартные поля на кастомные, если они переданы
    через LoggerAdapter.process() → kwargs['extra'].

    Аналог подхода из fit_trainer_bot (CustomFilter + LoggerAdapter),
    но без inspect.currentframe() — стандартный logging сам правильно
    определяет caller через stacklevel при вызове logger.xxx() напрямую
    (без промежуточных обёрток).
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if hasattr(record, "custom_pathname"):
            record.pathname = record.custom_pathname
        if hasattr(record, "custom_funcname"):
            record.funcName = record.custom_funcname
        if hasattr(record, "custom_lineno"):
            record.lineno = record.custom_lineno
        return True


def _build_logging_config() -> dict:
    """
    Возвращает словарь конфигурации для logging.config.dictConfig().

    Выделено в функцию (как в handwriting_converter), а не задано как модульная
    константа — это позволяет вызвать повторно при изменении LOG_DIR или уровня
    (например, в тестах).
    """
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "filters": {
            "caller_filter": {
                "()": _CallerFilter,
            },
        },
        "formatters": {
            "colored_console": {
                "()": "config_data.logging_formatters.ColoredLevelFormatter",
            },
            "emoji_file": {
                "()": "config_data.logging_formatters.FileEmojiFormatter",
            },
        },
        "handlers": {
            "console": {
                "class": "logging.StreamHandler",
                "formatter": "colored_console",
                "filters": ["caller_filter"],
                "stream": "ext://sys.stdout",
                "level": _APP_LEVEL,
            },
            "file": {
                # TimedRotatingFileHandler: новый файл каждую полночь.
                # backupCount=14 — хранит 2 недели логов, потом удаляет старые.
                # Аналог из обоих исходных проектов.
                "class": "logging.handlers.TimedRotatingFileHandler",
                "formatter": "emoji_file",
                "filters": ["caller_filter"],
                "filename": str(LOG_DIR / "bot.log"),
                "when": "midnight",
                "interval": 1,
                "backupCount": 14,
                "encoding": "utf-8",
                "level": "DEBUG",  # В файл пишем всё — для отладки по логам без перезапуска
            },
        },
        "loggers": {
            # ── Наше приложение ───────────────────────────────────────────────
            # Единственный логгер, который используется через `from logger_config import logger`.
            # propagate=False — не дублируем записи в root-логгер.
            "calendar_bot": {
                "handlers": ["console", "file"],
                "level": _APP_LEVEL,
                "propagate": False,
            },
            # ── aiogram ───────────────────────────────────────────────────────
            # WARNING в продакшене — иначе aiogram логирует каждый апдейт на INFO,
            # что засоряет вывод. DEBUG при разработке — виден весь диспатч.
            "aiogram": {
                "handlers": ["console", "file"],
                "level": "DEBUG" if _IS_DEBUG else "WARNING",
                "propagate": False,
            },
            # ── aiohttp ───────────────────────────────────────────────────────
            # Логирует входящие webhook-запросы. WARNING — только ошибки сервера.
            "aiohttp": {
                "handlers": ["console", "file"],
                "level": "WARNING",
                "propagate": False,
            },
            # ── APScheduler ───────────────────────────────────────────────────
            # WARNING — только ошибки выполнения джоб (пропущенный запуск и т.п.).
            # INFO добавить, если нужно видеть старт/стоп каждой джобы.
            "apscheduler": {
                "handlers": ["console", "file"],
                "level": "WARNING",
                "propagate": False,
            },
            # ── SQLAlchemy ────────────────────────────────────────────────────
            # WARNING по умолчанию. Для отладки SQL-запросов временно ставим DEBUG —
            # тогда в консоль польётся весь SQL (аналог echo=True в create_async_engine).
            "sqlalchemy": {
                "handlers": ["console", "file"],
                "level": "WARNING",
                "propagate": False,
            },
            # ── Google API клиент ─────────────────────────────────────────────
            "googleapiclient": {
                "handlers": ["console", "file"],
                "level": "WARNING",
                "propagate": False,
            },
        },
        # Root-логгер перехватывает всё, что не попало в именованные логгеры выше.
        # WARNING — чтобы шумные сторонние библиотеки (urllib3, httpcore и т.п.)
        # не засоряли консоль и файл.
        "root": {
            "handlers": ["console", "file"],
            "level": "WARNING",
        },
    }


# Применяем конфигурацию один раз при импорте модуля
logging.config.dictConfig(_build_logging_config())

# Единый логгер приложения — используется во всех модулях:
#   from logger_config import logger
#   logger.info("Бот запущен")
logger = logging.getLogger("calendar_bot")

logger.info("Logger initialized | mode=%s | level=%s", "DEBUG" if _IS_DEBUG else "PRODUCTION", _APP_LEVEL)