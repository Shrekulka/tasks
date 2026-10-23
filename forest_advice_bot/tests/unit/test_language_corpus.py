"""Метрики языкового фильтра на размеченном корпусе (tests/eval/language_cases.json).

Фильтр эвристический, поэтому тест фиксирует измеренные ошибки, а не «идеал»:
ложные отказы (хороший украинский отклонён) и ложные допуски (русский пропущен).
Пополняйте корпус реальными ответами моделей и пересматривайте пороги ниже.
"""

import json
from pathlib import Path

import pytest

from services.language import LanguageGuard, get_profile

CASES = json.loads(
    (Path(__file__).parent.parent / "eval" / "language_cases.json").read_text("utf-8")
)
MAX_FALSE_ACCEPT_RATE = 0.30  # русские фразы без характерных слов фильтр не поймает (см. README)


@pytest.fixture(scope="module")
def guard():
    return LanguageGuard(get_profile("uk"), marker_min_chars=60)


def _rates(guard):
    uk = [c for c in CASES if c["lang"] == "uk"]
    ru = [c for c in CASES if c["lang"] == "ru"]
    false_reject = [c["text"] for c in uk if not guard.looks_right(c["text"])]
    false_accept = [c["text"] for c in ru if guard.looks_right(c["text"])]
    return false_reject, len(uk), false_accept, len(ru)


def test_no_false_rejects_of_correct_ukrainian(guard):
    false_reject, total, _, _ = _rates(guard)

    assert false_reject == [], f"{len(false_reject)}/{total} украинских ответов отклонено"


def test_false_accept_rate_of_russian_stays_below_threshold(guard):
    _, _, false_accept, total = _rates(guard)

    assert len(false_accept) / total <= MAX_FALSE_ACCEPT_RATE, false_accept


def test_mixed_cases_marked_as_solid_are_classified_as_expected(guard):
    solid = [c for c in CASES if c["lang"] == "mixed" and not c.get("known_gap")]

    for case in solid:
        assert guard.looks_right(case["text"]) is case["expect_accept"], case["text"]


def test_rag_config_gives_a_real_path_for_profiles_dir_by_default(valid_env, no_dotenv):
    from config_data.config import load_rag_config

    rag = load_rag_config(no_dotenv)

    assert isinstance(rag.language_profiles_dir, Path)
    assert (rag.language_profiles_dir / "uk.json").is_file()
    assert get_profile(rag.answer_language, rag.language_profiles_dir).code == "uk"
