"""Язык ответа в промптах берётся из профиля, а не из кода."""

import json

import pytest

from config_data.config import ConfigError
from services.language import get_profile, load_profile
from services.prompts import PromptTexts


def _write_profile(directory, code, **overrides):
    raw = {
        "code": code,
        "scripts": ["LATIN"],
        "main_script": "LATIN",
        "answer_phrase": "in English",
        "min_script_share": 0.8,
        "own_letters": "",
        "foreign_letters": "",
        "own_words": ["the"],
        "foreign_words": [],
    }
    raw.update(overrides)
    path = directory / f"{code}.json"
    path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    return path


def test_default_profile_puts_its_phrase_into_both_prompts():
    texts = PromptTexts.for_profile(get_profile("uk"))

    assert "українською мовою" in texts.system
    assert "українською мовою" in texts.strict_reminder
    assert "{" not in texts.system.replace('{"found"', "").replace("}", "")  # плейсхолдеры закрыты


def test_new_language_needs_only_a_json_file(tmp_path):
    _write_profile(tmp_path, "en")

    texts = PromptTexts.for_profile(get_profile("en", tmp_path))

    assert "in English" in texts.system
    assert "українською" not in texts.system
    assert "українською" not in texts.strict_reminder


def test_prompt_version_changes_with_language(tmp_path):
    _write_profile(tmp_path, "en")

    uk = PromptTexts.for_profile(get_profile("uk"))
    en = PromptTexts.for_profile(get_profile("en", tmp_path))

    assert uk.version != en.version  # кэш ответов не смешивает языки


def test_profile_without_answer_phrase_is_a_config_error(tmp_path):
    path = _write_profile(tmp_path, "xx")
    raw = json.loads(path.read_text(encoding="utf-8"))
    del raw["answer_phrase"]
    path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(ConfigError):
        load_profile(path)


def test_prompt_files_hold_the_text_not_the_code(tmp_path):
    (tmp_path / "system.txt").write_text('Answer $answer_phrase. {"found": true}', "utf-8")
    (tmp_path / "strict_reminder.txt").write_text("\nStrict: $answer_phrase.", "utf-8")
    _write_profile(tmp_path, "en")

    texts = PromptTexts.for_profile(get_profile("en", tmp_path), tmp_path)

    assert texts.system == 'Answer in English. {"found": true}'  # скобки JSON не ломают шаблон
    assert texts.strict_reminder == "\nStrict: in English."


def test_language_specific_prompt_overrides_the_shared_one(tmp_path):
    (tmp_path / "system.txt").write_text("shared $answer_phrase", "utf-8")
    (tmp_path / "strict_reminder.txt").write_text("strict $answer_phrase", "utf-8")
    (tmp_path / "en").mkdir()
    (tmp_path / "en" / "system.txt").write_text("english only $answer_phrase", "utf-8")
    _write_profile(tmp_path, "en")

    texts = PromptTexts.for_profile(get_profile("en", tmp_path), tmp_path)

    assert texts.system == "english only in English"
    assert texts.strict_reminder == "strict in English"  # нет своего файла: берётся общий


def test_editing_a_prompt_file_changes_the_version_and_so_the_cache_key(tmp_path):
    (tmp_path / "strict_reminder.txt").write_text("strict", "utf-8")
    (tmp_path / "system.txt").write_text("one", "utf-8")
    profile = _write_profile(tmp_path, "en")
    first = PromptTexts.for_profile(load_profile(profile), tmp_path).version

    (tmp_path / "system.txt").write_text("two", "utf-8")
    second = PromptTexts.for_profile(load_profile(profile), tmp_path).version

    assert first != second


def test_missing_or_broken_prompt_file_is_a_config_error(tmp_path):
    profile = load_profile(_write_profile(tmp_path, "en"))
    with pytest.raises(ConfigError):
        PromptTexts.for_profile(profile, tmp_path)  # файлов нет

    (tmp_path / "system.txt").write_text("bad $unknown_placeholder", "utf-8")
    (tmp_path / "strict_reminder.txt").write_text("ok", "utf-8")
    with pytest.raises(ConfigError):
        PromptTexts.for_profile(profile, tmp_path)


def test_shipped_prompts_render_without_leftover_placeholders():
    texts = PromptTexts.for_profile(get_profile("uk"))

    assert "$" not in texts.system and "$" not in texts.strict_reminder
    assert "українською мовою" in texts.system
    assert '{"found": true або false' in texts.system  # JSON-формат ответа на месте
