# config_data/constants.py
from zoneinfo import ZoneInfo  # stdlib с Python 3.9+

from config_data.config import config

"""
Константы приложения — значения, которые являются частью логики кода,
а не конфигурацией окружения (в отличие от .env).
"""

# Права доступа, запрашиваемые у Google Calendar API.
# Нужны именно "calendar" (полный доступ к событиям), т.к. CalendarService
# делает insert/delete/list. Более узкий scope "calendar.events" тоже подошёл бы,
# но "calendar" — стандартный выбор для сервисных аккаунтов с одним календарём.
GOOGLE_CALENDAR_SCOPES = ["https://www.googleapis.com/auth/calendar"]

# HTTP-статусы, при которых "удаление" события считаем успешным,
# даже если Calendar API вернул ошибку (событие просто уже не существует).
EVENT_NOT_FOUND_STATUSES = (404, 410)

# Таймзона из конфига (например, "Europe/Kyiv")
TZ_LOCAL = ZoneInfo(config.google_calendar.timezone)

# Формат, который принимает бот от пользователя
INPUT_FORMAT = "%Y-%m-%d %H:%M"

# Формат для отображения пользователю в сообщениях
DISPLAY_FORMAT = "%d.%m.%Y %H:%M"