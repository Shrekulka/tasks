# forest-advice-bot/errors/errors.py

"""Доменные исключения проекта.

Текст для зверя выбирает обработчик ошибок по типу исключения (lexicon_uk.py), а не само
исключение: здесь нет пользовательских сообщений.
"""

from datetime import datetime


class DatabaseUnavailable(RuntimeError):
    """База данных не ответила за отведённое число попыток."""


# ---------- Документы ----------


class DocumentError(Exception):
    """Базовый класс ошибок обработки документа."""


class UnsupportedFormat(DocumentError):
    """Формат не поддерживается. reason: extension (чужое расширение) или mismatch (содержимое
    не соответствует расширению)."""

    def __init__(self, reason: str, detail: str = "") -> None:
        self.reason = reason
        super().__init__(f"{reason}: {detail}" if detail else reason)


class FileTooLarge(DocumentError):
    def __init__(self, size_bytes: int, limit_bytes: int) -> None:
        self.size_bytes = size_bytes
        self.limit_bytes = limit_bytes
        super().__init__(f"{size_bytes} bytes > limit {limit_bytes}")


class EmptyDocument(DocumentError):
    """В файле нет текста (скан, пустой файл)."""


class UnreadableDocument(DocumentError):
    """Файл повреждён, защищён паролем или слишком тяжёлый для разбора."""


class DuplicateDocument(DocumentError):
    """Документ с таким содержимым уже есть в коллекции.

    Поля описывают уже существующий документ: по ним бот называет, кто и когда его добавил.
    """

    def __init__(
        self,
        *,
        title: str,
        is_archive: bool,
        uploader_name: str | None,
        uploader_username: str | None,
        uploaded_at: datetime | None,
    ) -> None:
        self.title = title
        self.is_archive = is_archive
        self.uploader_name = uploader_name
        self.uploader_username = uploader_username
        self.uploaded_at = uploaded_at
        super().__init__(f"duplicate of «{title}»")


# ---------- LLM ----------


class LLMError(Exception):
    """Базовый класс ошибок обращения к LLM."""


class LLMUnavailable(LLMError):
    """Ответа нет по техническим причинам: нет моделей или ключей, сбои, дедлайн."""


class LLMQuotaExceeded(LLMError):
    """Исчерпан лимит запросов (минутный, дневной или общая пауза)."""

    def __init__(
        self, retry_after: float | None = None, detail: str = "", *, daily: bool = False
    ) -> None:
        self.retry_after = retry_after
        self.daily = daily  # True: исчерпана дневная квота, а не минутный лимит или пауза
        super().__init__(detail or "quota exceeded")


class LLMAccountBlocked(LLMError):
    """Аккаунт провайдера заблокирован (например, отрицательный баланс). Ретраи бесполезны."""

    def __init__(self, retry_after: float | None = None) -> None:
        self.retry_after = retry_after
        super().__init__("account blocked")
