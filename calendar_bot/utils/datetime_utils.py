# utils/datetime_utils.py

"""
Утилиты для работы с датой и временем.

Все функции работают с naive datetime в UTC.
Конвертация в локальную таймзону (Europe/Kyiv) происходит только
в двух местах:
  1. При передаче в Google Calendar API (поле timeZone в event body)
  2. При отображении пользователю в сообщениях (format_dt_for_user)

Внутри приложения (БД, сравнения, reminder_service) — всегда UTC.
"""

from datetime import datetime, timezone, timedelta

from config_data.constants import INPUT_FORMAT, TZ_LOCAL, DISPLAY_FORMAT


def parse_user_datetime(text: str) -> datetime | None:
    """
    Парсит строку от пользователя в naive UTC datetime.

    Пользователь вводит время в локальной таймзоне (Europe/Kyiv).
    Мы конвертируем в UTC для хранения в БД.

    Args:
        text: строка вида "2026-07-15 14:30"

    Returns:
        naive datetime в UTC, или None если формат неверный
    """
    text = text.strip()
    try:
        # Парсим как локальное время
        local_dt = datetime.strptime(text, INPUT_FORMAT)
        # Присваиваем таймзону (aware datetime)
        aware_local = local_dt.replace(tzinfo=TZ_LOCAL)
        # Конвертируем в UTC и делаем naive (для хранения в БД)
        utc_dt = aware_local.astimezone(timezone.utc).replace(tzinfo=None)
        return utc_dt
    except ValueError:
        return None


def format_dt_for_user(dt_utc: datetime) -> str:
    """
    Форматирует naive UTC datetime для показа пользователю в локальной TZ.

    Args:
        dt_utc: naive datetime в UTC (как хранится в БД)

    Returns:
        строка вида "15.07.2026 14:30" (в локальной таймзоне)
    """
    # Делаем aware UTC
    aware_utc = dt_utc.replace(tzinfo=timezone.utc)
    # Конвертируем в локальное время
    local_dt = aware_utc.astimezone(TZ_LOCAL)
    return local_dt.strftime(DISPLAY_FORMAT)


def utc_to_local_isoformat(dt_utc: datetime) -> str:
    """
    Конвертирует naive UTC datetime в ISO 8601 строку в локальной TZ.

    Используется при передаче времени в Google Calendar API:
        event["start"]["dateTime"] = utc_to_local_isoformat(booking.start_time)
        event["start"]["timeZone"] = config.google_calendar.timezone

    Returns:
        строка вида "2026-07-15T14:30:00+03:00"
    """
    aware_utc = dt_utc.replace(tzinfo=timezone.utc)
    local_dt = aware_utc.astimezone(TZ_LOCAL)
    return local_dt.isoformat()


def is_future(dt_utc: datetime, min_minutes_ahead: int = 10) -> bool:
    """
    Проверяет, что время события в будущем (минимум min_minutes_ahead минут от сейчас).

    Args:
        dt_utc: naive datetime в UTC
        min_minutes_ahead: минимальный запас в минутах (по умолчанию 10)

    Returns:
        True если событие достаточно далеко в будущем
    """
    now_utc = datetime.utcnow()
    return dt_utc >= now_utc + timedelta(minutes=min_minutes_ahead)


def is_end_after_start(start_utc: datetime, end_utc: datetime, min_duration_minutes: int = 15) -> bool:
    """
    Проверяет, что конец события позже начала минимум на min_duration_minutes минут.

    Args:
        start_utc: время начала (naive UTC)
        end_utc: время конца (naive UTC)
        min_duration_minutes: минимальная длительность в минутах

    Returns:
        True если конец позже начала на достаточный интервал
    """
    return end_utc >= start_utc + timedelta(minutes=min_duration_minutes)


def get_hint_example() -> str:
    """
    Возвращает пример корректного ввода времени на основе текущей даты.

    Используется в сообщениях-подсказках пользователю, чтобы пример
    всегда был "в будущем" и не вводил в заблуждение.
    """
    # Показываем завтра в 12:00 как пример
    tomorrow = datetime.utcnow().replace(tzinfo=timezone.utc).astimezone(TZ_LOCAL)
    tomorrow = tomorrow.replace(hour=12, minute=0, second=0, microsecond=0)
    tomorrow = tomorrow + timedelta(days=1)
    return tomorrow.strftime(INPUT_FORMAT)