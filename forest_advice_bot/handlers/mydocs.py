# forest-advice-bot/handlers/mydocs.py

"""/mydocs: список своих документов и удаление с подтверждением.

Владелец проверяется в самом запросе к БД по callback.from_user.id. Идентификатор в данных
кнопки доказательством права не считается: его можно подделать.
"""

import html
import logging
import math
from zoneinfo import ZoneInfo

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from config_data.config import Config
from filters.private_chat import PRIVATE_CHAT
from keyboards.keyboard_utils import MyDocsCb, confirm_keyboard, docs_page_keyboard
from lexicon import lexicon_uk as lexicon
from models.methods import (
    count_user_documents,
    delete_user_document,
    get_user_document,
    list_user_documents,
)
from models.models import DocStatus
from services.answer_cache import AnswerCache

log = logging.getLogger(__name__)

_STATUS_TEXT = {
    DocStatus.PROCESSING: lexicon.STATUS_PROCESSING,
    DocStatus.READY: lexicon.STATUS_READY,
    DocStatus.FAILED: lexicon.STATUS_FAILED,
}


def _escape(text: str) -> str:
    return html.escape(text, quote=False)


async def render_page(
    session_factory: async_sessionmaker[AsyncSession],
    owner_id: int,
    page: int,
    config: Config,
) -> tuple[str, InlineKeyboardMarkup | None]:
    """Текст и клавиатура страницы. Номер страницы приводится к допустимому."""
    size = config.limits.mydocs_page_size
    timezone = ZoneInfo(config.tg_bot.display_timezone)
    async with session_factory() as session:
        total = await count_user_documents(session, owner_id)
        if total == 0:
            return lexicon.NO_DOCS, None
        pages = math.ceil(total / size)
        page = min(max(page, 1), pages)
        docs = await list_user_documents(session, owner_id, offset=(page - 1) * size, limit=size)

    first_number = (page - 1) * size + 1
    lines = [lexicon.MYDOCS_HEADER.format(total=total, page=page, pages=pages)]
    for offset, doc in enumerate(docs):
        lines.append(
            lexicon.MYDOCS_LINE.format(
                number=first_number + offset,
                title=_escape(doc.title),
                when=doc.uploaded_at.astimezone(timezone).strftime(lexicon.DATE_FORMAT),
                status=_STATUS_TEXT[doc.status],
            )
        )
    keyboard = docs_page_keyboard(
        [doc.id for doc in docs], first_number=first_number, page=page, pages=pages
    )
    return "\n".join(lines), keyboard


async def _edit(callback: CallbackQuery, text: str, keyboard: InlineKeyboardMarkup | None) -> None:
    """Правит сообщение со списком. «Сообщение не изменилось» не считается ошибкой."""
    if not isinstance(callback.message, Message):
        return
    try:
        await callback.message.edit_text(text, reply_markup=keyboard)
    except TelegramBadRequest as error:
        if "not modified" not in str(error).lower():
            raise


async def cmd_mydocs(
    message: Message, config: Config, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    if message.from_user is None:
        return
    text, keyboard = await render_page(session_factory, message.from_user.id, 1, config)
    await message.answer(text, reply_markup=keyboard)


async def on_page(
    callback: CallbackQuery,
    callback_data: MyDocsCb,
    config: Config,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    text, keyboard = await render_page(
        session_factory, callback.from_user.id, callback_data.page, config
    )
    await _edit(callback, text, keyboard)
    await callback.answer()


async def on_ask_delete(
    callback: CallbackQuery,
    callback_data: MyDocsCb,
    config: Config,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        doc = await get_user_document(session, callback_data.doc_id, callback.from_user.id)
    if doc is None:
        await callback.answer(lexicon.FOREIGN_DOC, show_alert=True)
        return
    await _edit(
        callback,
        lexicon.CONFIRM_DELETE.format(title=_escape(doc.title)),
        confirm_keyboard(doc.id, callback_data.page),
    )
    await callback.answer()


async def on_confirm_delete(
    callback: CallbackQuery,
    callback_data: MyDocsCb,
    config: Config,
    session_factory: async_sessionmaker[AsyncSession],
    cache: AnswerCache,
) -> None:
    owner_id = callback.from_user.id
    async with session_factory() as session:
        doc = await get_user_document(session, callback_data.doc_id, owner_id)
        deleted = doc is not None and await delete_user_document(session, doc.id, owner_id)
        await session.commit()
    if not deleted or doc is None:
        await callback.answer(lexicon.FOREIGN_DOC, show_alert=True)
        return

    cache.bump()  # старые ответы могли опираться на удалённый документ
    log.info("Документ %d удалён владельцем", doc.id)
    text, keyboard = await render_page(session_factory, owner_id, callback_data.page, config)
    await _edit(callback, text, keyboard)
    await callback.answer(lexicon.DOC_DELETED.format(title=doc.title), show_alert=False)


async def on_cancel_delete(
    callback: CallbackQuery,
    callback_data: MyDocsCb,
    config: Config,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    text, keyboard = await render_page(
        session_factory, callback.from_user.id, callback_data.page, config
    )
    await _edit(callback, text, keyboard)
    await callback.answer()


def create_router() -> Router:
    router = Router(name="mydocs")
    router.message.filter(PRIVATE_CHAT)
    router.callback_query.filter(F.message.chat.type == "private")
    router.message.register(cmd_mydocs, Command("mydocs"))
    router.callback_query.register(on_page, MyDocsCb.filter(F.action == "page"))
    router.callback_query.register(on_ask_delete, MyDocsCb.filter(F.action == "del"))
    router.callback_query.register(on_confirm_delete, MyDocsCb.filter(F.action == "delok"))
    router.callback_query.register(on_cancel_delete, MyDocsCb.filter(F.action == "delno"))
    return router
