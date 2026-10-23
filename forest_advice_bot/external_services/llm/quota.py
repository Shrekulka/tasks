# forest-advice-bot/external_services/llm/quota.py

"""Дневная квота бесплатных запросов. Разбор ответа провайдера и учёт остатка."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from utils.utils import Clock


@dataclass(frozen=True, slots=True)
class DailyQuota:
    limit: int
    used: int
    remaining: int


def parse_daily_quota(payload: Mapping[str, Any], field_name: str) -> DailyQuota | None:
    """None, если провайдер не сообщает квоту: тогда работает запасной локальный счётчик.

    field_name задаёт адаптер (для OpenRouter это free_model_daily_requests).
    """
    data = payload.get("data", payload)
    block = data.get(field_name) if isinstance(data, Mapping) else None
    if not isinstance(block, Mapping):
        return None
    values: list[int] = []
    for key in ("limit", "used", "remaining"):
        value = block.get(key)
        if isinstance(value, bool) or not isinstance(value, int):
            return None
        values.append(value)
    return DailyQuota(limit=values[0], used=values[1], remaining=values[2])


class QuotaTracker:
    """Помнит остаток квоты между запросами.

    Источники в порядке доверия: данные провайдера (observe), затем запасной локальный
    счётчик (fallback_limit > 0). Если нет ни того, ни другого, считаем квоту неисчерпанной
    и полагаемся на ответ 429.
    """

    def __init__(
        self,
        *,
        ttl_seconds: float,
        fallback_limit: int,
        clock: Clock,
        utc_now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._ttl = ttl_seconds
        self._fallback_limit = fallback_limit
        self._clock = clock
        self._utc_now = utc_now
        self._fetched_at: float | None = None
        self._remaining: int | None = None
        self._fallback_day = self._utc_now().date()
        self._fallback_used = 0

    def is_fresh(self) -> bool:
        return self._fetched_at is not None and self._clock() - self._fetched_at < self._ttl

    def observe(self, quota: DailyQuota | None) -> None:
        """Сохраняет ответ провайдера. None значит «провайдер квоту не сообщил»."""
        self._fetched_at = self._clock()
        self._remaining = quota.remaining if quota is not None else None

    def consume(self) -> None:
        """Учитывает успешно отправленный запрос."""
        if self._remaining is not None:
            self._remaining = max(self._remaining - 1, 0)
            return
        self._roll_fallback_day()
        self._fallback_used += 1

    def exhausted(self) -> bool:
        if self._remaining is not None:
            return self._remaining <= 0
        if self._fallback_limit > 0:
            self._roll_fallback_day()
            return self._fallback_used >= self._fallback_limit
        return False

    def seconds_until_reset(self) -> float:
        """Секунды до ближайшей полуночи UTC (дневной счётчик считается по UTC)."""
        now = self._utc_now()
        midnight = datetime.combine(now.date() + timedelta(days=1), datetime.min.time(), UTC)
        return max((midnight - now).total_seconds(), 0.0)

    def _roll_fallback_day(self) -> None:
        today = self._utc_now().date()
        if today != self._fallback_day:
            self._fallback_day = today
            self._fallback_used = 0
