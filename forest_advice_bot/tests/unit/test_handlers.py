"""Хендлеры через настоящий диспетчер на заглушках сервисов (без базы данных)."""

import asyncio
import dataclasses
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

import bot as bot_module
from config_data.config import load_config
from errors.errors import (
    DuplicateDocument,
    EmptyDocument,
    FileTooLarge,
    LLMAccountBlocked,
    LLMQuotaExceeded,
    LLMUnavailable,
    UnreadableDocument,
    UnsupportedFormat,
)
from lexicon import lexicon_uk as lexicon
from services.answering import Answer
from services.background import BackgroundTasks
from services.ingestion import Submission
from services.parsers import Segment
from tests.fake_telegram import FakeTelegram, Updates, make_bot
from utils.utils import InFlightGuard


class FakeAnswers:
    def __init__(self, result=None):
        self.result = result or Answer(text="Порада.", found=True, sources=1, model="m:free")
        self.questions = []
        self.gate: asyncio.Event | None = None

    async def answer(self, question):
        self.questions.append(question)
        if self.gate is not None:
            await self.gate.wait()
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class FakeIngestion:
    def __init__(self):
        self.validate_error = None
        self.submit_error = None
        self.index_error = None
        self.submitted = []
        self.indexed = []
        self.text_len = 100
        self.submit_delay = 0.0
        self.index_delay = 0.0

    def validate_upload(self, filename, size):
        if self.validate_error:
            raise self.validate_error

    async def submit(self, data, source):
        if self.submit_delay:
            await asyncio.sleep(self.submit_delay)
        if self.submit_error:
            raise self.submit_error
        self.submitted.append((data, source))
        segments = [Segment(text="x" * self.text_len)]
        return Submission(
            document_id=len(self.submitted),
            title=source.filename.rsplit(".", 1)[0],
            segments=segments,
        )

    async def index(self, submission):
        if self.index_delay:
            await asyncio.sleep(self.index_delay)
        if self.index_error:
            raise self.index_error
        self.indexed.append(submission.document_id)
        return True


@pytest.fixture
def config(valid_env, no_dotenv):
    valid_env.setenv("PROCESSING_NOTICE_AFTER_SECONDS", "0.02")  # тесты не ждут по-настоящему
    return load_config(no_dotenv)


@pytest.fixture
def rig(config):
    telegram = FakeTelegram()
    answers, ingestion, tasks = FakeAnswers(), FakeIngestion(), BackgroundTasks()
    services = {
        "answers": answers,
        "ingestion": ingestion,
        "tasks": tasks,
        "question_guard": InFlightGuard[int](),
        "cache": SimpleNamespace(bump=lambda: None),
    }
    dispatcher = bot_module.build_dispatcher(config, None, services)
    return SimpleNamespace(
        tg=telegram,
        bot=make_bot(telegram),
        dp=dispatcher,
        answers=answers,
        ingestion=ingestion,
        tasks=tasks,
        up=Updates(),
        config=config,
    )


async def feed(rig, update):
    await rig.dp.feed_update(rig.bot, update)


# ---------------------------------------------------------------- вопросы


async def test_question_gets_answer(rig):
    await feed(rig, rig.up.text("Де ховати горіхи?"))

    assert rig.tg.texts() == ["Порада."]
    assert rig.answers.questions == ["Де ховати горіхи?"]


async def test_typing_indicator_is_shown_while_answer_is_slow(rig):
    rig.answers.gate = asyncio.Event()
    task = asyncio.create_task(feed(rig, rig.up.text("питання")))
    await asyncio.sleep(0.05)

    assert "typing" in rig.tg.actions
    rig.answers.gate.set()
    await task


async def test_long_answer_is_split_under_telegram_limit(rig):
    rig.answers.result = Answer(text="\n".join(["рядок порад"] * 1000), found=True, sources=0)

    await feed(rig, rig.up.text("питання"))

    assert len(rig.tg.sent) > 1
    assert all(len(item.text) <= 4096 for item in rig.tg.sent)


async def test_too_long_question_is_refused_without_answering(rig):
    limit = rig.config.limits.max_question_chars

    await feed(rig, rig.up.text("я" * (limit + 1)))

    assert rig.tg.texts() == [lexicon.QUESTION_TOO_LONG.format(limit=limit)]
    assert rig.answers.questions == []


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (LLMQuotaExceeded(60, daily=True), lexicon.LLM_DAILY_LIMIT),
        (LLMQuotaExceeded(60), lexicon.LLM_RATE_LIMIT),
        (LLMUnavailable("x"), lexicon.LLM_UNAVAILABLE),
        (LLMAccountBlocked(900), lexicon.LLM_UNAVAILABLE),
    ],
)
async def test_llm_errors_become_friendly_messages(rig, error, expected):
    rig.answers.result = error

    await feed(rig, rig.up.text("питання"))

    assert rig.tg.texts() == [expected]


