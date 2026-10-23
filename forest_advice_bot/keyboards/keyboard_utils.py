# forest-advice-bot/keyboards/keyboard_utils.py

"""Inline-клавиатуры /mydocs. Данные кнопок короткие: у Telegram предел 64 байта."""

from collections.abc import Sequence

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from lexicon import lexicon_uk as lexicon


class MyDocsCb(CallbackData, prefix="md"):
    """action: page (показать страницу), del (спросить), delok (удалить), delno (отмена)."""

    action: str
    doc_id: int = 0
    page: int = 0


def docs_page_keyboard(
    doc_ids: Sequence[int], *, first_number: int, page: int, pages: int
) -> InlineKeyboardMarkup:
    """По кнопке «Видалити» на документ и навигация. first_number: номер первого документа."""
    builder = InlineKeyboardBuilder()
    for offset, doc_id in enumerate(doc_ids):
        builder.button(
            text=lexicon.BTN_DELETE.format(number=first_number + offset),
            callback_data=MyDocsCb(action="del", doc_id=doc_id, page=page),
        )
    builder.adjust(1)

    navigation: list = []
    if page > 1:
        navigation.append(("prev", page - 1))
    if page < pages:
        navigation.append(("next", page + 1))
    if navigation:
        nav = InlineKeyboardBuilder()
        for kind, target in navigation:
            text = lexicon.BTN_PREV if kind == "prev" else lexicon.BTN_NEXT
            nav.button(text=text, callback_data=MyDocsCb(action="page", page=target))
        nav.adjust(len(navigation))
        builder.attach(nav)
    return builder.as_markup()


def confirm_keyboard(doc_id: int, page: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(
        text=lexicon.BTN_CONFIRM_DELETE,
        callback_data=MyDocsCb(action="delok", doc_id=doc_id, page=page),
    )
    builder.button(text=lexicon.BTN_CANCEL, callback_data=MyDocsCb(action="delno", page=page))
    builder.adjust(2)
    return builder.as_markup()
