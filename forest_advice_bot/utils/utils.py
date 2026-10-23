# forest-advice-bot/utils/utils.py

"""Мелкие чистые структуры. Время подаётся снаружи, поэтому тесты не ждут по-настоящему."""

from collections import deque
from collections.abc import Callable, Hashable

Clock = Callable[[], float]

TELEGRAM_MESSAGE_LIMIT = 4096  # предел длины одного сообщения в Telegram Bot API


class TTLSet[K: Hashable]:
    """Множество ключей с временем жизни: карантины ключей, моделей, пар."""

    def __init__(self, clock: Clock) -> None:
        self._clock = clock
        self._until: dict[K, float] = {}

    def add(self, key: K, ttl_seconds: float) -> None:
        self._until[key] = self._clock() + ttl_seconds

    def __contains__(self, key: object) -> bool:
        until = self._until.get(key)  # type: ignore[arg-type]
        if until is None:
            return False
        if until <= self._clock():
            del self._until[key]  # type: ignore[arg-type]
            return False
        return True

    def active(self) -> frozenset[K]:
        now = self._clock()
        self._until = {key: until for key, until in self._until.items() if until > now}
        return frozenset(self._until)


class SlidingWindowLimiter:
    """Не больше max_events событий за window_seconds."""

    def __init__(self, max_events: int, window_seconds: float, clock: Clock) -> None:
        if max_events < 1 or window_seconds <= 0:
            raise ValueError("max_events >= 1 и window_seconds > 0")
        self._max = max_events
        self._window = window_seconds
        self._clock = clock
        self._events: deque[float] = deque()

    def try_acquire(self) -> float:
        """0.0: слот занят сейчас. Иначе сколько секунд ждать, слот не занят."""
        now = self._clock()
        while self._events and self._events[0] <= now - self._window:
            self._events.popleft()
        if len(self._events) < self._max:
            self._events.append(now)
            return 0.0
        return self._events[0] + self._window - now

    def is_idle(self) -> bool:
        """Нет событий в текущем окне: лимитер можно забыть без потери состояния."""
        now = self._clock()
        while self._events and self._events[0] <= now - self._window:
            self._events.popleft()
        return not self._events


class InFlightGuard[K: Hashable]:
    """Не даёт одному ключу (зверю) запустить второе действие, пока идёт первое."""

    def __init__(self) -> None:
        self._active: set[K] = set()

    def try_acquire(self, key: K) -> bool:
        if key in self._active:
            return False
        self._active.add(key)
        return True

    def release(self, key: K) -> None:
        self._active.discard(key)
