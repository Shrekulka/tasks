# models/models.py

import enum
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Enum, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class BookingStatus(str, enum.Enum):
    """Статус заявки."""
    CONFIRMED = "confirmed"
    CANCELLED = "cancelled"


class BookingRequest(Base):
    """
    Заявка пользователя на событие в календаре.

    Жизненный цикл:
        PENDING -> APPROVED (создаётся google_event_id)
        PENDING -> REJECTED
        PENDING -> CANCELLED
    """
    __tablename__ = "booking_requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # Кто оставил заявку — нужно, чтобы отправить ему результат модерации
    # и напоминание перед началом события.
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    username: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # Содержимое заявки
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Время события храним в UTC (naive datetime, tzinfo не пишем в БД,
    # таймзона применяется на уровне Calendar API и при выводе пользователю)
    start_time: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    end_time: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    status: Mapped[BookingStatus] = mapped_column(
        Enum(BookingStatus, name="booking_status"),
        default=BookingStatus.CONFIRMED,
        nullable=False,
        index=True,
    )

    # Событие в Calendar создаётся ДО записи в БД, поэтому ID всегда известен
    google_event_id: Mapped[str] = mapped_column(String(255), nullable=False)

    # Флаги отправленных напоминаний.
    # Формат: "60,15" — список offset'ов (в минутах), по которым уже отправлено напоминание.
    # Простая строка вместо отдельной таблицы — для MVP достаточно и проще в миграциях.
    sent_reminder_offsets: Mapped[str] = mapped_column(String(64), default="", nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )

    def __repr__(self) -> str:
        return (
            f"<BookingRequest id={self.id} chat_id={self.chat_id} "
            f"title={self.title!r} status={self.status}>"
        )