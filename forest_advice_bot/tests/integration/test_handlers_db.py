"""Сквозные сценарии через диспетчер: настоящая БД, настоящие сервисы, поддельные Telegram и LLM."""

import dataclasses
import os
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select, text

import bot as bot_module
from config_data.config import load_config
from database.database import (
    create_engine,
    create_session_factory,
    ensure_vector_extension,
    init_db,
)
from keyboards.keyboard_utils import MyDocsCb
from lexicon import lexicon_uk as lexicon
from models.models import DocStatus, Document, SourceType
from services.answer_cache import AnswerCache
from services.answering import Answer, AnswerService
from services.background import BackgroundTasks
from services.ingestion import IngestionService
from services.retrieval import Retriever
from tests.fake_embedder import HashEmbedder
from tests.fake_openrouter import make_llm_config
from tests.fake_telegram import FakeTelegram, Updates, make_bot
from utils.utils import InFlightGuard

pytestmark = pytest.mark.integration


class ScriptedLLM:
    def __init__(self):
        self.calls = 0

    async def ask(self, messages, *, validate=None, options=None, retry_messages=None):
        self.calls += 1
        reply = '{"found": true, "answer": "Ховайте горіхи в кількох місцях.", "used": [1]}'
        assert validate is None or validate(reply)
        return SimpleNamespace(text=reply, model="m:free")


@pytest.fixture
async def session_factory():
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("Переменная TEST_DATABASE_URL не задана")
    await ensure_vector_extension(url)
    engine = create_engine(url)
    await init_db(engine)
    async with engine.begin() as connection:
        await connection.execute(
            text("TRUNCATE chunks, documents, app_meta RESTART IDENTITY CASCADE")
        )
    try:
        yield create_session_factory(engine)
    finally:
        await engine.dispose()


@pytest.fixture
def rig(session_factory, valid_env, no_dotenv):
    base = load_config(no_dotenv)
    config = dataclasses.replace(
        base,
        rag=dataclasses.replace(
            base.rag, relevance_threshold=0.8, embedding_dim=64, chunk_size=300, chunk_overlap=50
        ),
        limits=dataclasses.replace(base.limits, mydocs_page_size=2),
    )
    embedder = HashEmbedder()
    cache: AnswerCache[Answer] = AnswerCache(ttl_seconds=3600, max_entries=50)
    llm = ScriptedLLM()
    ingestion = IngestionService(session_factory, embedder, config.rag, on_change=cache.bump)
    answers = AnswerService(
        Retriever(session_factory, embedder, config.rag),
        llm,
        rag=config.rag,
        llm_config=make_llm_config("http://x"),
        timezone=config.tg_bot.display_timezone,
        cache=cache,
    )
    tasks = BackgroundTasks()
    services = {
        "ingestion": ingestion,
        "answers": answers,
        "cache": cache,
        "tasks": tasks,
        "question_guard": InFlightGuard[int](),
    }
    telegram = FakeTelegram()
    return SimpleNamespace(
        tg=telegram,
        bot=make_bot(telegram),
        dp=bot_module.build_dispatcher(config, session_factory, services),
        up=Updates(),
        tasks=tasks,
        llm=llm,
        cache=cache,
        sf=session_factory,
    )


async def feed(rig, update):
    await rig.dp.feed_update(rig.bot, update)
    await rig.tasks.shutdown(5)


async def upload(rig, file_id, filename, content, **user):
    rig.tg.files[file_id] = content.encode()
    await feed(rig, rig.up.document(file_id, filename, **user))


async def doc_ids(rig, owner=None):
    async with rig.sf() as session:
        query = select(Document.id).order_by(Document.id)
        if owner is not None:
            query = query.where(Document.uploader_tg_id == owner)
        return list((await session.scalars(query)).all())


def cb(action, doc_id=0, page=1):
    return MyDocsCb(action=action, doc_id=doc_id, page=page).pack()


# ---------------------------------------------------------------- загрузка и ответ


async def test_upload_then_question_names_author_and_date(rig):
    await upload(rig, "f1", "zyma.txt", "Горіхи ховайте у кількох місцях узимку.")

    await feed(rig, rig.up.text("Де ховати горіхи узимку?"))

    assert rig.tg.texts()[0] == lexicon.DOC_ACCEPTED.format(title="zyma")
    answer = rig.tg.texts()[1]
    assert answer.startswith("Ховайте горіхи в кількох місцях.")
    assert "• «zyma» — додано: Їжак (@hedgehog), " in answer


async def test_upload_document_becomes_ready(rig):
    await upload(rig, "f1", "zyma.txt", "Горіхи ховайте у кількох місцях узимку.")

    async with rig.sf() as session:
        (status,) = (await session.scalars(select(Document.status))).all()
    assert status is DocStatus.READY


async def test_second_user_uploading_same_file_is_told_who_added_it(rig):
    await upload(rig, "f1", "a.txt", "Горіхи ховайте у кількох місцях.")

    await upload(rig, "f2", "b.txt", "Горіхи ховайте у кількох місцях.", user_id=7, name="Борсук")

    assert rig.tg.texts()[-1].startswith("Цей документ уже є в зібранні — додано: Їжак (@hedgehog)")
    assert len(await doc_ids(rig)) == 1