async def test_second_question_while_first_is_running_is_refused(rig):
    rig.answers.gate = asyncio.Event()
    first = asyncio.create_task(feed(rig, rig.up.text("перше")))
    await asyncio.sleep(0.05)

    await feed(rig, rig.up.text("друге"))
    rig.answers.gate.set()
    await first

    assert lexicon.QUESTION_BUSY in rig.tg.texts()
    assert rig.answers.questions == ["перше"]


async def test_guard_is_released_after_error(rig):
    rig.answers.result = LLMUnavailable("x")
    await feed(rig, rig.up.text("перше"))
    rig.answers.result = Answer(text="Порада.", found=True, sources=0)

    await feed(rig, rig.up.text("друге"))

    assert rig.tg.texts()[-1] == "Порада."


async def test_questions_are_throttled_per_user(rig, config):
    limit = config.limits.questions_per_minute
    for number in range(limit):
        await feed(rig, rig.up.text(f"питання {number}"))

    await feed(rig, rig.up.text("зайве"))
    await feed(rig, rig.up.text("інший звір", user_id=7))  # у другого зверя свой лимит

    assert rig.tg.texts()[limit] == lexicon.QUESTIONS_THROTTLED
    assert rig.tg.texts()[limit + 1] == "Порада."
    assert len(rig.answers.questions) == limit + 1


# ---------------------------------------------------------------- документы


async def test_document_is_accepted_and_indexed_in_background(rig):
    rig.tg.files["f1"] = b"text of the document"

    await feed(rig, rig.up.document("f1", "winter-tips.txt"))
    await rig.tasks.shutdown(1)

    assert rig.tg.texts() == [lexicon.DOC_ACCEPTED.format(title="winter-tips")]
    data, source = rig.ingestion.submitted[0]
    assert data == b"text of the document"
    assert (source.uploader_tg_id, source.uploader_name, source.uploader_username) == (
        42,
        "Їжак",
        "hedgehog",
    )
    assert rig.ingestion.indexed == [1]


async def test_caption_is_not_a_question(rig):
    rig.tg.files["f1"] = b"x"

    await feed(rig, rig.up.document("f1", "a.txt", caption="Де ховати горіхи?"))
    await rig.tasks.shutdown(1)

    assert rig.tg.texts()[0].endswith(lexicon.DOC_CAPTION_NOTE)
    assert rig.answers.questions == []


async def test_title_is_html_escaped_in_confirmation(rig):
    rig.tg.files["f1"] = b"x"

    await feed(rig, rig.up.document("f1", "<b>x</b>.txt"))
    await rig.tasks.shutdown(1)

    assert "<b>" not in rig.tg.texts()[0]


async def test_slow_indexing_gets_progress_and_ready_messages(rig):
    rig.tg.files["f1"] = b"x"
    rig.ingestion.index_delay = 0.2

    await feed(rig, rig.up.document("f1", "guide.txt"))
    await rig.tasks.shutdown(2)

    assert rig.tg.texts() == [
        lexicon.DOC_ACCEPTED.format(title="guide"),
        lexicon.DOC_PROCESSING.format(title="guide"),
        lexicon.DOC_READY.format(title="guide"),
    ]


async def test_large_document_by_size_gets_notice_at_once_and_ready_after(rig):
    rig.tg.files["f1"] = b"x"
    rig.ingestion.text_len = rig.config.rag.large_document_chars  # ровно на пороге
    rig.ingestion.index_delay = 0  # быстрая индексация: порог времени не сработает

    await feed(rig, rig.up.document("f1", "guide.txt"))
    await rig.tasks.shutdown(2)

    assert rig.tg.texts() == [
        lexicon.DOC_ACCEPTED.format(title="guide"),
        lexicon.DOC_PROCESSING.format(title="guide"),
        lexicon.DOC_READY.format(title="guide"),
    ]


async def test_already_notified_skips_time_wait_and_sends_only_ready():
    from handlers.documents import index_and_notify

    sent = []

    class Bot:
        async def send_message(self, chat_id, text, **kwargs):
            sent.append(text)

    class Fast:
        async def index(self, submission):
            return True

    await index_and_notify(
        Bot(), 1, Fast(), SimpleNamespace(title="t"), notice_after_seconds=30, already_notified=True
    )

    assert sent == [lexicon.DOC_READY.format(title="t")]  # без «обробляю» и без ожидания 30 с


