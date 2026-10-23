import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import make_samples
from scripts.eval_retrieval import run_eval
from services.evaluation import (
    EvalQuestion,
    IndexReport,
    ThresholdResult,
    build_index,
    evaluate_threshold,
    format_report,
    load_questions,
    rank_candidates,
    recommend_threshold,
    sweep,
)
from tests.fake_embedder import HashEmbedder

QUESTIONS_FILE = Path(__file__).parents[1] / "eval" / "questions.json"
SHIPPED_PDF = Path(__file__).parents[2] / "samples" / "archive" / "tuman-orientyry.pdf"


def _rag(**overrides):
    values = {
        "chunk_size": 200,
        "chunk_overlap": 40,
        "top_k": 3,
        "candidate_factor": 4,
        "relevance_threshold": 0.9,
        "relevance_margin": 0.0,
        "max_pdf_pages": 50,
        "max_zip_entries": 500,
        "max_unpacked_mb": 50,
        "embedding_query_prefix": "query:",
        "embedding_passage_prefix": "passage:",
        "embedding_batch_size": 8,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _write(directory: Path, name: str, content: str) -> None:
    (directory / name).write_text(content, encoding="utf-8")


@pytest.fixture
def archive(tmp_path):
    _write(tmp_path, "nuts.txt", "Горіхи й жолуді ховайте у кількох місцях. Перевіряйте запаси.")
    _write(tmp_path, "water.txt", "Воду беріть зі струмка на світанку. Калюжі небезпечні.")
    _write(
        tmp_path, "nuts_copy.txt", "Горіхи й жолуді ховайте у кількох місцях. Перевіряйте запаси."
    )
    (tmp_path / "scan.pdf").write_bytes(b"%PDF-1.4 broken")
    return tmp_path


# ---------------------------------------------------------------- файл вопросов


def test_bundled_questions_file_is_valid_and_balanced():
    questions = load_questions(QUESTIONS_FILE)

    assert 10 <= len(questions) <= 40
    assert any(not q.answerable for q in questions)  # вопросы без ответа
    assert any(q.match == "all" for q in questions)  # противоречивые документы
    assert sum(q.answerable for q in questions) >= 8


async def test_bundled_questions_point_to_documents_that_get_indexed(tmp_path):
    """Названия в вопросах должны совпадать с теми, что получатся после удаления дублей."""
    make_samples.main(tmp_path)
    shutil.copy(
        SHIPPED_PDF, tmp_path
    )  # текстовый PDF собирается отдельным скриптом и лежит в репозитории

    index, report = await build_index(tmp_path, HashEmbedder(), _rag())

    titles = {chunk.title for chunk in index}
    for question in load_questions(QUESTIONS_FILE):
        assert set(question.expected) <= titles, question.question
    assert any("zymovi-zapasy_kopiya.txt: дубль" in item for item in report.skipped)
    assert any("skan-bez-tekstu.pdf" in item for item in report.skipped)


@pytest.mark.parametrize(
    "payload",
    [[], {}, [{"question": "q"}], [{"question": "q", "expected": [], "match": "some"}]],
)
def test_invalid_questions_file_is_rejected(tmp_path, payload):
    path = tmp_path / "q.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError):
        load_questions(path)


# ---------------------------------------------------------------- индекс


async def test_build_index_skips_duplicates_and_unreadable_files(archive):
    index, report = await build_index(archive, HashEmbedder(), _rag())

    assert report.documents == 2
    assert {c.title for c in index} == {"nuts", "water"}
    assert sorted(report.skipped) == ["nuts_copy.txt: дубль", "scan.pdf: UnreadableDocument"]
    assert report.chunks == len(index)


# ---------------------------------------------------------------- расчёт


async def _prepare(archive, questions, rag=None):
    rag = rag or _rag()
    embedder = HashEmbedder()
    index, _ = await build_index(archive, embedder, rag)
    limit = rag.top_k * rag.candidate_factor
    candidates = [
        rank_candidates(await embedder.embed_query(q.question), index, limit) for q in questions
    ]
    return candidates


async def test_ranking_prefers_the_document_with_shared_words(archive):
    questions = [EvalQuestion("Де ховати горіхи й жолуді?", ("nuts",))]

    (found,) = await _prepare(archive, questions)

    assert found[0].title == "nuts"
    assert found[0].distance < found[-1].distance
    assert [c.distance for c in found] == sorted(c.distance for c in found)


async def test_threshold_separates_answerable_from_unanswerable(archive):
    questions = [
        EvalQuestion("Де ховати горіхи й жолуді?", ("nuts",)),
        EvalQuestion("Звідки брати воду зі струмка на світанку?", ("water",)),
        EvalQuestion("квантова хромодинаміка нейтрино", ()),
    ]
    candidates = await _prepare(archive, questions)

    strict = evaluate_threshold(questions, candidates, threshold=0.0, top_k=3)
    loose = evaluate_threshold(questions, candidates, threshold=2.0, top_k=3)

    assert (strict.hit_rate, strict.reject_rate) == (0.0, 1.0)
    assert (loose.hit_rate, loose.reject_rate) == (1.0, 0.0)
    best = recommend_threshold(sweep(questions, candidates, top_k=3))
    assert best is not None
    assert best.result.hit_rate == 1.0
    assert best.result.reject_rate == 1.0


async def test_match_all_requires_every_expected_document(archive):
    both = EvalQuestion("горіхи жолуді струмка світанку", ("nuts", "water"), match="all")
    candidates = await _prepare(archive, [both])

    loose = evaluate_threshold([both], candidates, threshold=2.0, top_k=3)
    tight = evaluate_threshold([both], candidates, threshold=0.0, top_k=3)

    assert loose.outcomes[0].titles and set(loose.outcomes[0].titles) == {"nuts", "water"}
    assert loose.hit_rate == 1.0
    assert tight.hit_rate == 0.0


def _result(threshold, hit, reject):
    return ThresholdResult(threshold, hit, reject, ())


def test_recommend_is_recall_first_and_takes_the_middle_of_the_plateau():
    results = [
        _result(0.1, 0.5, 1.0),  # выше качество отклонения, но теряет вопросы с ответом
        _result(0.2, 1.0, 0.5),
        _result(0.3, 1.0, 1.0),
        _result(0.4, 1.0, 1.0),
        _result(0.5, 1.0, 1.0),
        _result(0.6, 1.0, 0.0),
    ]

    recommendation = recommend_threshold(results)

    assert (recommendation.low, recommendation.high) == (0.3, 0.5)
    assert recommendation.value == 0.4
    assert (recommendation.result.hit_rate, recommendation.result.reject_rate) == (1.0, 1.0)


def test_recommend_matches_real_e5_run_instead_of_fragile_balanced_peak():
    """Реальные числа первого запуска multilingual-e5-base на samples/archive.

    Баланс 50/50 давал порог 0.176 (диапазон шириной 0.001), который терял законный вопрос.
    Политика «полнота прежде всего» выбирает середину устойчивого диапазона.
    """
    results = [
        _result(0.113, 0.0, 1.0),
        _result(0.152, 0.69, 1.0),
        _result(0.175, 0.92, 1.0),
        _result(0.176, 0.92, 1.0),
        _result(0.182, 0.92, 0.75),
        _result(0.196, 0.92, 0.75),
        _result(0.197, 1.0, 0.75),
        _result(0.200, 1.0, 0.75),
        _result(0.209, 1.0, 0.75),
        _result(0.221, 1.0, 0.75),
        _result(0.228, 1.0, 0.25),
        _result(0.297, 1.0, 0.0),
    ]

    recommendation = recommend_threshold(results)

    assert (recommendation.low, recommendation.high) == (0.197, 0.221)
    assert recommendation.value == pytest.approx(0.209, abs=0.002)
    assert recommendation.result.hit_rate == 1.0


def test_recommend_when_full_recall_is_unreachable_takes_best_available():
    results = [_result(0.1, 0.4, 1.0), _result(0.2, 0.8, 0.5), _result(0.3, 0.8, 0.25)]

    recommendation = recommend_threshold(results)

    assert recommendation.result.hit_rate == 0.8
    assert recommendation.result.reject_rate == 0.5  # среди равных по полноте лучшее отклонение


def test_recommend_min_recall_can_be_relaxed():
    results = [_result(0.1, 0.9, 1.0), _result(0.2, 1.0, 0.0)]

    assert recommend_threshold(results, min_recall=1.0).result.threshold == 0.2
    assert recommend_threshold(results, min_recall=0.9).result.threshold == 0.1


def test_recommend_handles_empty_and_single_group():
    assert recommend_threshold([]) is None
    only_answerable = [_result(0.1, 0.5, None), _result(0.2, 1.0, None), _result(0.3, 1.0, None)]
    assert recommend_threshold(only_answerable).value == 0.2  # самый тесный порог с полнотой


def test_margin_removes_the_tail_of_weakly_related_documents():
    from services.retrieval import filter_relevant

    chunks = [
        SimpleNamespace(distance=0.10),
        SimpleNamespace(distance=0.13),
        SimpleNamespace(distance=0.19),
    ]

    assert len(filter_relevant(chunks, threshold=0.2)) == 3
    assert len(filter_relevant(chunks, threshold=0.2, margin=0.05)) == 2
    assert filter_relevant(chunks, threshold=0.05, margin=0.05) == []  # порог главнее margin
    assert filter_relevant([], threshold=0.2, margin=0.05) == []


# ---------------------------------------------------------------- сквозной запуск


async def test_run_eval_produces_markdown_report(archive, tmp_path):
    questions = tmp_path / "q.json"
    questions.write_text(
        json.dumps(
            [
                {"question": "Де ховати горіхи й жолуді?", "expected": ["nuts"]},
                {"question": "квантова хромодинаміка нейтрино", "expected": []},
            ]
        ),
        encoding="utf-8",
    )

    report = await run_eval(
        embedder=HashEmbedder(), rag=_rag(), archive=archive, questions_path=questions
    )

    assert "| Вопрос | Ожидается |" in report
    assert "Де ховати горіхи й жолуді?" in report
    assert "**Рекомендация:** `RELEVANCE_THRESHOLD=" in report
    assert "nuts_copy.txt: дубль" in report


async def test_run_eval_on_empty_folder_exits_with_message(tmp_path):
    questions = tmp_path / "q.json"
    questions.write_text(json.dumps([{"question": "q", "expected": []}]), encoding="utf-8")
    empty = tmp_path / "empty"
    empty.mkdir()

    with pytest.raises(SystemExit):
        await run_eval(embedder=HashEmbedder(), rag=_rag(), archive=empty, questions_path=questions)


def test_report_without_recommendation():
    from services.evaluation import IndexReport

    report = format_report(
        index_report=IndexReport(),
        current=_result(0.2, None, None),
        results=[],
        recommendation=None,
        top_k=5,
    )

    assert "Порог подобрать не удалось" in report


async def test_cli_passes_margin_and_min_recall_to_the_evaluation(
    archive, tmp_path, monkeypatch, capsys
):
    """Регрессия: флаги --margin и --min-recall разбирались, но не доходили до расчёта."""
    import scripts.eval_retrieval as cli

    questions = tmp_path / "q.json"
    questions.write_text(
        json.dumps([{"question": "Де ховати горіхи й жолуді?", "expected": ["nuts"]}]),
        encoding="utf-8",
    )
    monkeypatch.setattr(cli, "load_rag_config", lambda: _rag())
    monkeypatch.setattr(
        cli.SentenceTransformerEmbedder, "load", staticmethod(lambda rag: HashEmbedder())
    )

    code = await cli.main(
        [
            "--archive",
            str(archive),
            "--questions",
            str(questions),
            "--margin",
            "0.05",
            "--min-recall",
            "0.5",
        ]
    )

    out = capsys.readouterr().out
    assert code == 0
    assert "margin=0.05" in out
    assert "полнота не ниже 50%" in out


async def test_cli_defaults_to_margin_from_config(archive, tmp_path, monkeypatch, capsys):
    import scripts.eval_retrieval as cli

    questions = tmp_path / "q.json"
    questions.write_text(
        json.dumps([{"question": "Горіхи?", "expected": ["nuts"]}]), encoding="utf-8"
    )
    monkeypatch.setattr(cli, "load_rag_config", lambda: _rag(relevance_margin=0.03))
    monkeypatch.setattr(
        cli.SentenceTransformerEmbedder, "load", staticmethod(lambda rag: HashEmbedder())
    )

    await cli.main(["--archive", str(archive), "--questions", str(questions)])

    assert "margin=0.03" in capsys.readouterr().out


def test_report_contains_settings_group_metrics_and_llm_request_count():
    questions = [
        EvalQuestion("є відповідь", ("doc-a",)),
        EvalQuestion("поруч по темі", (), kind="near_miss"),
        EvalQuestion("зовсім інше", ()),
    ]
    near = SimpleNamespace(title="doc-a", distance=0.1, document_id=1, chunk_id=1)
    candidates = [[near], [near], []]
    current = evaluate_threshold(questions, candidates, threshold=0.25, top_k=3)

    report = format_report(
        index_report=IndexReport(documents=1, chunks=1),
        current=current,
        results=[current],
        recommendation=None,
        top_k=3,
        settings={"модель": "fake/e5", "чанкинг": "200/40"},
    )

    assert "- модель: `fake/e5`" in report
    assert "| near_miss | 1 | 0% | 1 |" in report  # ложный допуск: дошёл бы до LLM
    assert "| off_topic | 1 | 100% | 0 |" in report
    assert "Запросов к LLM на 3 вопросов: 2." in report
    assert current.llm_requests == 2
