"""Тесты правок по ревью: язык ответа, не блокирующий эмбеддер, small talk, лимитеры."""

import asyncio

import pytest

from config_data.config import ConfigError
from external_services.llm.free_filter import parse_free_model
from external_services.llm.openrouter import build_free_policy
from services.embeddings import SentenceTransformerEmbedder
from services.intent import SmallTalkDetector
from services.language import LanguageGuard, get_profile
from utils.utils import SlidingWindowLimiter


@pytest.fixture
def guard():
    return LanguageGuard(get_profile("uk"), marker_min_chars=60)


@pytest.mark.parametrize(
    "text",
    [
        "Не бегите от волка, стойте спокойно.",  # русский без ы э ъ ё
        "Ховайте орехи в разных местах, чтобы не потерять запас.",
        "Hello, hide the nuts in several places.",
    ],
)
def test_guard_rejects_non_ukrainian(guard, text):
    assert guard.looks_right(text) is False


@pytest.mark.parametrize(
    "text",
    ["Не тікай від вовка, стій спокійно.", "Ховай горіхи в різних місцях, щоб не втратити запас."],
)
def test_guard_accepts_ukrainian(guard, text):
    assert guard.looks_right(text) is True


def test_unknown_language_profile_is_a_config_error():
    with pytest.raises(ConfigError):
        get_profile("xx")


class _LoggingModel:
    """Пишет порядок вызовов encode: тест не зависит от скорости машины."""

    def __init__(self):
        self.calls: list[list[str]] = []

    def get_sentence_embedding_dimension(self):
        return 2

    def encode(self, texts, batch_size, normalize_embeddings, show_progress_bar):
        self.calls.append(list(texts))
        return [[1.0, 0.0]] * len(texts)


async def test_query_is_served_between_document_batches_not_after_all_of_them():
    model = _LoggingModel()
    embedder = SentenceTransformerEmbedder(
        model, model_name="m", query_prefix="q:", passage_prefix="p:", batch_size=16
    )
    big = asyncio.create_task(embedder.embed_passages(["t"] * 160))  # 10 пачек
    await asyncio.sleep(0)  # документ занял первую пачку и ждёт lock
    await embedder.embed_query("вопрос")
    await big

    query_position = next(i for i, call in enumerate(model.calls) if call == ["q: вопрос"])
    assert len(model.calls) == 11
    assert query_position < len(model.calls) - 1  # вопрос не в хвосте очереди
    assert query_position <= 2  # встал сразу за текущей пачкой


def test_small_talk_matches_whole_message_only():
    detector = SmallTalkDetector(["привіт", "дякую"])

    assert detector.matches("Привіт!")
    assert detector.matches("  ДЯКУЮ  ")
    assert not detector.matches("Привіт, де ховати горіхи?")


def test_limiter_reports_idle_after_window():
    now = [0.0]
    limiter = SlidingWindowLimiter(1, 10.0, lambda: now[0])
    limiter.try_acquire()
    assert limiter.is_idle() is False

    now[0] = 11.0

    assert limiter.is_idle() is True


def _entry(pricing):
    return {
        "id": "v/m:free",
        "pricing": pricing,
        "architecture": {"input_modalities": ["text"], "output_modalities": ["text"]},
        "context_length": 32000,
    }


@pytest.mark.parametrize(
    ("pricing", "accepted"),
    [
        ({"prompt": "0", "completion": "0", "request": None, "image": ""}, True),
        ({"prompt": "0", "completion": "0", "web_search": "0.01"}, False),
        ({"prompt": None, "completion": "0"}, False),
    ],
)
def test_free_filter_tolerates_null_optional_prices_but_never_money(pricing, accepted):
    policy = build_free_policy(8000)

    assert (parse_free_model(_entry(pricing), policy) is not None) is accepted


def test_idle_limiters_are_evicted_and_memory_stays_bounded():
    from middlewares import throttling

    now = [0.0]
    middleware = throttling.ThrottlingMiddleware.__new__(throttling.ThrottlingMiddleware)
    middleware._limiters = {}
    for user in range(2000):
        limiter = SlidingWindowLimiter(1, 10.0, lambda: now[0])
        limiter.try_acquire()
        middleware._limiters[(user, "question")] = limiter
    now[0] = 11.0  # окно истекло у всех

    middleware._evict_idle()

    assert middleware._limiters == {}
