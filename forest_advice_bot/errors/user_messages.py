# forest-advice-bot/errors/user_messages.py

"""Превращает доменные исключения в тексты для зверя. Единственное место такого перевода."""

from zoneinfo import ZoneInfo

from errors.errors import (
    DocumentError,
    DuplicateDocument,
    EmptyDocument,
    FileTooLarge,
    LLMError,
    LLMQuotaExceeded,
    UnreadableDocument,
    UnsupportedFormat,
)
from lexicon import lexicon_uk as lexicon
from services.parsers import supported_extensions


def formats_text() -> str:
    """«DOCX, PDF, TXT»: список строится из реально поддерживаемых расширений."""
    return ", ".join(extension.lstrip(".").upper() for extension in supported_extensions())


def llm_error_text(error: LLMError) -> str:
    if isinstance(error, LLMQuotaExceeded):
        return lexicon.LLM_DAILY_LIMIT if error.daily else lexicon.LLM_RATE_LIMIT
    # LLMUnavailable и LLMAccountBlocked: детали (баланс аккаунта) зверям не раскрываем
    return lexicon.LLM_UNAVAILABLE


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def document_error_text(error: DocumentError, *, max_mb: int, timezone: ZoneInfo) -> str:
    if isinstance(error, UnsupportedFormat):
        template = (
            lexicon.UNSUPPORTED_MISMATCH
            if error.reason == "mismatch"
            else lexicon.UNSUPPORTED_EXTENSION
        )
        return template.format(formats=formats_text())
    if isinstance(error, FileTooLarge):
        return lexicon.FILE_TOO_LARGE.format(max_mb=max_mb)
    if isinstance(error, EmptyDocument):
        return lexicon.NO_TEXT
    if isinstance(error, DuplicateDocument):
        if error.is_archive:
            return lexicon.DUPLICATE_ARCHIVE.format(title=_escape(error.title))
        author = _escape(error.uploader_name or lexicon.UNKNOWN_AUTHOR)
        if error.uploader_username:
            author += lexicon.AUTHOR_USERNAME.format(username=_escape(error.uploader_username))
        when = (
            error.uploaded_at.astimezone(timezone).strftime(lexicon.DATE_FORMAT)
            if error.uploaded_at
            else "—"
        )
        return lexicon.DUPLICATE_USER.format(author=author, when=when)
    if isinstance(error, UnreadableDocument):
        return lexicon.UNREADABLE
    return lexicon.UNEXPECTED_ERROR
