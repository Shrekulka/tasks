"""Тесты конфигурации: проверяем поведение, а не реализацию."""

from urllib.parse import unquote, urlsplit

import pytest

from config_data.config import (
    ConfigError,
    load_config,
    load_llm_config,
    parse_csv,
)
from tests.conftest import FAKE_LLM_KEY, FAKE_TG_TOKEN


def test_valid_env_gives_defaults(valid_env, no_dotenv):
    config = load_config(no_dotenv)

    assert config.tg_bot.token == FAKE_TG_TOKEN
    assert config.db.host == "localhost" and config.db.port == 5432
    assert config.llm.api_keys == (FAKE_LLM_KEY,)
    assert config.llm.provider == "openrouter"
    assert config.llm.base_url == ""  # адрес по умолчанию задаёт адаптер провайдера
    assert config.llm.min_context == 8000
    assert config.llm.catalog_ttl_seconds == 3600
    assert config.llm.deadline_seconds >= config.llm.timeout_seconds
    assert config.llm.max_rpm == 15  # меньше лимита бесплатных моделей OpenRouter (20 в минуту)
    assert config.rag.top_k == 5 and config.rag.chunk_overlap < config.rag.chunk_size
    assert config.rag.max_file_mb == 20
    assert config.log.color is None  # auto


def test_all_problems_are_reported_at_once(clean_env, no_dotenv):
    with pytest.raises(ConfigError) as exc_info:
        load_config(no_dotenv)

    problems = exc_info.value.problems
    for name in ("TELEGRAM_BOT_TOKEN", "POSTGRES_DB", "POSTGRES_USER", "POSTGRES_PASSWORD"):
        assert name in problems
    assert "LLM_API_KEYS" in problems
    assert "TELEGRAM_BOT_TOKEN" in str(exc_info.value)  # и в тексте ошибки тоже


def test_garbage_telegram_token_is_rejected(valid_env, no_dotenv):
    valid_env.setenv("TELEGRAM_BOT_TOKEN", "your-token-here")
    with pytest.raises(ConfigError) as exc_info:
        load_config(no_dotenv)
    assert "TELEGRAM_BOT_TOKEN" in exc_info.value.problems
    assert "your-token-here" not in str(exc_info.value)  # значение в ошибке не показываем


def test_repr_does_not_leak_secrets(valid_env, no_dotenv):
    config = load_config(no_dotenv)
    text = repr(config) + config.summary()

    assert FAKE_TG_TOKEN not in text
    assert "s3cret-password" not in text
    assert FAKE_LLM_KEY not in text
    assert "keys=1" in config.summary()  # зато сколько ключей, видно


def test_db_url_escapes_special_characters(valid_env, no_dotenv):
    valid_env.setenv("POSTGRES_PASSWORD", "p@ss:w/rd#1")
    valid_env.setenv("POSTGRES_HOST", "db")
    config = load_config(no_dotenv)

    parts = urlsplit(config.db.url)
    assert parts.scheme == "postgresql+asyncpg"
    assert parts.hostname == "db" and parts.port == 5432
    assert parts.path == "/forest"
    assert unquote(parts.password) == "p@ss:w/rd#1"  # после разбора пароль вернулся исходным


def test_parse_csv_strips_dedupes_and_removes_quotes():
    assert parse_csv(" k1, 'k2' ,,\"k1\" , k3 ") == ("k1", "k2", "k3")
    assert parse_csv("") == ()


def test_llm_keys_list_wins_over_single_key(clean_env, no_dotenv):
    clean_env.setenv("LLM_API_KEYS", "a, b")
    clean_env.setenv("LLM_API_KEY", "single")
    assert load_llm_config(no_dotenv).api_keys == ("a", "b")


def test_llm_single_key_is_used_as_fallback(clean_env, no_dotenv):
    clean_env.setenv("LLM_API_KEY", "single")
    assert load_llm_config(no_dotenv).api_keys == ("single",)


def test_llm_loader_needs_no_telegram_or_db_variables(clean_env, no_dotenv):
    clean_env.setenv("LLM_API_KEYS", "k1")
    clean_env.setenv("LLM_MODELS", "model-a, model-b")
    llm_config = load_llm_config(no_dotenv)  # не падает: TELEGRAM_BOT_TOKEN не нужен
    assert llm_config.models == ("model-a", "model-b")


def test_llm_deadline_must_not_be_less_than_timeout(valid_env, no_dotenv):
    valid_env.setenv("LLM_TIMEOUT_SECONDS", "20")
    valid_env.setenv("LLM_DEADLINE_SECONDS", "10")
    with pytest.raises(ConfigError) as exc_info:
        load_config(no_dotenv)
    assert "LLM_DEADLINE_SECONDS" in exc_info.value.problems


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("TOP_K", "0"),
        ("MAX_FILE_MB", "21"),  # больше лимита Telegram на скачивание
        ("RELEVANCE_THRESHOLD", "1.5"),
        ("POSTGRES_PORT", "70000"),
        ("LOG_LEVEL", "LOUD"),
        ("LOG_COLOR", "maybe"),
    ],
)
def test_out_of_range_values_are_rejected(valid_env, no_dotenv, name, value):
    valid_env.setenv(name, value)
    with pytest.raises(ConfigError) as exc_info:
        load_config(no_dotenv)
    assert name in exc_info.value.problems


