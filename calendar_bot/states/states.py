# states/states.py

from aiogram.fsm.state import State, StatesGroup


class BookingFSM(StatesGroup):
    """
    Состояния FSM для создания заявки.

    Флоу:
        /new_booking
            └─► title        (пользователь вводит название)
                └─► description  (пользователь вводит описание или пропускает)
                    └─► start_time  (пользователь вводит дату и время начала)
                        └─► end_time    (пользователь вводит дату и время конца)
                            └─► confirm    (инлайн-кнопки ✅ Создать / ❌ Отмена)

    Почему description идёт до времени, а не после?
    Психологически проще: сначала "что за событие" (title + description),
    потом "когда" (start + end). Так же работают большинство календарных приложений.
    """

    title = State()        # Название события
    description = State()  # Описание (можно пропустить через кнопку)
    start_time = State()   # Дата и время начала (текстом: YYYY-MM-DD HH:MM)
    end_time = State()     # Дата и время конца  (текстом: YYYY-MM-DD HH:MM)
    confirm = State()      # Просмотр итогов + подтверждение или отмена


class CancelBookingFSM(StatesGroup):
    """
    Состояния FSM для отмены существующей заявки.

    Флоу:
        /my_bookings
            └─► выбор заявки (инлайн-список)
                └─► confirm_cancel (инлайн-кнопки ✅ Да, отменить / ◀️ Назад)
    """

    select = State()         # Пользователь выбирает заявку из списка
    confirm_cancel = State() # Подтверждение отмены конкретной заявки