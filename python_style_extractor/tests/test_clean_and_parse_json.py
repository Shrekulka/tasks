# python_style_extractor/tests/test_clean_and_parse_json.py

"""Тесты разбора ответа LLM в `SemanticMap` (`clean_and_parse_json`)."""

import json

import pytest

from scripts.generate_landing import clean_and_parse_json

# Пример ответа модели: плоский словарь `{node_id: {...}}` без обёртки `decisions`.
REAL_FAILING_CASE_1 = json.dumps({
    "1122:6773": {"text": "Create and share your Affiliate link",
                  "semantic_role": "body", "html_tag": "p"},
    "1122:6781": {"text": "Sign up to become an Affiliate",
                  "semantic_role": "heading_h2", "html_tag": "h2"},
})


def test_flat_dict_repair() -> None:
    """Плоский словарь без обёртки `decisions` приводится к `SemanticMap`."""
    result = clean_and_parse_json(REAL_FAILING_CASE_1)
    ids = {d.id for d in result.decisions}
    assert ids == {"1122:6773", "1122:6781"}


def test_proper_decisions_wrapper() -> None:
    """Корректный ответ с обёрткой `decisions` разбирается как есть."""
    payload = json.dumps({
        "decisions": [
            {"id": "a", "semantic_role": "body", "html_tag": "p", "confidence": 0.9}
        ]
    })
    result = clean_and_parse_json(payload)
    assert result.decisions[0].id == "a"


def test_bare_array_without_wrapper() -> None:
    """Голый массив решений без обёртки `decisions` разбирается."""
    payload = json.dumps([
        {"id": "x", "semantic_role": "caption", "html_tag": "span"}
    ])
    result = clean_and_parse_json(payload)
    assert result.decisions[0].id == "x"


def test_markdown_fenced_response() -> None:
    """Ответ, обёрнутый в markdown-блок кода, разбирается."""
    payload = "```json\n" + json.dumps({
        "decisions": [{"id": "y", "semantic_role": "stat", "html_tag": "span"}]
    }) + "\n```"
    result = clean_and_parse_json(payload)
    assert result.decisions[0].id == "y"


def test_role_alias_field_name() -> None:
    """Альтернативные имена полей (`role`, `tag`) распознаются."""
    payload = json.dumps({"n1": {"role": "body", "tag": "p"}})
    result = clean_and_parse_json(payload)
    assert result.decisions[0].semantic_role == "body"


def test_unrecognizable_format_raises_value_error() -> None:
    """Нераспознаваемый формат даёт `ValueError` с текстом «Could not parse»."""
    payload = json.dumps({"n1": {"foo": "bar"}})
    with pytest.raises(ValueError, match="Could not parse"):
        clean_and_parse_json(payload)
