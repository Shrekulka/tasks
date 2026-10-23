# forest-advice-bot/services/background.py

"""Фоновые задачи с хранением ссылок.

asyncio хранит только слабые ссылки на задачи: задача, на которую никто не ссылается, может
быть собрана сборщиком мусора на середине работы. Поэтому ссылки держим в наборе.
Исключения из задач логируются, а не теряются.
"""

import asyncio
import logging
from collections.abc import Coroutine
from typing import Any

log = logging.getLogger(__name__)


class BackgroundTasks:
    def __init__(self) -> None:
        self._tasks: set[asyncio.Task[Any]] = set()

    def spawn(self, coroutine: Coroutine[Any, Any, Any], *, name: str) -> asyncio.Task[Any]:
        task = asyncio.create_task(coroutine, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._finished)
        return task

    def _finished(self, task: asyncio.Task[Any]) -> None:
        self._tasks.discard(task)

        if task.cancelled():
            log.info("Фоновая задача %s отменена", task.get_name())
            return

        error = task.exception()
        if error is not None:
            log.error(
                "Фоновая задача %s завершилась с ошибкой",
                task.get_name(),
                exc_info=(type(error), error, error.__traceback__),
            )

    @property
    def pending(self) -> int:
        return len(self._tasks)

    async def shutdown(
        self,
        timeout_seconds: float,
        cancellation_timeout_seconds: float = 5.0,
    ) -> None:
        """Дожидается задач, отменяет оставшиеся и выявляет зависшую отмену."""
        tasks = set(self._tasks)
        if not tasks:
            return

        log.info("Ожидание фоновых задач: %d", len(tasks))
        _, pending = await asyncio.wait(tasks, timeout=timeout_seconds)

        if not pending:
            return

        log.warning(
            "Время ожидания фоновых задач истекло; отмена задач: %d",
            len(pending),
        )
        for task in pending:
            task.cancel()

        _, still_pending = await asyncio.wait(
            pending,
            timeout=cancellation_timeout_seconds,
        )

        if still_pending:
            names = ", ".join(sorted(task.get_name() for task in still_pending))
            log.critical(
                "Фоновые задачи не завершились после отмены: %s",
                names,
            )
            raise RuntimeError(f"Не удалось остановить фоновые задачи: {names}")

        log.warning("Отменено фоновых задач: %d", len(pending))
