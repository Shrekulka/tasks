# forest-advice-bot/errors/handlers.py

"""Общий обработчик необработанных ошибок: зверь не остаётся без ответа, traceback в лог."""

import logging

from aiogram.exceptions import TelegramAPIError
from aiogram.types import ErrorEvent
from sqlalchemy.exc import InterfaceError, OperationalError

from errors.errors import DatabaseUnavailable
from lexicon import lexicon_uk as lexicon

log = logging.getLogger(__name__)

_DB_ERRORS = (OperationalError, InterfaceError, DatabaseUnavailable)


async def on_error(event: ErrorEvent) -> bool:
    error = event.exception
    log.error("Необработанная ошибка в обработчике", exc_info=error)
    text = lexicon.DB_UNAVAILABLE if isinstance(error, _DB_ERRORS) else lexicon.UNEXPECTED_ERROR

    update = event.update
    try:
        if update.message is not None:
            await update.message.answer(text)
        elif update.callback_query is not None:
            await update.callback_query.answer(text, show_alert=True)
    except TelegramAPIError:
        log.warning("Не удалось сообщить зверю об ошибке", exc_info=True)
    return True  # ошибка обработана: aiogram не пробрасывает её дальше
