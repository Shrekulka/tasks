from datetime import UTC, datetime, timedelta

import pytest

from external_services.llm.quota import DailyQuota, QuotaTracker, parse_daily_quota

FIELD = "free_model_daily_requests"


def _payload(**values):
    return {"data": {FIELD: values}}


def test_parse_ok():
    quota = parse_daily_quota(_payload(limit=50, used=3, remaining=47), FIELD)

    assert quota == DailyQuota(limit=50, used=3, remaining=47)


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"data": {}},
        {"data": {FIELD: None}},
        _payload(limit=50, used=3),
        _payload(limit="50", used=3, remaining=47),
        _payload(limit=True, used=3, remaining=47),
    ],
)
def test_parse_returns_none_when_not_reported(payload):
    assert parse_daily_quota(payload, FIELD) is None


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def _tracker(clock, *, fallback=0, utc=None):
    moment = utc or datetime(2026, 10, 20, 23, 0, tzinfo=UTC)
    return QuotaTracker(
        ttl_seconds=30, fallback_limit=fallback, clock=clock, utc_now=lambda: moment
    )


def test_nothing_known_means_not_exhausted():
    tracker = _tracker(Clock())

    assert tracker.is_fresh() is False
    assert tracker.exhausted() is False


def test_provider_quota_is_consumed_and_expires():
    clock = Clock()
    tracker = _tracker(clock)
    tracker.observe(DailyQuota(limit=50, used=48, remaining=2))

    assert tracker.is_fresh() is True
    tracker.consume()
    assert tracker.exhausted() is False
    tracker.consume()
    assert tracker.exhausted() is True
    tracker.consume()  # не уходит в минус
    assert tracker.exhausted() is True

    clock.now += 31
    assert tracker.is_fresh() is False


def test_fallback_counter_used_when_provider_silent():
    tracker = _tracker(Clock(), fallback=2)
    tracker.observe(None)

    tracker.consume()
    assert tracker.exhausted() is False
    tracker.consume()
    assert tracker.exhausted() is True


def test_fallback_counter_resets_on_new_utc_day():
    days = [datetime(2026, 10, 20, 12, 0, tzinfo=UTC)]
    tracker = QuotaTracker(ttl_seconds=30, fallback_limit=1, clock=Clock(), utc_now=lambda: days[0])
    tracker.observe(None)
    tracker.consume()
    assert tracker.exhausted() is True

    days[0] += timedelta(days=1)

    assert tracker.exhausted() is False


def test_seconds_until_reset():
    tracker = _tracker(Clock(), utc=datetime(2026, 10, 20, 23, 0, tzinfo=UTC))

    assert tracker.seconds_until_reset() == 3600.0
