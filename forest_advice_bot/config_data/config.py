"""Конфигурация приложения.

Идея простая: переменные окружения (и файл `.env`, если он есть) читаются ОДИН раз при
старте и складываются в неизменяемые датаклассы. Дальше весь код получает готовый объект
`Config` и ничего не знает про `os.environ`.

Важные свойства:
  * ничего не читается при импорте модуля, конфиг создаётся явным вызовом `load_config()`;
  * все ошибки собираются сразу и показываются одним сообщением (`ConfigError`);
  * секреты (токен, пароль, ключи) не попадают в `repr()`, а значит и в логи;
  * у каждой секции свой загрузчик: скрипту, которому нужен только LLM, не нужен токен Telegram.
"""

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from environs import Env, EnvValidationError
from marshmallow.validate import Range, Regexp

DEFAULT_ENV_FILE = ".env"
JSON_MODES = ("auto", "off")
INVALID_STRATEGIES = ("strict_retry", "next_model")
LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
TELEGRAM_FILE_LIMIT_MB = 20  # Telegram Bot API не отдаёт боту файлы больше 20 МБ
_TELEGRAM_TOKEN_RE = r"^\d{6,}:[A-Za-z0-9_-]{30,}$"


class ConfigError(Exception):
    """Конфигурация неверна. В тексте перечислены все проблемные переменные сразу."""

    def __init__(self, problems: dict[str, list[str]]) -> None:
        self.problems = problems
        lines = ["Ошибка конфигурации. Проверьте переменные окружения (файл .env):"]
        for name, messages in problems.items():
            lines.append(f"  - {name}: {'; '.join(messages)}")
        super().__init__("\n".join(lines))


# ----------------------------------------------------------------------------------------
# Секции конфигурации. frozen=True: после создания менять нельзя (меньше сюрпризов).
# field(repr=False): значение не показывается в repr(), поэтому секрет не утечёт в лог.
# ----------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TgBotConfig:
    token: str = field(repr=False)
    display_timezone: str  # часовой пояс для дат в ответах (IANA, например Europe/Kyiv)


@dataclass(frozen=True, slots=True)
class DbConfig:
    host: str
    port: int
    name: str
    user: str
    password: str = field(repr=False)

    @property
    def url(self) -> str:
        """URL для SQLAlchemy (драйвер asyncpg). Логин и пароль экранируются:
        пароль вида `p@ss:w/rd` без этого сломал бы разбор адреса."""
        user = quote(self.user, safe="")
        password = quote(self.password, safe="")
        return f"postgresql+asyncpg://{user}:{password}@{self.host}:{self.port}/{self.name}"


@dataclass(frozen=True, slots=True)
class LLMConfig:
    """Настройки LLM-провайдера. Конкретный провайдер выбирается адаптером по `provider`."""

    provider: str
    api_keys: tuple[str, ...] = field(repr=False)
    models: tuple[str, ...]  # приоритетные модели (пробуются первыми)
    models_blocklist: tuple[str, ...]
    base_url: str  # пусто = адрес по умолчанию из адаптера
    timeout_seconds: float
    deadline_seconds: float
    max_models_tried: int
    max_rpm: int
    min_context: int
    catalog_ttl_seconds: int
    rate_pause_seconds: float
    account_pause_seconds: float
    key_quarantine_seconds: float  # на сколько отключаем неверный или исчерпанный ключ
    model_quarantine_seconds: float  # на сколько отключаем модель или пару «модель + ключ»
    quota_cache_seconds: float  # как долго верим закэшированному остатку дневной квоты
    daily_request_limit: int  # запасной локальный лимит в сутки (0 = выключен)
    pair_failures_before_model_quarantine: (
        int  # сколько ключей должны отказать, чтобы отключить модель
    )
    temperature: float  # температура генерации
    max_tokens: int  # потолок длины ответа модели
    max_limited_attempts: int  # сколько ответов 429 от поставщика терпим, затем общая пауза (D30)
    limit_pause_seconds: float  # длина общей паузы после исчерпания попыток (0 = без паузы)
    invalid_response_strategy: str  # strict_retry (ТЗ §12) или next_model
    upstream_quarantine_seconds: float  # на сколько отключаем модель после 429 поставщика
    count_failed_requests: bool  # считать ли в локальную квоту таймауты, 429 и 5xx
    daily_reset_threshold_seconds: float  # пауза не короче этой считается дневным лимитом
    json_mode: str  # auto: JSON-режим, если модель его поддерживает; off: не использовать


