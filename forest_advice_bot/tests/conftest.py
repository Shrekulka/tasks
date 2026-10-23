"""Общие фикстуры тестов."""

import logging
import os

import pytest

# Все переменные, которые читает конфиг. Перед каждым тестом они удаляются из окружения,
# чтобы настоящий .env или переменные разработчика не влияли на результат.
CONFIG_ENV_NAMES = [
    "TELEGRAM_BOT_TOKEN",
    "POSTGRES_HOST", "POSTGRES_PORT", "POSTGRES_DB", "POSTGRES_USER", "POSTGRES_PASSWORD",
    "LLM_API_KEYS", "LLM_API_KEY", "LLM_MODELS", "LLM_MODELS_BLOCKLIST", "LLM_BASE_URL",
    "LLM_TIMEOUT_SECONDS", "LLM_DEADLINE_SECONDS", "LLM_MAX_MODELS_TRIED", "LLM_MAX_RPM",
    "LLM_PROVIDER", "LLM_MIN_CONTEXT", "LLM_CATALOG_TTL_SECONDS",
    "LLM_RATE_PAUSE_SECONDS", "LLM_ACCOUNT_PAUSE_SECONDS", "LLM_KEY_QUARANTINE_SECONDS",
    "LLM_MODEL_QUARANTINE_SECONDS", "LLM_QUOTA_CACHE_SECONDS", "LLM_DAILY_REQUEST_LIMIT",
    "MAX_PDF_PAGES", "MAX_ZIP_ENTRIES", "MAX_UNPACKED_MB", "LLM_TEMPERATURE", "LLM_MAX_TOKENS",
    "EMBEDDING_QUERY_PREFIX", "EMBEDDING_PASSAGE_PREFIX", "EMBEDDING_BATCH_SIZE",
    "RETRIEVAL_CANDIDATE_FACTOR", "MAX_CONTEXT_CHARS", "DISPLAY_TIMEZONE",
    "PROCESSING_NOTICE_AFTER_SECONDS", "LARGE_DOCUMENT_CHARS",
    "ANSWER_LANGUAGE", "LANGUAGE_PROFILES_DIR", "PROMPTS_DIR",
    "LANGUAGE_MARKER_MIN_CHARS",
    "LLM_MAX_LIMITED_ATTEMPTS", "LLM_LIMIT_PAUSE_SECONDS", "LLM_JSON_MODE",
    "LLM_INVALID_RESPONSE_STRATEGY", "LLM_UPSTREAM_QUARANTINE_SECONDS",
    "LLM_COUNT_FAILED_REQUESTS", "LLM_DAILY_RESET_THRESHOLD_SECONDS",
    "ANSWER_CACHE_TTL_SECONDS", "ANSWER_CACHE_MAX_ENTRIES",
    "MYDOCS_PAGE_SIZE", "SHUTDOWN_TIMEOUT_SECONDS", "RELEVANCE_MARGIN",
    "LLM_PAIR_FAILURES_BEFORE_MODEL_QUARANTINE",
    "EMBEDDING_MODEL", "EMBEDDING_DIM", "TOP_K", "RELEVANCE_THRESHOLD",
    "CHUNK_SIZE", "CHUNK_OVERLAP", "ARCHIVE_DIR", "MAX_FILE_MB",
    "UPLOADS_PER_HOUR", "QUESTIONS_PER_MINUTE", "MAX_QUESTION_CHARS",
    "LOG_LEVEL", "LOG_COLOR", "LOG_TO_FILE", "LOG_DIR",
]  # fmt: skip

FAKE_TG_TOKEN = "123456789:AAH_fake_token_for_tests_0123456789abcd"
FAKE_LLM_KEY = "sk-or-v1-fakekeyfortests0123456789abcdef"
FAKE_GROQ_KEY = "gsk_fakekeyfortests0123456789abcdefgh"  # формат прежнего провайдера


@pytest.fixture
def clean_env(monkeypatch):
    """Чистое окружение: ни одной переменной конфига."""
    for name in CONFIG_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


@pytest.fixture
def valid_env(clean_env):
    """Минимально достаточный набор переменных для успешной загрузки конфига."""
    clean_env.setenv("TELEGRAM_BOT_TOKEN", FAKE_TG_TOKEN)
    clean_env.setenv("POSTGRES_DB", "forest")
    clean_env.setenv("POSTGRES_USER", "forest_user")
    clean_env.setenv("POSTGRES_PASSWORD", "s3cret-password")
    clean_env.setenv("LLM_API_KEYS", FAKE_LLM_KEY)
    return clean_env


@pytest.fixture
def no_dotenv(tmp_path):
    """Путь к несуществующему .env: загрузчик ничего не прочитает с диска."""
    return tmp_path / "no-such.env"


@pytest.fixture(autouse=True)
def restore_root_logger():
    """setup_logging() меняет корневой логгер. Возвращаем всё как было после каждого теста."""
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    yield
    for handler in root.handlers[:]:
        root.removeHandler(handler)
        handler.close()
    for handler in handlers:
        root.addHandler(handler)
    root.setLevel(level)


def pytest_collection_modifyitems(config, items):
    """Тесты с меткой integration нужен настоящий PostgreSQL. Без TEST_DATABASE_URL
    они пропускаются, чтобы обычный `pytest` работал у любого и без базы."""
    if os.environ.get("TEST_DATABASE_URL"):
        return
    skip = pytest.mark.skip(reason="не задана переменная TEST_DATABASE_URL")
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip)
