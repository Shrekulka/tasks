from __future__ import annotations


class TeremokError(Exception):
    """Базовий виняток для скрапера Teremok."""


class RobotsDeniedError(TeremokError):
    """Скрапінг заборонено правилами robots.txt або джерело недоступне."""


class FetchError(TeremokError):
    """Помилка мережевого запиту або отримання HTTP-відповіді."""


class ParseError(TeremokError):
    """Помилка структури HTML або валідації полів."""


class ValidationError(TeremokError):
    """Фінальний набір даних не відповідає обов'язковим бізнес-інваріантам."""