@dataclass(frozen=True, slots=True)
class RagConfig:
    embedding_model: str
    embedding_dim: int
    embedding_query_prefix: str  # префикс вопроса для моделей семейства e5 (пусто, если не нужен)
    embedding_passage_prefix: str  # префикс фрагмента документа
    embedding_batch_size: int
    top_k: int
    relevance_threshold: float  # максимальное косинусное расстояние, при котором фрагмент годится
    relevance_margin: float  # доп. отсев: не дальше лучшего фрагмента + margin (0 = выключено)
    chunk_size: int
    chunk_overlap: int
    archive_dir: Path
    max_file_mb: int
    max_pdf_pages: int  # защита от гигантских PDF
    max_zip_entries: int  # защита DOCX от архивной бомбы: число записей в ZIP
    max_unpacked_mb: int  # защита DOCX от архивной бомбы: размер после распаковки
    candidate_factor: int  # во сколько раз больше кандидатов берём из БД, чем top_k
    max_context_chars: int  # потолок суммарного размера фрагментов в промпте
    processing_notice_after_seconds: float  # через сколько секунд индексации писать «обробляю»
    large_document_chars: int  # от скольких символов документ «большой» (0 = выкл.)
    cache_ttl_seconds: float  # время жизни кэша ответов, 0 = кэш выключен
    cache_max_entries: int  # сколько ответов держать в кэше
    answer_language: str  # код языка ответов (профиль в services/language.py)
    language_profiles_dir: Path  # каталог JSON-профилей языков ответа
    prompts_dir: Path  # каталог текстов промптов (system.txt, strict_reminder.txt)
    language_marker_min_chars: int  # с какой длины в ответе обязательны буквы данного языка


@dataclass(frozen=True, slots=True)
class LimitsConfig:
    uploads_per_hour: int
    questions_per_minute: int
    max_question_chars: int
    mydocs_page_size: int  # сколько документов на одной странице /mydocs
    shutdown_timeout_seconds: float  # сколько ждать фоновые задачи при остановке


@dataclass(frozen=True, slots=True)
class LogConfig:
    level: int
    color: bool | None  # None = автоопределение (цвет только в терминале)
    to_file: bool
    dir: str


@dataclass(frozen=True, slots=True)
class Config:
    tg_bot: TgBotConfig
    db: DbConfig
    llm: LLMConfig
    rag: RagConfig
    limits: LimitsConfig
    log: LogConfig

    def summary(self) -> str:
        """Безопасное описание для лога при старте: без токенов, паролей и ключей."""
        return (
            f"db={self.db.user}@{self.db.host}:{self.db.port}/{self.db.name}; "
            f"llm: keys={len(self.llm.api_keys)}, pinned_models={len(self.llm.models)}, "
            f"deadline={self.llm.deadline_seconds}s, max_rpm={self.llm.max_rpm}; "
            f"rag: model={self.rag.embedding_model}, top_k={self.rag.top_k}, "
            f"threshold={self.rag.relevance_threshold}, archive_dir={self.rag.archive_dir}; "
            f"limits: uploads/h={self.limits.uploads_per_hour}, "
            f"questions/min={self.limits.questions_per_minute}"
        )


# ----------------------------------------------------------------------------------------
# Вспомогательные функции
# ----------------------------------------------------------------------------------------


def parse_csv(raw: str) -> tuple[str, ...]:
    """'a, b ,,a' -> ('a', 'b'): убираем пробелы, кавычки, пустые значения и повторы."""
    seen: dict[str, None] = {}
    for item in raw.split(","):
        value = item.strip().strip("'\"").strip()
        if value:
            seen.setdefault(value, None)
    return tuple(seen)


def _open_env(env_path: str | Path | None) -> Env:
    """Создаёт читатель окружения.

    eager=False: ошибки не бросаются на первой же переменной, а копятся, и `_seal` покажет
    их все сразу. recurse=False: `.env` ищется только по указанному пути (по умолчанию
    в текущей папке), а не выше по дереву каталогов, где может лежать чужой файл.
    Реальные переменные окружения важнее содержимого `.env`.
    """
    env = Env(eager=False)
    env.read_env(str(env_path or DEFAULT_ENV_FILE), recurse=False)
    return env


def _seal(env: Env) -> None:
    """Проверяет всё, что было прочитано, и бросает ConfigError со списком проблем."""
    try:
        env.seal()
    except EnvValidationError as exc:
        problems = {
            name: [_translate(message) for message in messages]
            for name, messages in exc.error_messages.items()
        }
        raise ConfigError(problems) from None


def _translate(message: str) -> str:
    if message == "Environment variable not set.":
        return "переменная не задана"
    return message


# ----------------------------------------------------------------------------------------
# Загрузчики секций
# ----------------------------------------------------------------------------------------


