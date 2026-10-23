# forest-advice-bot/handlers/fallback.py

"""Всё, что не подошло другим хендлерам: неизвестные команды и не-текстовые сообщения."""

from aiogram import F, Router
from aiogram.types import Message

from errors.user_messages import formats_text
from filters.private_chat import PRIVATE_CHAT
from lexicon import lexicon_uk as lexicon


async def unknown_command(message: Message) -> None:
    await message.answer(lexicon.UNKNOWN_COMMAND)


async def non_text(message: Message) -> None:
    await message.answer(lexicon.NON_TEXT.format(formats=formats_text()))


def create_router() -> Router:
    """Подключается последним: порядок роутеров задаёт приоритет."""
    router = Router(name="fallback")
    router.message.filter(PRIVATE_CHAT)
    router.message.register(unknown_command, F.text.startswith("/"))
    router.message.register(non_text, ~F.text)
    return router
