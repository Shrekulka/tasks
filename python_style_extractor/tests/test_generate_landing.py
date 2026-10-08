# python_style_extractor/tests/test_generate_landing.py

"""Тесты регулярного выражения роли `stat` из `generate_landing.py`."""

import re

import pytest

from scripts.generate_landing import STAT_REGEX


def test_stat_regex_is_compiled() -> None:
    """`STAT_REGEX` — либо `None` (правило отключено), либо скомпилированный `re.Pattern`."""
    assert STAT_REGEX is None or isinstance(STAT_REGEX, re.Pattern)


def test_stat_regex_matches_expected_values() -> None:
    """Проверяет контракт regex роли `stat`.

    Regex принимает `+17%` и `=17` и отвергает обычный текст. Примеры должны
    соответствовать паттерну `regex_patterns.stat` из `configs/semantic_rules.json`:
    при изменении паттерна тест обновляется вместе с ним. Тест пропускается, если
    правило отключено (паттерн не задан).
    """
    if STAT_REGEX is None:
        pytest.skip("stat regex disabled in semantic_rules.json")

    assert STAT_REGEX.fullmatch("+17%")
    assert STAT_REGEX.fullmatch("=17")
    assert not STAT_REGEX.fullmatch("Hello world")