def load_tg_config(env_path: str | Path | None = None) -> TgBotConfig:
    env = _open_env(env_path)
    token = env.str(
        "TELEGRAM_BOT_TOKEN",
        validate=Regexp(
            _TELEGRAM_TOKEN_RE,
            error="не похоже на токен Telegram (вид 123456789:AAAA..., выдаёт @BotFather)",
        ),
    )
    timezone = env.str("DISPLAY_TIMEZONE", "Europe/Kyiv")
    _seal(env)
    try:
        ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError):
        raise ConfigError(
            {"DISPLAY_TIMEZONE": [f"неизвестный часовой пояс «{timezone}»"]}
        ) from None
    return TgBotConfig(token=token, display_timezone=timezone)


def load_db_config(env_path: str | Path | None = None) -> DbConfig:
    env = _open_env(env_path)
    host = env.str("POSTGRES_HOST", "localhost")
    port = env.int("POSTGRES_PORT", 5432, validate=Range(min=1, max=65535))
    name = env.str("POSTGRES_DB")
    user = env.str("POSTGRES_USER")
    password = env.str("POSTGRES_PASSWORD")
    _seal(env)
    return DbConfig(host=host, port=port, name=name, user=user, password=password)


def load_llm_config(env_path: str | Path | None = None) -> LLMConfig:
    env = _open_env(env_path)
    provider = env.str("LLM_PROVIDER", "openrouter").strip().lower()
    raw_keys = env.str("LLM_API_KEYS", "")
    raw_single_key = env.str("LLM_API_KEY", "")
    raw_models = env.str("LLM_MODELS", "")
    raw_blocklist = env.str("LLM_MODELS_BLOCKLIST", "")
    base_url = env.str("LLM_BASE_URL", "")
    timeout = env.float("LLM_TIMEOUT_SECONDS", 20.0, validate=Range(min=1, max=120))
    deadline = env.float("LLM_DEADLINE_SECONDS", 45.0, validate=Range(min=1, max=300))
    max_models = env.int("LLM_MAX_MODELS_TRIED", 3, validate=Range(min=1, max=20))
    max_rpm = env.int("LLM_MAX_RPM", 15, validate=Range(min=1, max=600))
    min_context = env.int("LLM_MIN_CONTEXT", 8000, validate=Range(min=1000, max=2_000_000))
    catalog_ttl = env.int("LLM_CATALOG_TTL_SECONDS", 3600, validate=Range(min=60, max=86_400))
    rate_pause = env.float("LLM_RATE_PAUSE_SECONDS", 60.0, validate=Range(min=1, max=3600))
    account_pause = env.float("LLM_ACCOUNT_PAUSE_SECONDS", 900.0, validate=Range(min=1, max=86_400))
    key_quarantine = env.float(
        "LLM_KEY_QUARANTINE_SECONDS", 3600.0, validate=Range(min=1, max=86_400)
    )
    model_quarantine = env.float(
        "LLM_MODEL_QUARANTINE_SECONDS", 600.0, validate=Range(min=1, max=86_400)
    )
    quota_cache = env.float("LLM_QUOTA_CACHE_SECONDS", 30.0, validate=Range(min=0, max=3600))
    daily_limit = env.int("LLM_DAILY_REQUEST_LIMIT", 0, validate=Range(min=0, max=1_000_000))
    pair_failures = env.int(
        "LLM_PAIR_FAILURES_BEFORE_MODEL_QUARANTINE", 2, validate=Range(min=1, max=20)
    )
    temperature = env.float("LLM_TEMPERATURE", 0.1, validate=Range(min=0.0, max=2.0))
    max_tokens = env.int("LLM_MAX_TOKENS", 600, validate=Range(min=16, max=8000))
    max_limited = env.int("LLM_MAX_LIMITED_ATTEMPTS", 2, validate=Range(min=1, max=20))
    limit_pause = env.float("LLM_LIMIT_PAUSE_SECONDS", 60.0, validate=Range(min=0, max=3600))
    json_mode = env.str("LLM_JSON_MODE", "auto").strip().lower()
    invalid_strategy = env.str("LLM_INVALID_RESPONSE_STRATEGY", "strict_retry").strip().lower()
    upstream_quarantine = env.float(
        "LLM_UPSTREAM_QUARANTINE_SECONDS", 60.0, validate=Range(min=0, max=86_400)
    )
    count_failed = env.bool("LLM_COUNT_FAILED_REQUESTS", True)
    daily_threshold = env.float(
        "LLM_DAILY_RESET_THRESHOLD_SECONDS", 3600.0, validate=Range(min=60, max=86_400)
    )
    _seal(env)

    keys = parse_csv(raw_keys) or parse_csv(raw_single_key)
    problems: dict[str, list[str]] = {}
    if not keys:
        problems["LLM_API_KEYS"] = [
            "не задан ни один ключ (LLM_API_KEYS=key1,key2 или LLM_API_KEY=key)"
        ]
    if deadline < timeout:
        problems["LLM_DEADLINE_SECONDS"] = ["должен быть не меньше LLM_TIMEOUT_SECONDS"]
    if json_mode not in JSON_MODES:
        problems["LLM_JSON_MODE"] = [f"допустимо: {', '.join(JSON_MODES)}"]
    if invalid_strategy not in INVALID_STRATEGIES:
        problems["LLM_INVALID_RESPONSE_STRATEGY"] = [f"допустимо: {', '.join(INVALID_STRATEGIES)}"]
    if base_url and not re.match(r"^https?://", base_url):
        problems["LLM_BASE_URL"] = ["должен начинаться с http:// или https://"]
    if problems:
        raise ConfigError(problems)

    return LLMConfig(
        provider=provider,
        api_keys=keys,
        models=parse_csv(raw_models),
        models_blocklist=parse_csv(raw_blocklist),
        base_url=base_url.rstrip("/"),
        timeout_seconds=timeout,
        deadline_seconds=deadline,
        max_models_tried=max_models,
        max_rpm=max_rpm,
        min_context=min_context,
        catalog_ttl_seconds=catalog_ttl,
        rate_pause_seconds=rate_pause,
        account_pause_seconds=account_pause,
        key_quarantine_seconds=key_quarantine,
        model_quarantine_seconds=model_quarantine,
        quota_cache_seconds=quota_cache,
        daily_request_limit=daily_limit,
        pair_failures_before_model_quarantine=pair_failures,
        temperature=temperature,
        max_tokens=max_tokens,
        max_limited_attempts=max_limited,
        limit_pause_seconds=limit_pause,
        invalid_response_strategy=invalid_strategy,
        upstream_quarantine_seconds=upstream_quarantine,
        count_failed_requests=count_failed,
        daily_reset_threshold_seconds=daily_threshold,
        json_mode=json_mode,
    )


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _project_path(value: str) -> Path:
    """Относительный путь считается от корня проекта, а не от текущего каталога запуска."""
    path = Path(value)
    return path if path.is_absolute() else PROJECT_ROOT / path


