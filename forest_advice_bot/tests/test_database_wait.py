"""wait_for_database не требует настоящей базы: проверяем, что без неё он сдаётся."""

import pytest

from database.database import wait_for_database
from errors.errors import DatabaseUnavailable


async def test_wait_for_database_gives_up_when_nothing_listens():
    # Порт 1 никто не слушает: подключение сразу отклоняется.
    url = "postgresql+asyncpg://user:pass@127.0.0.1:1/none"
    with pytest.raises(DatabaseUnavailable):
        await wait_for_database(url, attempts=2, delay=0.01)
