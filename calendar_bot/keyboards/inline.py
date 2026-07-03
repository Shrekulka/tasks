# keyboards/inline.py

"""
Инлайн-клавиатуры бота.

Все callback_data строятся по схеме:
    "action:payload"

Например:
    "booking_confirm:yes"
    "booking_cancel_select:42"    ← 42 это booking.id
    "booking_cancel_confirm:42"
    "booking_cancel_abort:42"

Почему не использовать CallbackData (pydantic-модели из aiogram)?
Для нашего количества типов callback'ов строковая схема достаточна,
прозрачна и не требует дополнительных импортов в хендлерах.
При расширении (10+ типов кнопок) стоит перейти на CallbackData.
"""

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def get_confirm_keyboard() -> InlineKeyboardMarkup:
    """
    Клавиатура подтверждения создания заявки.

    Используется в состоянии BookingFSM.confirm.
    """
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Создать в Calendar",
                    callback_data="booking_confirm:yes",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="✏️ Начать заново",
                    callback_data="booking_confirm:restart",
                ),
                InlineKeyboardButton(
                    text="❌ Отмена",
                    callback_data="booking_confirm:no",
                ),
            ],
        ]
    )


def get_skip_keyboard() -> InlineKeyboardMarkup:
    """
    Клавиатура с единственной кнопкой «Пропустить».

    Используется в состоянии BookingFSM.description —
    описание необязательно, пользователь может пропустить.
    """
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="⏭ Пропустить",
                    callback_data="booking_skip_description",
                ),
            ]
        ]
    )


def get_bookings_list_keyboard(
    bookings: list[tuple[int, str, str]],
) -> InlineKeyboardMarkup:
    """
    Клавиатура со списком заявок для выбора при отмене.

    Args:
        bookings: список кортежей (booking_id, title, start_time_str).
                  Формирует по одной кнопке на каждую заявку.
                  Максимум показываем 10 заявок (иначе клавиатура
                  становится нечитаемой).

    Кнопки расположены по одной в строке — так проще нажимать
    на мобильном устройстве, особенно если title длинный.
    """
    buttons = []
    for booking_id, title, start_time_str in bookings[:10]:
        # Обрезаем длинные названия для читаемости кнопки
        label = f"📌 {title[:30]}{'...' if len(title) > 30 else ''} ({start_time_str})"
        buttons.append(
            [
                InlineKeyboardButton(
                    text=label,
                    callback_data=f"booking_cancel_select:{booking_id}",
                )
            ]
        )

    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_confirm_cancel_keyboard(booking_id: int) -> InlineKeyboardMarkup:
    """
    Клавиатура подтверждения отмены конкретной заявки.

    Args:
        booking_id: ID заявки в БД — передаётся в callback_data,
                    чтобы хендлер знал, что именно отменять.
    """
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Да, отменить",
                    callback_data=f"booking_cancel_confirm:{booking_id}",
                ),
                InlineKeyboardButton(
                    text="◀️ Назад",
                    callback_data=f"booking_cancel_abort:{booking_id}",
                ),
            ]
        ]
    )