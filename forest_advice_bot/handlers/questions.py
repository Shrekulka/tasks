# forest-advice-bot/handlers/questions.py

"""Вопрос зверя: проверки, ответ по документам, понятное сообщение при сбое LLM."""

import logging
import time

from aiogram import Bot, F, Router
from aiogram.types import Message
from aiogram.utils.chat_action import ChatActionSender

from config_data.config import Config
from errors.errors import LLMError
from errors.user_messages import llm_error_text
from filters.private_chat import PRIVATE_CHAT
from lexicon import lexicon_uk as lexicon
from services.answering import AnswerService, split_message
from services.intent import SmallTalkDetector
from utils.utils import InFlightGuard

log = logging.getLogger(__name__)

_SMALLTALK = SmallTalkDetector(lexicon.SMALLTALK_PHRASES)


async def handle_question(
    message: Message,
    bot: Bot,
    config: Config,
    answers: AnswerService,
    question_guard: InFlightGuard[int],
) -> None:
    question = (message.text or "").strip()
    if not question:
        return
    if len(question) > config.limits.max_question_chars:
        await message.answer(
            lexicon.QUESTION_TOO_LONG.format(limit=config.limits.max_question_chars)
        )
        return

    if _SMALLTALK.matches(question):  # приветствие: ни эмбеддинга, ни LLM
        await message.answer(lexicon.SMALLTALK_REPLY)
        return

    user_id = message.from_user.id if message.from_user else message.chat.id
    if not question_guard.try_acquire(user_id):
        await message.answer(lexicon.QUESTION_BUSY)
        return

    started = time.monotonic()
    try:
        async with ChatActionSender.typing(bot=bot, chat_id=message.chat.id):
            answer = await answers.answer(question)
    except LLMError as error:
        log.warning("LLM не ответила: %s (%s)", type(error).__name__, error)
        await message.answer(llm_error_text(error))
        return
    finally:
        question_guard.release(user_id)

    for part in split_message(answer.text):
        await message.answer(part)
    # Текст вопроса в лог не пишем: только факты об ответе.
    log.info(
        "Ответ: found=%s sources=%d model=%s cache=%s %.2fs",
        answer.found,
        answer.sources,
        answer.model,
        answer.from_cache,
        time.monotonic() - started,
    )


def create_router() -> Router:
    router = Router(name="questions")
    router.message.filter(PRIVATE_CHAT)
    router.message.register(
        handle_question,
        F.text,
        ~F.text.startswith("/"),
        flags={"throttle": "questions"},
    )
    return router
