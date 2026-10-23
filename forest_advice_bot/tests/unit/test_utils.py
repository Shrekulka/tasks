import pytest

from utils.utils import SlidingWindowLimiter, TTLSet


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def test_ttl_set_expires():
    clock = Clock()
    items: TTLSet[str] = TTLSet(clock)
    items.add("a", 10)

    assert "a" in items
    clock.now += 9.9
    assert "a" in items
    clock.now += 0.2
    assert "a" not in items
    assert items.active() == frozenset()


def test_ttl_set_active_snapshot():
    clock = Clock()
    items: TTLSet[int] = TTLSet(clock)
    items.add(1, 5)
    items.add(2, 50)
    clock.now += 10

    assert items.active() == frozenset({2})


def test_limiter_allows_up_to_max_then_reports_wait():
    clock = Clock()
    limiter = SlidingWindowLimiter(2, 60, clock)

    assert limiter.try_acquire() == 0.0
    clock.now += 10
    assert limiter.try_acquire() == 0.0
    clock.now += 10
    assert limiter.try_acquire() == pytest.approx(40.0)  # первое событие выйдет из окна через 40 с


def test_limiter_frees_slot_after_window():
    clock = Clock()
    limiter = SlidingWindowLimiter(1, 60, clock)
    limiter.try_acquire()
    clock.now += 60

    assert limiter.try_acquire() == 0.0


@pytest.mark.parametrize(("max_events", "window"), [(0, 60), (1, 0)])
def test_limiter_rejects_bad_parameters(max_events, window):
    with pytest.raises(ValueError, match="max_events"):
        SlidingWindowLimiter(max_events, window, Clock())