async def test_repeated_question_uses_cache_until_collection_changes(rig):
    await upload(rig, "f1", "a.txt", "Горіхи ховайте у кількох місцях узимку.")

    await feed(rig, rig.up.text("Де ховати горіхи узимку?"))
    await feed(rig, rig.up.text("де ховати горіхи узимку?"))
    assert rig.llm.calls == 1

    await upload(rig, "f2", "b.txt", "Інша порада про горіхи узимку.")
    await feed(rig, rig.up.text("Де ховати горіхи узимку?"))

    assert rig.llm.calls == 2


async def test_unrelated_question_gets_no_answer_without_llm(rig):
    await upload(rig, "f1", "a.txt", "Горіхи ховайте у кількох місцях узимку.")

    await feed(rig, rig.up.text("квантова хромодинаміка нейтрино"))

    assert rig.tg.texts()[-1] == lexicon.NO_ANSWER
    assert rig.llm.calls == 0


# ---------------------------------------------------------------- /mydocs


async def test_mydocs_empty(rig):
    await feed(rig, rig.up.text("/mydocs"))

    assert rig.tg.texts() == [lexicon.NO_DOCS]


async def test_mydocs_lists_only_own_documents_with_status(rig):
    await upload(rig, "f1", "mine.txt", "Моя порада про зиму.")
    await upload(rig, "f2", "theirs.txt", "Чужа порада про літо.", user_id=7, name="Борсук")

    await feed(rig, rig.up.text("/mydocs"))

    listing = rig.tg.texts()[-1]
    assert "«mine»" in listing and "готовий" in listing
    assert "theirs" not in listing


async def test_mydocs_pagination(rig):
    for number in range(3):
        await upload(rig, f"f{number}", f"doc{number}.txt", f"Порада номер {number} про зиму.")

    await feed(rig, rig.up.text("/mydocs"))
    first_page = rig.tg.sent[-1]
    assert "сторінка 1 з 2" in first_page.text
    buttons = [b.callback_data for row in first_page.reply_markup.inline_keyboard for b in row]
    assert cb("page", page=2) in buttons

    await feed(rig, rig.up.callback(cb("page", page=2)))

    assert "сторінка 2 з 2" in rig.tg.edits[-1].text
    assert rig.tg.callback_answers[-1].text is None


async def test_delete_flow_removes_document_and_invalidates_cache(rig):
    await upload(rig, "f1", "zyma.txt", "Горіхи ховайте у кількох місцях узимку.")
    (doc_id,) = await doc_ids(rig)
    await feed(rig, rig.up.text("Де ховати горіхи узимку?"))

    await feed(rig, rig.up.callback(cb("del", doc_id)))
    assert lexicon.CONFIRM_DELETE.format(title="zyma") == rig.tg.edits[-1].text
    await feed(rig, rig.up.callback(cb("delok", doc_id)))

    assert await doc_ids(rig) == []
    assert rig.tg.edits[-1].text == lexicon.NO_DOCS
    assert rig.tg.callback_answers[-1].text == lexicon.DOC_DELETED.format(title="zyma")
    calls_before = rig.llm.calls
    await feed(rig, rig.up.text("Де ховати горіхи узимку?"))
    assert rig.tg.texts()[-1] == lexicon.NO_ANSWER  # старый ответ из кэша не вернулся
    assert rig.llm.calls == calls_before


async def test_cancel_returns_to_the_list_and_keeps_document(rig):
    await upload(rig, "f1", "zyma.txt", "Горіхи ховайте у кількох місцях узимку.")
    (doc_id,) = await doc_ids(rig)

    await feed(rig, rig.up.callback(cb("del", doc_id)))
    await feed(rig, rig.up.callback(cb("delno")))

    assert await doc_ids(rig) == [doc_id]
    assert "Твої документи" in rig.tg.edits[-1].text


async def test_foreign_user_cannot_delete_with_forged_callback(rig):
    await upload(rig, "f1", "mine.txt", "Моя порада про зиму.")
    (doc_id,) = await doc_ids(rig)

    await feed(rig, rig.up.callback(cb("del", doc_id), user_id=7, name="Хитрун"))
    await feed(rig, rig.up.callback(cb("delok", doc_id), user_id=7, name="Хитрун"))

    assert await doc_ids(rig) == [doc_id]
    assert [a.text for a in rig.tg.callback_answers] == [lexicon.FOREIGN_DOC] * 2
    assert all(a.show_alert for a in rig.tg.callback_answers)


async def test_archive_documents_are_neither_listed_nor_deletable(rig):
    async with rig.sf() as session:
        session.add(
            Document(
                sha256="a" * 64,
                title="archive-doc",
                filename="a.txt",
                source_type=SourceType.ARCHIVE,
                source_path="a.txt",
                size_bytes=1,
                format="txt",
                status=DocStatus.READY,
            )
        )
        await session.commit()
        archive_id = await session.scalar(select(Document.id))

    await feed(rig, rig.up.text("/mydocs"))
    await feed(rig, rig.up.callback(cb("delok", archive_id)))

    assert rig.tg.texts() == [lexicon.NO_DOCS]
    async with rig.sf() as session:
        assert await session.scalar(select(func.count()).select_from(Document)) == 1


async def test_deleting_last_document_on_page_clamps_to_previous_page(rig):
    for number in range(3):
        await upload(rig, f"f{number}", f"doc{number}.txt", f"Порада номер {number} про зиму.")
    ids = await doc_ids(rig)

    # самый новый документ первым: удаляем его со второй страницы через подделанную страницу 5
    await feed(rig, rig.up.callback(cb("delok", ids[0], page=5)))

    assert "сторінка" in rig.tg.edits[-1].text
    assert len(await doc_ids(rig)) == 2
