"""Конфигурация, .env.example, фикстуры тестов и docs/configuration.md не должны расходиться."""

import re
from pathlib import Path

from scripts.gen_config_docs import parse_env_example, render
from tests.conftest import CONFIG_ENV_NAMES

ROOT = Path(__file__).parents[1]
# Читаются только docker compose, а не config.py.
COMPOSE_ONLY = {"ARCHIVE_HOST_DIR", "ARCHIVE_CONTAINER_DIR"}


def _config_variables() -> set[str]:
    source = (ROOT / "config_data" / "config.py").read_text(encoding="utf-8")
    return set(re.findall(r'env\.\w+\(\s*"([A-Z][A-Z0-9_]*)"', source))


def _example_variables() -> set[str]:
    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    return {name for _, variables in parse_env_example(text) for name, _, _ in variables}


def test_every_variable_read_by_config_is_in_env_example():
    missing = _config_variables() - _example_variables()

    assert not missing, f"нет в .env.example: {sorted(missing)}"


def test_env_example_has_no_unknown_variables():
    extra = _example_variables() - _config_variables() - COMPOSE_ONLY

    assert not extra, f"в .env.example есть то, что config.py не читает: {sorted(extra)}"


def test_test_fixtures_clean_every_config_variable():
    """Иначе настройки разработчика просочатся в тесты и результат будет зависеть от машины."""
    missing = _config_variables() - set(CONFIG_ENV_NAMES)

    assert not missing, f"нет в CONFIG_ENV_NAMES (tests/conftest.py): {sorted(missing)}"


def test_configuration_docs_are_up_to_date():
    expected = render((ROOT / ".env.example").read_text(encoding="utf-8"))
    actual = (ROOT / "docs" / "configuration.md").read_text(encoding="utf-8")

    assert actual == expected, "выполните: python -m scripts.gen_config_docs"


def test_every_variable_is_documented():
    docs = (ROOT / "docs" / "configuration.md").read_text(encoding="utf-8")

    for name in _example_variables():
        assert f"`{name}`" in docs
