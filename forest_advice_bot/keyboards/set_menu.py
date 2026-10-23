"""Меню команд Telegram (кнопка «Меню» рядом с полем ввода)."""

from aiogram import Bot
from aiogram.types import BotCommand

from lexicon import lexicon_uk as lexicon


async def set_main_menu(bot: Bot) -> None:
    await bot.set_my_commands(
        [
            BotCommand(command="start", description=lexicon.CMD_START_DESC),
            BotCommand(command="help", description=lexicon.CMD_HELP_DESC),
            BotCommand(command="mydocs", description=lexicon.CMD_MYDOCS_DESC),
        ]
    )
