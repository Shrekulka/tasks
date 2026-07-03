# external_services/google_calendar.py

import asyncio
from datetime import datetime
from typing import Any

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from config_data.config import config
from config_data.constants import EVENT_NOT_FOUND_STATUSES, GOOGLE_CALENDAR_SCOPES
from logger_config import logger


class GoogleCalendarError(Exception):
    """
    Базовая ошибка интеграции с Google Calendar.

    Хендлеры ловят именно это исключение и показывают пользователю
    понятное сообщение, не раскрывая внутренние детали API.
    """


class CalendarService:
    """
    Обёртка над Google Calendar API для работы через Service Account.

    Сам клиент googleapiclient синхронный, поэтому каждый метод,
    выполняющий сетевой запрос, оборачивается в asyncio.to_thread —
    это снимает блокировку event loop без переписывания клиента на async-аналог.
    """

    def __init__(
        self,
        credentials_file: str,
        calendar_id: str,
        timezone: str,
    ) -> None:
        self.calendar_id = calendar_id
        self.timezone = timezone

        try:
            credentials = Credentials.from_service_account_file(
                credentials_file, scopes=GOOGLE_CALENDAR_SCOPES
            )
            self._service = build("calendar", "v3", credentials=credentials)
            logger.info("Google Calendar service initialized successfully")
        except FileNotFoundError as error:
            logger.error(f"Service account file not found: {credentials_file}")
            raise GoogleCalendarError(
                f"Файл сервисного аккаунта не найден: {credentials_file}"
            ) from error
        except Exception as error:
            logger.error(f"Failed to initialize Google Calendar service: {error}")
            raise GoogleCalendarError("Не удалось подключиться к Google Calendar") from error

    def _build_description(self, username: str | None, description: str | None) -> str:
        """Формирует описание события: источник заявки + текст пользователя."""
        source_line = f"Заявка от @{username} (Telegram)" if username else "Заявка из Telegram-бота"
        if description:
            return f"{source_line}\n\n{description}"
        return source_line

    # ──────────────────────── синхронные вызовы к API ────────────────────────
    # Названия с префиксом _sync_ — выполняются внутри to_thread,
    # не вызывать напрямую из async-кода.

    def _sync_create_event(self, event_body: dict[str, Any]) -> dict[str, Any]:
        return (
            self._service.events()
            .insert(calendarId=self.calendar_id, body=event_body)
            .execute()
        )

    def _sync_delete_event(self, event_id: str) -> None:
        self._service.events().delete(
            calendarId=self.calendar_id, eventId=event_id
        ).execute()

    def _sync_list_events(self, time_min_str: str, time_max_str: str) -> list[dict[str, Any]]:
        events_result = (
            self._service.events()
            .list(
                calendarId=self.calendar_id,
                timeMin=time_min_str,
                timeMax=time_max_str,
                singleEvents=True,
                orderBy="startTime",
            )
            .execute()
        )
        return events_result.get("items", [])

    # ──────────────────────── публичные async-методы ────────────────────────

    async def create_event(
        self,
        title: str,
        start_time: datetime,
        end_time: datetime,
        username: str | None = None,
        description: str | None = None,
    ) -> dict[str, Any]:
        """
        Создаёт событие в календаре.

        Args:
            title: название события
            start_time: начало события в UTC (naive datetime)
            end_time: конец события в UTC (naive datetime)
            username: telegram username пользователя
            description: текст заявки от пользователя

        Returns:
            dict с ключами "event_id" и "html_link"

        Raises:
            GoogleCalendarError: если Calendar API вернул ошибку
        """
        event_body = {
            "summary": title,
            "description": self._build_description(username, description),
            "start": {"dateTime": start_time.isoformat(), "timeZone": self.timezone},
            "end": {"dateTime": end_time.isoformat(), "timeZone": self.timezone},
        }

        try:
            created_event = await asyncio.to_thread(self._sync_create_event, event_body)
        except HttpError as error:
            logger.error(f"Failed to create calendar event: {error}")
            raise GoogleCalendarError("Не удалось создать событие в календаре") from error

        logger.info(f"Calendar event created: {created_event.get('id')}")
        return {
            "event_id": created_event["id"],
            "html_link": created_event.get("htmlLink", ""),
        }

    async def delete_event(self, event_id: str) -> None:
        """
        Удаляет событие из календаря.

        Если событие уже удалено вручную (404/410) — это НЕ ошибка,
        итог "события больше нет" уже достигнут.
        """
        try:
            await asyncio.to_thread(self._sync_delete_event, event_id)
            logger.info(f"Calendar event deleted: {event_id}")
        except HttpError as error:
            if error.resp.status in EVENT_NOT_FOUND_STATUSES:
                logger.warning(
                    f"Event {event_id} was already deleted or not found, treating as success"
                )
                return
            logger.error(f"Failed to delete calendar event {event_id}: {error}")
            raise GoogleCalendarError("Не удалось удалить событие из календаря") from error

    async def list_events_in_range(
        self,
        time_min: datetime,
        time_max: datetime,
    ) -> list[dict[str, Any]]:
        """
        Возвращает события календаря, начинающиеся в интервале [time_min, time_max].

        Подготовлено для этапа 2 (проверка изменений события прямо в Google Calendar),
        в текущем MVP не используется.
        """
        time_min_str = time_min.isoformat() + "Z"
        time_max_str = time_max.isoformat() + "Z"

        try:
            return await asyncio.to_thread(self._sync_list_events, time_min_str, time_max_str)
        except HttpError as error:
            logger.error(f"Failed to list calendar events: {error}")
            raise GoogleCalendarError("Не удалось получить список событий") from error


# Единственный экземпляр сервиса, используется во всём приложении
calendar_service = CalendarService(
    credentials_file=config.google_calendar.credentials_file,
    calendar_id=config.google_calendar.calendar_id,
    timezone=config.google_calendar.timezone,
)