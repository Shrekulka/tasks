"""Примеры из репозитория (samples/archive) индексируются так, как ждут контрольные вопросы."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from services.evaluation import build_index, load_questions
from services.parsers import ParseLimits, extract_segments
from tests.fake_embedder import HashEmbedder

ROOT = Path(__file__).parents[2]
SAMPLES = ROOT / "samples" / "archive"


def _rag():
    return SimpleNamespace(
        chunk_size=1000,
        chunk_overlap=200,
        max_pdf_pages=500,
        max_zip_entries=2000,
        max_unpacked_mb=100,
    )


def test_samples_cover_all_formats_and_required_count():
    files = [p for p in SAMPLES.iterdir() if not p.name.startswith(".")]

    assert 8 <= len(files) <= 12
    assert {p.suffix for p in files} == {".txt", ".docx", ".pdf"}


async def test_samples_index_and_match_eval_questions():
    index, report = await build_index(SAMPLES, HashEmbedder(), _rag())

    titles = {chunk.title for chunk in index}
    for question in load_questions(ROOT / "tests" / "eval" / "questions.json"):
        assert set(question.expected) <= titles, question.question
    skipped = " ".join(report.skipped)
    assert "zymovi-zapasy_kopiya.txt: дубль" in skipped  # копия, а не оригинал, пропускается
    assert "skan-bez-tekstu.pdf: EmptyDocument" in skipped
    assert report.documents == 7


def test_ukrainian_text_pdf_is_extracted_with_page_numbers():
    data = (SAMPLES / "tuman-orientyry.pdf").read_bytes()
    limits = ParseLimits(max_pdf_pages=500, max_zip_entries=2000, max_unpacked_bytes=10**8)

    _, segments = extract_segments("tuman-orientyry.pdf", data, limits)

    assert [s.page for s in segments] == [1, 2]
    assert "Як не заблукати в тумані" in segments[0].text
    assert "заблукали" in segments[1].text


@pytest.mark.parametrize("name", ["zymovi-zapasy.txt", "vodopij-v-posukhu.txt"])
def test_text_samples_are_utf8_ukrainian(name):
    text = (SAMPLES / name).read_text(encoding="utf-8")

    assert any(ch in text for ch in "іїєґ")
