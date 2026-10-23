"""Тесты логгера: что реально попадает в консоль и файл."""

import logging

from logger_config import redact, setup_logging
from tests.conftest import FAKE_GROQ_KEY, FAKE_LLM_KEY, FAKE_TG_TOKEN


def test_redact_masks_telegram_token_llm_keys_and_bearer():
    text = (
        f"url=https://api.telegram.org/bot{FAKE_TG_TOKEN}/getMe "
        f"key={FAKE_LLM_KEY} old={FAKE_GROQ_KEY} header=Authorization: Bearer abcdef1234567890xyz"
    )
    cleaned = redact(text)

    assert FAKE_TG_TOKEN not in cleaned
    assert FAKE_LLM_KEY not in cleaned
    assert FAKE_GROQ_KEY not in cleaned
    assert "abcdef1234567890xyz" not in cleaned
    assert "<telegram-token>" in cleaned and "<openrouter-key>" in cleaned
    assert "<groq-key>" in cleaned


def test_redact_leaves_ordinary_text_alone():
    text = "Документ принят: 12:30, версия 2, размер 15 МБ"
    assert redact(text) == text


def test_console_output_has_class_and_method_without_colors(capsys):
    setup_logging(level=logging.DEBUG, color=False)

    class Greeter:
        def say(self) -> None:
            logging.getLogger("tests.greeter").info("Привет, лес")

    Greeter().say()
    out = capsys.readouterr().out

    assert "Greeter.say()" in out
    assert "Привет, лес" in out
    assert "\x1b[" not in out  # цветовых кодов нет


def test_function_call_outside_class_gets_dash(capsys):
    setup_logging(level=logging.DEBUG, color=False)
    logging.getLogger("tests.func").info("из функции")
    assert "-.test_function_call_outside_class_gets_dash()" in capsys.readouterr().out


def test_color_output_contains_ansi_codes(capsys):
    setup_logging(level=logging.DEBUG, color=True)
    logging.getLogger("tests.color").warning("внимание")
    assert "\x1b[" in capsys.readouterr().out


def test_extra_fields_are_rendered(capsys):
    setup_logging(level=logging.INFO, color=False)
    logging.getLogger("tests.extra").info("Заказ", extra={"order_id": 42})
    assert "[order_id=42]" in capsys.readouterr().out


def test_level_filtering(capsys):
    setup_logging(level=logging.WARNING, color=False)
    log = logging.getLogger("tests.level")
    log.info("тихо")
    log.error("громко")
    out = capsys.readouterr().out
    assert "тихо" not in out and "громко" in out


def test_secrets_are_masked_in_console_and_file(capsys, tmp_path):
    setup_logging(level=logging.INFO, color=False, log_to_file=True, log_dir=tmp_path)
    log = logging.getLogger("tests.secrets")

    log.info("токен %s и ключ %s", FAKE_TG_TOKEN, FAKE_LLM_KEY)
    try:
        raise RuntimeError(f"запрос к https://api.telegram.org/bot{FAKE_TG_TOKEN}/send упал")
    except RuntimeError:
        log.exception("ошибка")

    for handler in logging.getLogger().handlers:
        handler.flush()
    console = capsys.readouterr().out
    file_text = (tmp_path / "app.log").read_text(encoding="utf-8")

    for text in (console, file_text):
        assert FAKE_TG_TOKEN not in text
        assert FAKE_LLM_KEY not in text
        assert "<telegram-token>" in text
    assert "Traceback" in file_text  # traceback в файле есть, но уже без секретов


def test_file_has_no_color_codes(tmp_path):
    setup_logging(level=logging.INFO, color=True, log_to_file=True, log_dir=tmp_path)
    logging.getLogger("tests.file").info("в файл")
    for handler in logging.getLogger().handlers:
        handler.flush()
    assert "\x1b[" not in (tmp_path / "app.log").read_text(encoding="utf-8")


def test_repeated_setup_does_not_duplicate_handlers(tmp_path):
    setup_logging(log_to_file=True, log_dir=tmp_path, color=False)
    setup_logging(log_to_file=True, log_dir=tmp_path, color=False)
    assert len(logging.getLogger().handlers) == 2  # консоль + файл, а не четыре


def test_noisy_libraries_are_quieted():
    setup_logging(level=logging.DEBUG, color=False)
    assert logging.getLogger("sqlalchemy.engine").level == logging.WARNING


def test_chatty_http_libraries_are_quiet_by_default():
    import logging

    from logger_config import DEFAULT_QUIET_LOGGERS

    for name in ("httpx", "httpcore", "huggingface_hub"):
        assert DEFAULT_QUIET_LOGGERS[name] >= logging.WARNING