def load_rag_config(env_path: str | Path | None = None) -> RagConfig:
    env = _open_env(env_path)
    embedding_model = env.str("EMBEDDING_MODEL", "intfloat/multilingual-e5-base")
    embedding_dim = env.int("EMBEDDING_DIM", 768, validate=Range(min=1, max=4096))
    query_prefix = env.str("EMBEDDING_QUERY_PREFIX", "query:")
    passage_prefix = env.str("EMBEDDING_PASSAGE_PREFIX", "passage:")
    batch_size = env.int("EMBEDDING_BATCH_SIZE", 16, validate=Range(min=1, max=256))
    top_k = env.int("TOP_K", 5, validate=Range(min=1, max=20))
    threshold = env.float("RELEVANCE_THRESHOLD", 0.25, validate=Range(min=0.01, max=1.0))
    margin = env.float("RELEVANCE_MARGIN", 0.0, validate=Range(min=0.0, max=1.0))
    chunk_size = env.int("CHUNK_SIZE", 1000, validate=Range(min=200, max=4000))
    chunk_overlap = env.int("CHUNK_OVERLAP", 200, validate=Range(min=0, max=1000))
    archive_dir = env.str("ARCHIVE_DIR", "./samples/archive")
    max_file_mb = env.int(
        "MAX_FILE_MB", TELEGRAM_FILE_LIMIT_MB, validate=Range(min=1, max=TELEGRAM_FILE_LIMIT_MB)
    )
    max_pdf_pages = env.int("MAX_PDF_PAGES", 500, validate=Range(min=1, max=100_000))
    max_zip_entries = env.int("MAX_ZIP_ENTRIES", 2000, validate=Range(min=1, max=1_000_000))
    max_unpacked_mb = env.int("MAX_UNPACKED_MB", 100, validate=Range(min=1, max=10_000))
    candidate_factor = env.int("RETRIEVAL_CANDIDATE_FACTOR", 4, validate=Range(min=1, max=50))
    max_context_chars = env.int("MAX_CONTEXT_CHARS", 6000, validate=Range(min=500, max=100_000))
    notice_after = env.float("PROCESSING_NOTICE_AFTER_SECONDS", 5.0, validate=Range(min=0, max=600))
    large_document_chars = env.int(
        "LARGE_DOCUMENT_CHARS", 10_000, validate=Range(min=0, max=10_000_000)
    )
    cache_ttl = env.float("ANSWER_CACHE_TTL_SECONDS", 3600.0, validate=Range(min=0, max=86_400))
    cache_max = env.int("ANSWER_CACHE_MAX_ENTRIES", 500, validate=Range(min=1, max=100_000))
    answer_language = env.str("ANSWER_LANGUAGE", "uk").strip().lower()
    language_dir = _project_path(env.str("LANGUAGE_PROFILES_DIR", "data/languages"))
    prompts_dir = _project_path(env.str("PROMPTS_DIR", "data/prompts"))
    language_min_chars = env.int("LANGUAGE_MARKER_MIN_CHARS", 60, validate=Range(min=1, max=1000))
    _seal(env)

    if chunk_overlap >= chunk_size:
        raise ConfigError({"CHUNK_OVERLAP": ["должен быть меньше CHUNK_SIZE"]})

    return RagConfig(
        embedding_model=embedding_model,
        embedding_dim=embedding_dim,
        embedding_query_prefix=query_prefix.strip(),
        embedding_passage_prefix=passage_prefix.strip(),
        embedding_batch_size=batch_size,
        top_k=top_k,
        relevance_threshold=threshold,
        relevance_margin=margin,
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
        archive_dir=Path(archive_dir),
        max_file_mb=max_file_mb,
        max_pdf_pages=max_pdf_pages,
        max_zip_entries=max_zip_entries,
        max_unpacked_mb=max_unpacked_mb,
        candidate_factor=candidate_factor,
        max_context_chars=max_context_chars,
        processing_notice_after_seconds=notice_after,
        large_document_chars=large_document_chars,
        cache_ttl_seconds=cache_ttl,
        cache_max_entries=cache_max,
        answer_language=answer_language,
        language_profiles_dir=language_dir,
        prompts_dir=prompts_dir,
        language_marker_min_chars=language_min_chars,
    )


