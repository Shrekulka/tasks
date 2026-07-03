# keyboards/keyboard_utils.py

"""
Вспомогательные утилиты для работы с клавиатурами.
"""

from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove


def get_main_menu() -> ReplyKeyboardMarkup:
    """
    Постоянная reply-клавиатура с основными командами.

    Показывается после /start и после завершения любого действия.
    Упрощает навигацию — пользователю не нужно помнить команды.
    """
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📅 Новая заявка")],
            [KeyboardButton(text="📋 Мои заявки")],
        ],
        resize_keyboard=True,       # меньше места в интерфейсе
        input_field_placeholder="Выбери действие или введи команду...",
    )


def remove_keyboard() -> ReplyKeyboardRemove:
    """Убирает reply-клавиатуру (используем внутри FSM-диалога)."""
    return ReplyKeyboardRemove()