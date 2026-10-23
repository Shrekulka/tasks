"""Тесты сборки бота: диспетчер, маршрутизация команд, сообщения об ошибках запуска."""

from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest
from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramNetworkError, TelegramUnauthorizedError
from aiogram.methods import GetMe
from aiogram.types import Chat, Message, MessageEntity, Update, User

import bot as bot_module
from config_data.config import ConfigError, load_config
from handlers import common
from lexicon import lexicon_uk as lexicon
from tests.conftest import FAKE_TG_TOKEN
from tests.fakes import FakeSession


def _command_update(text: str, chat_id: int = 42) -> Update:
    command_length = len(text.split()[0])
    return Update(
        update_id=1,
        message=Message(
            message_id=1,
            date=datetime.now(tz=UTC),
            chat=Chat(id=chat_id, type="private"),
            from_user=User(id=chat_id, is_bot=False, first_name="Їжак"),
            text=text,
            entities=[MessageEntity(type="bot_command", offset=0, length=command_length)],
        ),
    )


@pytest.fixture
def config(valid_env, no_dotenv):
    return load_config(no_dotenv)


def test_build_dispatcher_can_be_called_repeatedly(config):
    # Если бы роутер был общим на уровне модуля, второй вызов упал бы с ошибкой.
    first = bot_module.build_dispatcher(config, MagicMock())
    second = bot_module.build_dispatcher(config, MagicMock())
    assert isinstance(first, Dispatcher) and isinstance(second, Dispatcher)


def test_create_router_returns_fresh_router_each_time():
    assert common.create_router() is not common.create_router()


async def test_start_command_goes_through_router_and_gets_answer(config):
    session = FakeSession()
    bot = Bot(token=FAKE_TG_TOKEN, session=session)
    dispatcher = bot_module.build_dispatcher(config, MagicMock())

    await dispatcher.feed_update(bot, _command_update("/start"))

    assert len(session.requests) == 1
    assert session.requests[0].text == lexicon.START
    assert session.requests[0].chat_id == 42


async def test_help_command_receives_config_by_dependency_injection(config):
    session = FakeSession()
    bot = Bot(token=FAKE_TG_TOKEN, session=session)
    dispatcher = bot_module.build_dispatcher(config, MagicMock())

    await dispatcher.feed_update(bot, _command_update("/help"))

    assert f"{config.rag.max_file_mb} МБ" in session.requests[0].text


def test_run_exits_with_readable_message_on_config_error(monkeypatch):
    async def broken_main():
        raise ConfigError({"TELEGRAM_BOT_TOKEN": ["переменная не задана"]})

    monkeypatch.setattr(bot_module, "main", broken_main)

    with pytest.raises(SystemExit) as exc_info:
        bot_module.run()

    assert "TELEGRAM_BOT_TOKEN" in str(exc_info.value.code)


@pytest.mark.parametrize(
    ("error_factory", "expected_text"),
    [
        (lambda: TelegramUnauthorizedError(method=GetMe(), message="Unauthorized"), "токен"),
        (lambda: TelegramNetworkError(method=GetMe(), message="boom"), "Telegram"),
    ],
)
def test_run_turns_telegram_startup_errors_into_readable_exit(
    monkeypatch, error_factory, expected_text
):
    async def failing_main():
        raise error_factory()

    monkeypatch.setattr(bot_module, "main", failing_main)

    with pytest.raises(SystemExit) as exc_info:
        bot_module.run()

    assert expected_text in str(exc_info.value.code)
    assert "boom" not in str(exc_info.value.code)  # детали ошибки наружу не выводим