def test_chunk_overlap_must_be_smaller_than_chunk_size(valid_env, no_dotenv):
    valid_env.setenv("CHUNK_SIZE", "500")
    valid_env.setenv("CHUNK_OVERLAP", "500")
    with pytest.raises(ConfigError) as exc_info:
        load_config(no_dotenv)
    assert "CHUNK_OVERLAP" in exc_info.value.problems


def test_dotenv_file_is_read_and_real_env_wins(clean_env, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        f"TELEGRAM_BOT_TOKEN={FAKE_TG_TOKEN}\n"
        "POSTGRES_DB=from_file\nPOSTGRES_USER=u\nPOSTGRES_PASSWORD=p\n"
        "LLM_API_KEYS=k1\nTOP_K=3\n",
        encoding="utf-8",
    )
    clean_env.setenv("TOP_K", "9")  # реальная переменная окружения важнее файла

    config = load_config(env_file)

    assert config.db.name == "from_file"
    assert config.rag.top_k == 9


def test_log_level_and_color_are_converted(valid_env, no_dotenv):
    valid_env.setenv("LOG_LEVEL", "debug")
    valid_env.setenv("LOG_COLOR", "0")
    config = load_config(no_dotenv)
    assert config.log.level == 10  # logging.DEBUG
    assert config.log.color is False


def test_new_llm_and_parsing_settings_have_defaults(valid_env, no_dotenv):
    config = load_config(no_dotenv)

    assert config.llm.key_quarantine_seconds == 3600
    assert config.llm.model_quarantine_seconds == 600
    assert config.llm.quota_cache_seconds == 30
    assert config.llm.daily_request_limit == 0
    assert config.rag.max_pdf_pages == 500
    assert config.rag.max_zip_entries == 2000
    assert config.rag.max_unpacked_mb == 100


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("LLM_KEY_QUARANTINE_SECONDS", "0"),
        ("LLM_DAILY_REQUEST_LIMIT", "-1"),
        ("MAX_PDF_PAGES", "0"),
        ("MAX_UNPACKED_MB", "abc"),
    ],
)
def test_new_settings_are_validated(valid_env, no_dotenv, monkeypatch, name, value):
    monkeypatch.setenv(name, value)

    with pytest.raises(ConfigError):
        load_config(no_dotenv)


def test_retrieval_embedding_and_timezone_defaults(valid_env, no_dotenv):
    config = load_config(no_dotenv)

    assert config.rag.embedding_query_prefix == "query:"
    assert config.rag.embedding_passage_prefix == "passage:"
    assert config.rag.embedding_batch_size == 16
    assert config.rag.candidate_factor == 4
    assert config.rag.max_context_chars == 6000
    assert config.llm.temperature == 0.1
    assert config.llm.max_tokens == 600
    assert config.tg_bot.display_timezone == "Europe/Kyiv"


def test_prefixes_are_stripped_and_may_be_empty(valid_env, no_dotenv, monkeypatch):
    monkeypatch.setenv("EMBEDDING_QUERY_PREFIX", "  ")
    monkeypatch.setenv("EMBEDDING_PASSAGE_PREFIX", "")

    config = load_config(no_dotenv)

    assert config.rag.embedding_query_prefix == ""
    assert config.rag.embedding_passage_prefix == ""


def test_unknown_timezone_is_a_config_error(valid_env, no_dotenv, monkeypatch):
    monkeypatch.setenv("DISPLAY_TIMEZONE", "Mars/Olympus")

    with pytest.raises(ConfigError) as exc_info:
        load_config(no_dotenv)

    assert "DISPLAY_TIMEZONE" in exc_info.value.problems


def test_relevance_margin_default_and_validation(valid_env, no_dotenv, monkeypatch):
    assert load_config(no_dotenv).rag.relevance_margin == 0.0

    monkeypatch.setenv("RELEVANCE_MARGIN", "0.05")
    assert load_config(no_dotenv).rag.relevance_margin == 0.05

    monkeypatch.setenv("RELEVANCE_MARGIN", "-0.1")
    with pytest.raises(ConfigError):
        load_config(no_dotenv)


def test_pair_failures_setting(valid_env, no_dotenv, monkeypatch):
    assert load_config(no_dotenv).llm.pair_failures_before_model_quarantine == 2

    monkeypatch.setenv("LLM_PAIR_FAILURES_BEFORE_MODEL_QUARANTINE", "0")
    with pytest.raises(ConfigError):
        load_config(no_dotenv)