def load_limits_config(env_path: str | Path | None = None) -> LimitsConfig:
    env = _open_env(env_path)
    uploads = env.int("UPLOADS_PER_HOUR", 10, validate=Range(min=1, max=1000))
    questions = env.int("QUESTIONS_PER_MINUTE", 6, validate=Range(min=1, max=600))
    max_chars = env.int("MAX_QUESTION_CHARS", 1000, validate=Range(min=50, max=4000))
    page_size = env.int("MYDOCS_PAGE_SIZE", 5, validate=Range(min=1, max=20))
    shutdown_timeout = env.float("SHUTDOWN_TIMEOUT_SECONDS", 30.0, validate=Range(min=1, max=600))
    _seal(env)
    return LimitsConfig(
        uploads_per_hour=uploads,
        questions_per_minute=questions,
        max_question_chars=max_chars,
        mydocs_page_size=page_size,
        shutdown_timeout_seconds=shutdown_timeout,
    )


def load_log_config(env_path: str | Path | None = None) -> LogConfig:
    env = _open_env(env_path)
    level_name = env.str("LOG_LEVEL", "INFO").upper()
    color = env.str("LOG_COLOR", "auto").lower()
    to_file = env.bool("LOG_TO_FILE", False)
    log_dir = env.str("LOG_DIR", "logs")
    _seal(env)

    problems: dict[str, list[str]] = {}
    if level_name not in LOG_LEVELS:
        problems["LOG_LEVEL"] = [f"допустимо: {', '.join(LOG_LEVELS)}"]
    if not re.fullmatch(r"auto|0|1", color):
        problems["LOG_COLOR"] = ["допустимо: auto, 1, 0"]
    if problems:
        raise ConfigError(problems)

    return LogConfig(
        level=getattr(logging, level_name),
        color=None if color == "auto" else color == "1",
        to_file=to_file,
        dir=log_dir,
    )


def load_config(env_path: str | Path | None = None) -> Config:
    """Загружает всю конфигурацию. Если что-то не так, ошибки всех секций приходят вместе."""
    loaders = {
        "tg_bot": load_tg_config,
        "db": load_db_config,
        "llm": load_llm_config,
        "rag": load_rag_config,
        "limits": load_limits_config,
        "log": load_log_config,
    }
    parts: dict[str, object] = {}
    problems: dict[str, list[str]] = {}
    for section, loader in loaders.items():
        try:
            parts[section] = loader(env_path)
        except ConfigError as exc:
            problems.update(exc.problems)
    if problems:
        raise ConfigError(problems)
    return Config(**parts)  # type: ignore[arg-type]
