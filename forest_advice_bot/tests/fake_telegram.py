# ruff: noqa: ASYNC109  (параметр timeout в сигнатурах задан базовым классом aiogram)
"""Поддельный Telegram для тестов хендлеров: настоящий диспетчер и роутеры, без сети.

Запоминает отправленные и изменённые сообщения, отдаёт «скачанные» файлы, отвечает на
callback-запросы. Конструкторы Update повторяют то, что присылает Telegram.
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import (
    AnswerCallbackQuery,
    EditMessageText,
    GetFile,
    SendChatAction,
    SendMessage,
)
from aiogram.types import (
    CallbackQuery,
    Chat,
    Document,
    File,
    Message,
    PhotoSize,
    Sticker,
    Update,
    User,
    Voice,
)

FAKE_TG_TOKEN = "123456:TEST-TOKEN-NOT-REAL"


@dataclass(slots=True)
class Sent:
    chat_id: int
    text: str
    reply_markup: Any = None


@dataclass(slots=True)
class CallbackAnswer:
    text: str | None
    show_alert: bool


@dataclass(slots=True)
class FakeTelegram(BaseSession):
    files: dict[str, bytes] = field(default_factory=dict)
    too_big: set[str] = field(default_factory=set)  # file_id, которые Telegram не отдаёт боту
    sent: list[Sent] = field(default_factory=list)
    edits: list[Sent] = field(default_factory=list)
    callback_answers: list[CallbackAnswer] = field(default_factory=list)
    actions: list[str] = field(default_factory=list)
    methods: list[Any] = field(default_factory=list)  # все вызванные методы Bot API
    _counter: int = 0

    def __post_init__(self) -> None:
        BaseSession.__init__(self)

    async def close(self) -> None:
        pass

    def _message(self, chat_id: int, text: str | None) -> Message:
        self._counter += 1
        return Message(
            message_id=self._counter,
            date=datetime.now(tz=UTC),
            chat=Chat(id=chat_id, type="private"),
            text=text,
        )

    async def make_request(self, bot, method, timeout=None):
        self.methods.append(method)
        if isinstance(method, SendMessage):
            self.sent.append(Sent(int(method.chat_id), method.text, method.reply_markup))
            return self._message(int(method.chat_id), method.text)
        if isinstance(method, EditMessageText):
            self.edits.append(Sent(int(method.chat_id or 0), method.text, method.reply_markup))
            return self._message(int(method.chat_id or 0), method.text)
        if isinstance(method, AnswerCallbackQuery):
            self.callback_answers.append(CallbackAnswer(method.text, bool(method.show_alert)))
            return True
        if isinstance(method, SendChatAction):
            self.actions.append(str(method.action))
            return True
        if isinstance(method, GetFile):
            if method.file_id in self.too_big:
                raise TelegramBadRequest(method=method, message="Bad Request: file is too big")
            data = self.files[method.file_id]
            return File(
                file_id=method.file_id,
                file_unique_id=f"u-{method.file_id}",
                file_size=len(data),
                file_path=f"files/{method.file_id}",
            )
        return True

    async def stream_content(
        self, url, headers=None, timeout=30, chunk_size=65536, raise_for_status=True
    ):
        yield self.files[str(url).rsplit("/", 1)[-1]]

    # ------------------------------------------------------------------ удобные чтения

    def texts(self) -> list[str]:
        return [item.text for item in self.sent]

    def last_markup_buttons(self) -> list[tuple[str, str]]:
        """(текст, callback_data) всех кнопок последнего сообщения или правки со списком."""
        source = (self.edits or self.sent)[-1]
        if source.reply_markup is None:
            return []
        return [
            (button.text, button.callback_data)
            for row in source.reply_markup.inline_keyboard
            for button in row
        ]


def make_bot(session: FakeTelegram) -> Bot:
    return Bot(token=FAKE_TG_TOKEN, session=session)


# ---------------------------------------------------------------------- Update


def _user(user_id: int, name: str, username: str | None) -> User:
    return User(id=user_id, is_bot=False, first_name=name, username=username)


def _message(
    update_id: int,
    *,
    user_id: int = 42,
    name: str = "Їжак",
    username: str | None = "hedgehog",
    chat_type: str = "private",
    chat_id: int | None = None,
    **content: Any,
) -> Update:
    message = Message(
        message_id=update_id,
        date=datetime.now(tz=UTC),
        chat=Chat(id=chat_id if chat_id is not None else user_id, type=chat_type),
        from_user=_user(user_id, name, username),
        **content,
    )
    return Update(update_id=update_id, message=message)


class Updates:
    """Счётчик update_id и фабрики Update."""

    def __init__(self) -> None:
        self._id = 0

    def _next(self) -> int:
        self._id += 1
        return self._id

    def text(self, text: str, **kwargs: Any) -> Update:
        entities = None
        if text.startswith("/"):
            from aiogram.types import MessageEntity

            entities = [MessageEntity(type="bot_command", offset=0, length=len(text.split()[0]))]
        return _message(self._next(), text=text, entities=entities, **kwargs)

    def document(
        self,
        file_id: str,
        file_name: str | None,
        *,
        size: int = 100,
        mime: str | None = None,
        caption: str | None = None,
        **kwargs: Any,
    ) -> Update:
        document = Document(
            file_id=file_id,
            file_unique_id=f"u-{file_id}",
            file_name=file_name,
            mime_type=mime,
            file_size=size,
        )
        return _message(self._next(), document=document, caption=caption, **kwargs)

    def voice(self, **kwargs: Any) -> Update:
        voice = Voice(file_id="v", file_unique_id="uv", duration=1)
        return _message(self._next(), voice=voice, **kwargs)

    def photo(self, **kwargs: Any) -> Update:
        photo = [PhotoSize(file_id="p", file_unique_id="up", width=10, height=10)]
        return _message(self._next(), photo=photo, **kwargs)

    def sticker(self, **kwargs: Any) -> Update:
        sticker = Sticker(
            file_id="s",
            file_unique_id="us",
            type="regular",
            width=10,
            height=10,
            is_animated=False,
            is_video=False,
        )
        return _message(self._next(), sticker=sticker, **kwargs)

    def callback(
        self,
        data: str,
        *,
        user_id: int = 42,
        name: str = "Їжак",
        username: str | None = "hedgehog",
    ) -> Update:
        update_id = self._next()
        message = Message(
            message_id=update_id,
            date=datetime.now(tz=UTC),
            chat=Chat(id=user_id, type="private"),
            text="list",
        )
        query = CallbackQuery(
            id=str(update_id),
            from_user=_user(user_id, name, username),
            chat_instance="ci",
            data=data,
            message=message,
        )
        return Update(update_id=update_id, callback_query=query)
