# forest-advice-bot/handlers/groups.py

"""Группы: на команды бот просит писать в личные сообщения, обычные сообщения не трогает."""

from aiogram import F, Router
from aiogram.types import Message

from filters.private_chat import GROUP_CHAT
from lexicon import lexicon_uk as lexicon


async def group_hint(message: Message) -> None:
    await message.answer(lexicon.GROUP_HINT)


def create_router() -> Router:
    router = Router(name="groups")
    router.message.filter(GROUP_CHAT)
    router.message.register(group_hint, F.text.startswith("/"))
    return router
