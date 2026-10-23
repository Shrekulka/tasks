# ruff: noqa: ASYNC109  (параметр timeout в сигнатурах задан базовым классом aiogram)
"""Поддельный HTTP-слой для aiogram: тесты не ходят в настоящий Telegram.

Бот отправляет запросы через «сессию». Подменяем её своей, которая запоминает запросы
и возвращает заготовленный ответ. Так можно проверить весь путь: команда -> роутер ->
хендлер -> ответ, не имея ни токена, ни интернета.
"""

from datetime import UTC, datetime

from aiogram.client.session.base import BaseSession
from aiogram.methods import SendMessage
from aiogram.types import Chat, Message


class FakeSession(BaseSession):
    def __init__(self) -> None:
        super().__init__()
        self.requests: list = []

    async def close(self) -> None:
        pass

    async def make_request(self, bot, method, timeout=None):
        self.requests.append(method)
        if isinstance(method, SendMessage):
            return Message(
                message_id=len(self.requests),
                date=datetime.now(tz=UTC),
                chat=Chat(id=int(method.chat_id), type="private"),
                text=method.text,
            )
        return True

    async def stream_content(
        self, url, headers=None, timeout=30, chunk_size=65536, raise_for_status=True
    ):
        yield b""