async def test_upload_shows_chat_action_while_downloading(rig):
    rig.tg.files["f1"] = b"x"
    rig.ingestion.submit_delay = 0.1  # действие уходит, пока обработка идёт

    await feed(rig, rig.up.document("f1", "note.txt"))
    await rig.tasks.shutdown(2)

    assert "upload_document" in rig.tg.actions


async def test_fast_indexing_sends_no_extra_messages(rig):
    rig.tg.files["f1"] = b"x"

    await feed(rig, rig.up.document("f1", "note.txt"))
    await rig.tasks.shutdown(2)

    assert rig.tg.texts() == [lexicon.DOC_ACCEPTED.format(title="note")]


async def test_indexing_failure_is_reported(rig):
    rig.tg.files["f1"] = b"x"
    rig.ingestion.index_error = RuntimeError("embedder died")

    await feed(rig, rig.up.document("f1", "a.txt"))
    await rig.tasks.shutdown(1)

    assert rig.tg.texts()[-1] == lexicon.DOC_INDEX_FAILED.format(title="a")


@pytest.mark.parametrize(
    ("where", "error", "expected_part"),
    [
        ("validate_error", UnsupportedFormat("extension", ".exe"), "формат"),
        ("validate_error", FileTooLarge(30 * 1024 * 1024, 20 * 1024 * 1024), "завеликий"),
        ("submit_error", UnsupportedFormat("mismatch", ".pdf"), "Вміст файлу"),
        ("submit_error", EmptyDocument("пусто"), "скан"),
        ("submit_error", UnreadableDocument("битый"), "прочитати"),
    ],
)
async def test_rejections_explain_the_reason_and_save_nothing(rig, where, error, expected_part):
    rig.tg.files["f1"] = b"x"
    setattr(rig.ingestion, where, error)

    await feed(rig, rig.up.document("f1", "a.txt"))

    assert len(rig.tg.sent) == 1
    assert expected_part in rig.tg.texts()[0]
    assert rig.ingestion.submitted == []
    assert rig.tasks.pending == 0


async def test_unsupported_extension_message_lists_formats(rig):
    rig.ingestion.validate_error = UnsupportedFormat("extension", ".exe")

    await feed(rig, rig.up.document("f1", "virus.exe"))

    assert "DOCX, PDF, TXT" in rig.tg.texts()[0]


async def test_file_too_big_for_telegram_download(rig):
    rig.tg.too_big.add("f1")

    await feed(rig, rig.up.document("f1", "big.pdf", size=None))

    assert rig.tg.texts() == [lexicon.FILE_TOO_LARGE.format(max_mb=rig.config.rag.max_file_mb)]


async def test_duplicate_of_user_document_names_author_and_kyiv_time(rig):
    rig.tg.files["f1"] = b"x"
    rig.ingestion.submit_error = DuplicateDocument(
        title="tips",
        is_archive=False,
        uploader_name="Борсук",
        uploader_username="badger",
        uploaded_at=datetime(2026, 10, 12, 11, 30, tzinfo=UTC),
    )

    await feed(rig, rig.up.document("f1", "a.txt"))

    assert rig.tg.texts() == [
        lexicon.DUPLICATE_USER.format(author="Борсук (@badger)", when="12.10.2026 14:30")
    ]


async def test_duplicate_of_archive_document(rig):
    rig.tg.files["f1"] = b"x"
    rig.ingestion.submit_error = DuplicateDocument(
        title="zymovi-zapasy",
        is_archive=True,
        uploader_name=None,
        uploader_username=None,
        uploaded_at=None,
    )

    await feed(rig, rig.up.document("f1", "a.txt"))

    assert rig.tg.texts() == [lexicon.DUPLICATE_ARCHIVE.format(title="zymovi-zapasy")]


async def test_document_without_name_gets_generated_one(rig):
    rig.tg.files["f1"] = b"x"

    await feed(rig, rig.up.document("f1", None, mime="application/pdf"))
    await rig.tasks.shutdown(1)

    assert rig.ingestion.submitted[0][1].filename == "document_u-f1.pdf"


async def test_uploads_are_throttled(rig, config):
    limit = config.limits.uploads_per_hour
    for number in range(limit):
        rig.tg.files[f"f{number}"] = b"x"
        await feed(rig, rig.up.document(f"f{number}", f"a{number}.txt"))
    rig.tg.files["extra"] = b"x"

    await feed(rig, rig.up.document("extra", "extra.txt"))
    await rig.tasks.shutdown(1)

    assert rig.tg.texts()[-1] == lexicon.UPLOADS_THROTTLED
    assert len(rig.ingestion.submitted) == limit


