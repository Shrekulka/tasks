import asyncio

import pytest

from services.answer_cache import AnswerCache, normalize_question
from services.background import BackgroundTasks
from utils.utils import InFlightGuard


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("  Де   ХОВАТИ горіхи?? ", "де ховати горіхи"), ("Привіт...", "привіт"), ("", "")],
)
def test_normalize_question(raw, expected):
    assert normalize_question(raw) == expected


def test_cache_hit_expiry_and_disabled():
    clock = Clock()
    cache: AnswerCache[str] = AnswerCache(ttl_seconds=10, max_entries=5, clock=clock)
    key = cache.make_key("питання", "v1")

    cache.put(key, "відповідь")
    assert cache.get(key) == "відповідь"
    clock.now += 11
    assert cache.get(key) is None

    off: AnswerCache[str] = AnswerCache(ttl_seconds=0, max_entries=5, clock=clock)
    off.put(key, "x")
    assert off.enabled is False
    assert off.get(key) is None


def test_cache_key_depends_on_parts_and_version():
    cache: AnswerCache[str] = AnswerCache(ttl_seconds=10, max_entries=5)

    first = cache.make_key("q", "prompt-1", 5)
    assert first != cache.make_key("q", "prompt-2", 5)
    assert first != cache.make_key("q", "prompt-1", 6)
    assert first == cache.make_key("Q ?", "prompt-1", 5)  # нормализация вопроса
    cache.bump()
    assert first != cache.make_key("q", "prompt-1", 5)


def test_bump_drops_everything():
    cache: AnswerCache[str] = AnswerCache(ttl_seconds=10, max_entries=5)
    key = cache.make_key("q")
    cache.put(key, "a")

    cache.bump()

    assert cache.get(key) is None
    assert cache.version == 1


def test_lru_eviction():
    cache: AnswerCache[int] = AnswerCache(ttl_seconds=10, max_entries=2)
    cache.put("a", 1)
    cache.put("b", 2)
    cache.get("a")  # «a» теперь свежее «b»
    cache.put("c", 3)

    assert cache.get("b") is None
    assert (cache.get("a"), cache.get("c")) == (1, 3)


def test_in_flight_guard():
    guard: InFlightGuard[int] = InFlightGuard()

    assert guard.try_acquire(1) is True
    assert guard.try_acquire(1) is False
    assert guard.try_acquire(2) is True
    guard.release(1)
    assert guard.try_acquire(1) is True
    guard.release(99)  # лишний release не ломает


async def test_background_tasks_keep_reference_and_finish():
    tasks = BackgroundTasks()
    done = asyncio.Event()

    async def work():
        await asyncio.sleep(0)
        done.set()

    tasks.spawn(work(), name="work")
    assert tasks.pending == 1
    await tasks.shutdown(timeout_seconds=1)

    assert done.is_set()
    assert tasks.pending == 0


async def test_background_task_error_is_logged_not_raised(caplog):
    tasks = BackgroundTasks()

    async def boom():
        raise RuntimeError("сбой")

    tasks.spawn(boom(), name="boom")
    await tasks.shutdown(timeout_seconds=1)

    assert "boom" in caplog.text


async def test_shutdown_cancels_tasks_that_do_not_finish():
    tasks = BackgroundTasks()
    cancelled = asyncio.Event()

    async def forever():
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    tasks.spawn(forever(), name="forever")
    await tasks.shutdown(timeout_seconds=0.05)

    assert cancelled.is_set()
    assert tasks.pending == 0


async def test_shutdown_reports_task_that_suppresses_cancellation():
    tasks = BackgroundTasks()
    cancellation_caught = asyncio.Event()
    release = asyncio.Event()

    async def stubborn():
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancellation_caught.set()
            await release.wait()
            return

    task = tasks.spawn(stubborn(), name="stubborn")

    try:
        with pytest.raises(RuntimeError, match="stubborn"):
            await tasks.shutdown(
                timeout_seconds=0.01,
                cancellation_timeout_seconds=0.01,
            )

        assert cancellation_caught.is_set()
        assert not task.done()
    finally:
        # Освобождаем задачу, чтобы тест сам не оставил её висеть.
        release.set()
        await asyncio.wait_for(task, timeout=1)

    await asyncio.sleep(0)
    assert tasks.pending == 0
