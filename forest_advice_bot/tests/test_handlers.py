"""Тесты хендлеров. Вызываем функцию напрямую с поддельным сообщением (AsyncMock)."""

from unittest.mock import AsyncMock

from config_data.config import load_config
from handlers.common import cmd_help, cmd_start
from lexicon import lexicon_uk as lexicon


async def test_start_answers_with_greeting():
    message = AsyncMock()
    await cmd_start(message)
    message.answer.assert_awaited_once_with(lexicon.START)


async def test_help_shows_file_limit_from_config(valid_env, no_dotenv):
    valid_env.setenv("MAX_FILE_MB", "15")
    config = load_config(no_dotenv)
    message = AsyncMock()

    await cmd_help(message, config)

    text = message.answer.await_args.args[0]
    assert "15 МБ" in text
    assert "{" not in text  # подстановка выполнена, шаблонных скобок не осталось