# ---------------------------------------------------------------- группы, команды, не-текст


async def test_group_command_gets_hint_but_plain_chatter_is_ignored(rig):
    await feed(rig, rig.up.text("/start", chat_type="group", chat_id=-100))
    await feed(rig, rig.up.text("Де ховати горіхи?", chat_type="supergroup", chat_id=-100))

    assert rig.tg.texts() == [lexicon.GROUP_HINT]
    assert rig.answers.questions == []


async def test_group_document_is_not_processed(rig):
    rig.tg.files["f1"] = b"x"

    await feed(rig, rig.up.document("f1", "a.txt", chat_type="group", chat_id=-100))

    assert rig.tg.texts() == []
    assert rig.ingestion.submitted == []


async def test_unknown_command_hints_help(rig):
    await feed(rig, rig.up.text("/dance"))

    assert rig.tg.texts() == [lexicon.UNKNOWN_COMMAND]


@pytest.mark.parametrize("kind", ["voice", "photo", "sticker"])
async def test_non_text_messages_get_polite_explanation(rig, kind):
    await feed(rig, getattr(rig.up, kind)())

    assert rig.tg.texts() == [lexicon.NON_TEXT.format(formats="DOCX, PDF, TXT")]


async def test_start_and_help_still_work_and_help_lists_formats(rig):
    await feed(rig, rig.up.text("/start"))
    await feed(rig, rig.up.text("/help"))

    assert rig.tg.texts()[0] == lexicon.START
    assert "DOCX, PDF, TXT" in rig.tg.texts()[1]
    assert "/mydocs" in rig.tg.texts()[1]


# ---------------------------------------------------------------- ошибки


async def test_unexpected_error_does_not_leave_the_user_without_answer(rig):
    rig.answers.result = RuntimeError("секретная деталь")

    await feed(rig, rig.up.text("питання"))

    assert rig.tg.texts() == [lexicon.UNEXPECTED_ERROR]
    assert "секретная" not in rig.tg.texts()[0]


async def test_database_error_gets_its_own_message(rig):
    from sqlalchemy.exc import OperationalError

    rig.answers.result = OperationalError("SELECT 1", {}, Exception("connection refused"))

    await feed(rig, rig.up.text("питання"))

    assert rig.tg.texts() == [lexicon.DB_UNAVAILABLE]


def test_config_is_a_dataclass(config):
    assert dataclasses.is_dataclass(config)


async def test_blocked_bot_during_indexing_does_not_break_the_job():
    import asyncio

    from aiogram.exceptions import TelegramForbiddenError
    from aiogram.methods import SendMessage

    from handlers.documents import index_and_notify

    indexed = []

    class Slow:
        async def index(self, submission):
            await asyncio.sleep(0.05)
            indexed.append(submission.title)
            return True

    class BlockedBot:
        async def send_message(self, *args, **kwargs):
            raise TelegramForbiddenError(SendMessage(chat_id=1, text="x"), "bot was blocked")

    await index_and_notify(
        BlockedBot(), 1, Slow(), SimpleNamespace(title="t"), notice_after_seconds=0.0
    )

    assert indexed == ["t"]  # исключение Telegram не вылетело и индексация завершилась


async def test_cancelled_notifier_waits_for_indexing_job_cleanup():
    import asyncio

    from handlers.documents import index_and_notify

    started = asyncio.Event()
    cleanup_started = asyncio.Event()
    allow_cleanup = asyncio.Event()
    cleanup_finished = asyncio.Event()

    class Slow:
        async def index(self, submission):
            started.set()
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                cleanup_started.set()
                await allow_cleanup.wait()
                cleanup_finished.set()
                raise

    class Bot:
        async def send_message(self, *args, **kwargs):
            return None

    task = asyncio.create_task(
        index_and_notify(
            Bot(),
            1,
            Slow(),
            SimpleNamespace(title="t"),
            notice_after_seconds=30,
        )
    )

    await asyncio.wait_for(started.wait(), timeout=1)
    task.cancel()

    try:
        await asyncio.wait_for(cleanup_started.wait(), timeout=1)
        outer_finished_before_cleanup = task.done()
    finally:
        # Не оставляем тестовую корутину на ожидании при ошибке проверки.
        allow_cleanup.set()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=1)

    await asyncio.wait_for(cleanup_finished.wait(), timeout=1)

    assert not outer_finished_before_cleanup
    assert cleanup_finished.is_set()
