# forest-advice-bot/middlewares/throttling.py

"""Ограничение частоты действий на одного зверя.

Хендлер помечается флагом: flags={"throttle": "questions"}. Правила (сколько событий за какое
окно и что ответить) задаются при создании, поэтому middleware не знает ни про вопросы,
ни про документы.
"""

import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

from aiogram import BaseMiddleware
from aiogram.dispatcher.flags import get_flag
from aiogram.types import Message

from config_data.config import LimitsConfig
from lexicon import lexicon_uk as lexicon
from utils.utils import Clock, SlidingWindowLimiter

_MINUTE = 60.0
_HOUR = 3600.0
_CLEANUP_EVERY_CALLS = 512  # как часто чистить лимитеры, у которых окно уже пусто


@dataclass(frozen=True, slots=True)
class ThrottleRule:
    max_events: int
    window_seconds: float
    reply: str  # что ответить, когда лимит исчерпан


def rules_from_config(limits: LimitsConfig) -> dict[str, ThrottleRule]:
    return {
        "questions": ThrottleRule(
            limits.questions_per_minute, _MINUTE, lexicon.QUESTIONS_THROTTLED
        ),
        "uploads": ThrottleRule(limits.uploads_per_hour, _HOUR, lexicon.UPLOADS_THROTTLED),
    }


class ThrottlingMiddleware(BaseMiddleware):
    def __init__(self, rules: Mapping[str, ThrottleRule], clock: Clock = time.monotonic) -> None:
        self._rules = dict(rules)
        self._clock = clock
        self._limiters: dict[tuple[int, str], SlidingWindowLimiter] = {}
        self._calls = 0

    def _evict_idle(self) -> None:
        """Без этого словарь рос бы вечно: по записи на каждого зверя и каждое правило."""
        self._limiters = {k: v for k, v in self._limiters.items() if not v.is_idle()}

    async def __call__(
        self,
        handler: Callable[[Message, dict[str, Any]], Awaitable[Any]],
        event: Message,
        data: dict[str, Any],
    ) -> Any:
        bucket = get_flag(data, "throttle")
        rule = self._rules.get(bucket) if bucket else None
        if rule is None or event.from_user is None:
            return await handler(event, data)

        self._calls += 1
        if self._calls % _CLEANUP_EVERY_CALLS == 0:
            self._evict_idle()
        key = (event.from_user.id, bucket)
        limiter = self._limiters.get(key)
        if limiter is None:
            limiter = SlidingWindowLimiter(rule.max_events, rule.window_seconds, self._clock)
            self._limiters[key] = limiter

        if limiter.try_acquire() > 0:
            await event.answer(rule.reply)
            return None
        return await handler(event, data)
