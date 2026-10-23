"""Общие команды: /start и /help."""

from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command, CommandStart
from aiogram.types import Message

from config_data.config import Config
from errors.user_messages import formats_text
from filters.private_chat import PRIVATE_CHAT
from lexicon import lexicon_uk as lexicon


async def cmd_start(message: Message) -> None:
    await message.answer(lexicon.START)


async def cmd_help(message: Message, config: Config) -> None:
    # `config` в хендлер подставляет aiogram: он берёт его из данных диспетчера по имени
    # аргумента (мы передали config=... в Dispatcher, см. bot.py).
    await message.answer(lexicon.HELP.format(max_mb=config.rag.max_file_mb, formats=formats_text()))


def create_router() -> Router:
    """Каждый вызов даёт НОВЫЙ роутер. Роутер можно подключить только к одному
    диспетчеру, поэтому общий роутер на уровне модуля сломал бы повторную сборку
    диспетчера (а мы собираем его заново в каждом тесте)."""
    router = Router(name="common")
    router.message.filter(PRIVATE_CHAT)  # в группах отвечает groups.py
    router.message.register(cmd_start, CommandStart())
    router.message.register(cmd_help, Command("help"))
    return router
