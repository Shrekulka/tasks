from types import SimpleNamespace

import pytest
from aiogram.dispatcher.flags import FlagGenerator

from keyboards.keyboard_utils import MyDocsCb, confirm_keyboard, docs_page_keyboard
from middlewares.throttling import ThrottleRule, ThrottlingMiddleware


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def _event(user_id=1):
    replies = []

    async def answer(text):
        replies.append(text)

    return SimpleNamespace(from_user=SimpleNamespace(id=user_id), answer=answer, replies=replies)


def _data(flag):
    flags = {"throttle": flag} if flag else {}
    return {"handler": SimpleNamespace(flags=flags)}


async def _run(middleware, event, flag):
    calls = []

    async def handler(ev, data):
        calls.append(1)
        return "handled"

    result = await middleware(handler, event, _data(flag))
    return result, len(calls)


async def test_limit_window_and_reset():
    clock = Clock()
    middleware = ThrottlingMiddleware({"q": ThrottleRule(2, 60, "стоп")}, clock=clock)
    event = _event()

    assert await _run(middleware, event, "q") == ("handled", 1)
    assert await _run(middleware, event, "q") == ("handled", 1)
    assert await _run(middleware, event, "q") == (None, 0)
    assert event.replies == ["стоп"]

    clock.now += 61
    assert await _run(middleware, event, "q") == ("handled", 1)


async def test_buckets_and_users_are_independent():
    middleware = ThrottlingMiddleware(
        {"a": ThrottleRule(1, 60, "a-стоп"), "b": ThrottleRule(1, 60, "b-стоп")}, clock=Clock()
    )
    first = _event(1)

    await _run(middleware, first, "a")
    assert (await _run(middleware, first, "b"))[1] == 1  # другой бакет
    assert (await _run(middleware, _event(2), "a"))[1] == 1  # другой пользователь
    assert (await _run(middleware, first, "a"))[1] == 0


@pytest.mark.parametrize("flag", [None, "unknown-bucket"])
async def test_unflagged_or_unknown_handlers_are_not_limited(flag):
    middleware = ThrottlingMiddleware({"q": ThrottleRule(1, 60, "стоп")}, clock=Clock())
    event = _event()

    for _ in range(5):
        assert await _run(middleware, event, flag) == ("handled", 1)


def test_flag_generator_is_the_way_handlers_are_marked():
    assert FlagGenerator  # flags={"throttle": ...} в register() использует тот же механизм


def test_callback_data_roundtrip_and_length():
    original = MyDocsCb(action="delok", doc_id=9_999_999_999, page=123_456)

    packed = original.pack()

    assert len(packed.encode()) <= 64
    assert MyDocsCb.unpack(packed) == original


def test_page_keyboard_buttons_and_navigation():
    keyboard = docs_page_keyboard([11, 12], first_number=6, page=2, pages=3)

    rows = [[(b.text, b.callback_data) for b in row] for row in keyboard.inline_keyboard]
    assert rows[0] == [("🗑 Видалити 6", "md:del:11:2")]
    assert rows[1] == [("🗑 Видалити 7", "md:del:12:2")]
    assert rows[2] == [("◀ Назад", "md:page:0:1"), ("Далі ▶", "md:page:0:3")]


@pytest.mark.parametrize(
    ("page", "pages", "labels"), [(1, 1, []), (1, 3, ["Далі ▶"]), (3, 3, ["◀ Назад"])]
)
def test_navigation_only_where_it_makes_sense(page, pages, labels):
    keyboard = docs_page_keyboard([1], first_number=1, page=page, pages=pages)

    nav = [b.text for row in keyboard.inline_keyboard[1:] for b in row]
    assert nav == labels


def test_confirm_keyboard_keeps_page_for_cancel():
    keyboard = confirm_keyboard(5, page=3)

    buttons = [(b.text, b.callback_data) for b in keyboard.inline_keyboard[0]]
    assert buttons == [("Так, видалити", "md:delok:5:3"), ("Скасувати", "md:delno:0:3")]
