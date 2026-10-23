# forest-advice-bot/handlers/documents.py

"""Приём документа от зверя: проверки, подтверждение, индексация в фоне."""

import asyncio
import html
import logging
import mimetypes
from io import BytesIO
from zoneinfo import ZoneInfo

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.types import Document, Message
from aiogram.utils.chat_action import ChatActionSender

from config_data.config import Config
from errors.errors import DocumentError, FileTooLarge
from errors.user_messages import document_error_text
from filters.private_chat import PRIVATE_CHAT
from lexicon import lexicon_uk as lexicon
from models.models import SourceType
from services.background import BackgroundTasks
from services.ingestion import DocumentSource, IngestionService, Submission

log = logging.getLogger(__name__)

_MB = 1024 * 1024
_TOO_BIG_MARKER = "too big"  # так Telegram сообщает о файле, который боту не отдают


def filename_of(document: Document) -> str:
    """Имя файла. Если его нет, имя строится из идентификатора и MIME-типа."""
    if document.file_name:
        return document.file_name
    extension = mimetypes.guess_extension(document.mime_type or "") or ""
    return f"document_{document.file_unique_id}{extension}"


async def _download(bot: Bot, document: Document, limit_bytes: int) -> bytes:
    buffer = BytesIO()
    try:
        await bot.download(document, destination=buffer)
    except TelegramBadRequest as error:
        if _TOO_BIG_MARKER in str(error).lower():
            raise FileTooLarge(document.file_size or 0, limit_bytes) from error
        raise
    return buffer.getvalue()


async def _safe_send(bot: Bot, chat_id: int, text: str) -> None:
    try:
        await bot.send_message(chat_id, text)
    except TelegramAPIError:
        log.warning("Не удалось отправить сообщение в чат %d", chat_id, exc_info=True)


async def index_and_notify(
    bot: Bot,
    chat_id: int,
    ingestion: IngestionService,
    submission: Submission,
    *,
    notice_after_seconds: float,
    already_notified: bool = False,
) -> None:
    """Фоновая индексация. Ошибка сообщается всегда.

    Если индексация идёт дольше notice_after_seconds, зверь получает «обробляю», а по
    окончании «готовий». Основной порог по времени: скорость зависит от железа и модели.
    already_notified: «обробляю» уже отправлено (большой документ по размеру), тогда ждать
    порог времени не нужно, а «готовий» отправляется по окончании.
    """
    title = html.escape(submission.title, quote=False)
    job = asyncio.ensure_future(ingestion.index(submission))
    notified = already_notified
    try:
        if not notified:
            done, _ = await asyncio.wait({job}, timeout=notice_after_seconds)
            if not done:
                await _safe_send(bot, chat_id, lexicon.DOC_PROCESSING.format(title=title))
                notified = True
        ready = await job
    except asyncio.CancelledError:
        job.cancel()
        await asyncio.gather(job, return_exceptions=True)
        raise
    except Exception:  # noqa: BLE001  подробности уже в логе IngestionService
        await _safe_send(bot, chat_id, lexicon.DOC_INDEX_FAILED.format(title=title))
        return
    if ready and notified:
        await _safe_send(bot, chat_id, lexicon.DOC_READY.format(title=title))


async def handle_document(
    message: Message,
    bot: Bot,
    config: Config,
    ingestion: IngestionService,
    tasks: BackgroundTasks,
) -> None:
    document = message.document
    user = message.from_user
    if document is None or user is None:
        return

    timezone = ZoneInfo(config.tg_bot.display_timezone)
    limit_bytes = config.rag.max_file_mb * _MB
    try:
        filename = filename_of(document)
        ingestion.validate_upload(filename, document.file_size)  # до скачивания
        # Скачивание и разбор большого файла занимают время: показываем, что бот работает.
        async with ChatActionSender.upload_document(bot=bot, chat_id=message.chat.id):
            data = await _download(bot, document, limit_bytes)
            source = DocumentSource(
                source_type=SourceType.USER,
                filename=filename,
                uploader_tg_id=user.id,
                uploader_name=user.full_name,
                uploader_username=user.username,
            )
            submission = await ingestion.submit(data, source)
    except DocumentError as error:
        log.info("Документ отклонён: %s", type(error).__name__)
        await message.answer(
            document_error_text(error, max_mb=config.rag.max_file_mb, timezone=timezone)
        )
        return

    title = html.escape(submission.title, quote=False)
    text = lexicon.DOC_ACCEPTED.format(title=title)
    if message.caption:  # подпись не считается вопросом
        text += " " + lexicon.DOC_CAPTION_NOTE
    await message.answer(text)

    threshold = config.rag.large_document_chars
    large = threshold > 0 and sum(len(seg.text) for seg in submission.segments) >= threshold
    if large:  # по размеру ясно заранее: предупреждаем сразу, не дожидаясь порога по времени
        await message.answer(lexicon.DOC_PROCESSING.format(title=title))
    tasks.spawn(
        index_and_notify(
            bot,
            message.chat.id,
            ingestion,
            submission,
            notice_after_seconds=config.rag.processing_notice_after_seconds,
            already_notified=large,
        ),
        name=f"index-{submission.document_id}",
    )


def create_router() -> Router:
    router = Router(name="documents")
    router.message.filter(PRIVATE_CHAT)
    router.message.register(handle_document, F.document, flags={"throttle": "uploads"})
    return router
