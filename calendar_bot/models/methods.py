# models/methods.py

from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.models import BookingRequest, BookingStatus


async def create_booking_request(
    session: AsyncSession,
    chat_id: int,
    username: str | None,
    title: str,
    description: str | None,
    start_time: datetime,
    end_time: datetime,
    google_event_id: str,
) -> BookingRequest:
    """
    Создаёт новую заявку со статусом CONFIRMED.

    Вызывается ПОСЛЕ успешного создания события в Google Calendar —
    google_event_id обязателен, так как без него запись в БД не имеет смысла
    (событие в календаре уже существует, нужно только сохранить связь
    chat_id <-> event_id для напоминаний и отмены).
    """
    booking = BookingRequest(
        chat_id=chat_id,
        username=username,
        title=title,
        description=description,
        start_time=start_time,
        end_time=end_time,
        google_event_id=google_event_id,
        status=BookingStatus.CONFIRMED,
    )
    session.add(booking)
    await session.commit()
    await session.refresh(booking)
    return booking


async def get_booking_by_id(session: AsyncSession, booking_id: int) -> BookingRequest | None:
    """Получает заявку по её ID."""
    return await session.get(BookingRequest, booking_id)


async def cancel_booking(session: AsyncSession, booking_id: int) -> BookingRequest | None:
    """
    Помечает заявку как CANCELLED.

    Сам вызов CalendarService.delete_event() сюда не входит — это задача
    booking_service.py, который сначала удаляет событие из Calendar,
    и только при успехе вызывает эту функцию (чтобы не было ситуации,
    когда в БД заявка отменена, а событие в календаре осталось).
    """
    booking = await session.get(BookingRequest, booking_id)
    if booking is None:
        return None

    booking.status = BookingStatus.CANCELLED
    await session.commit()
    await session.refresh(booking)
    return booking


async def get_user_bookings(
    session: AsyncSession,
    chat_id: int,
    statuses: list[BookingStatus] | None = None,
) -> list[BookingRequest]:
    """
    Возвращает заявки пользователя.

    Если передан statuses — фильтрует по этим статусам.
    Например, для "Мои заявки" обычно нужен только [CONFIRMED],
    а для истории — [CONFIRMED, CANCELLED].
    """
    query = select(BookingRequest).where(BookingRequest.chat_id == chat_id)
    if statuses:
        query = query.where(BookingRequest.status.in_(statuses))
    query = query.order_by(BookingRequest.start_time)

    result = await session.execute(query)
    return list(result.scalars().all())


async def get_upcoming_confirmed_bookings(
    session: AsyncSession,
    window_minutes: int,
) -> list[BookingRequest]:
    """
    Возвращает CONFIRMED-заявки, у которых start_time попадает
    в окно [сейчас, сейчас + window_minutes].

    window_minutes должен быть равен максимальному значению
    из config.google_calendar.reminder_offsets_minutes (например, 60) —
    тогда одним запросом покрываются все настроенные offset'ы,
    а конкретный выбор "пора слать или ещё нет" делает reminder_service
    для каждого offset отдельно.
    """
    now = datetime.utcnow()
    horizon = now + timedelta(minutes=window_minutes)

    query = select(BookingRequest).where(
        BookingRequest.status == BookingStatus.CONFIRMED,
        BookingRequest.start_time >= now,
        BookingRequest.start_time <= horizon,
    )

    result = await session.execute(query)
    return list(result.scalars().all())


async def mark_reminder_sent(
    session: AsyncSession,
    booking_id: int,
    offset_minutes: int,
) -> None:
    """Добавляет offset в список уже отправленных напоминаний для заявки."""
    booking = await session.get(BookingRequest, booking_id)
    if booking is None:
        return

    current = set(booking.sent_reminder_offsets.split(",")) if booking.sent_reminder_offsets else set()
    current.add(str(offset_minutes))
    booking.sent_reminder_offsets = ",".join(sorted(current, key=int))

    await session.commit()