import pytest

from services.chunker import chunk_segments, split_text
from services.parsers import Segment

UK = "Їжак ховає горіхи у кількох місцях. Білка перевіряє запаси щотижня. Борсук мовчить."


def test_short_text_is_one_chunk():
    assert split_text("Одна порада.", size=1000, overlap=100) == ["Одна порада."]


def test_empty_input_gives_no_chunks():
    assert chunk_segments([], size=100, overlap=10) == []
    assert chunk_segments([Segment(text="  \n ")], size=100, overlap=10) == []


@pytest.mark.parametrize(("size", "overlap"), [(0, 0), (10, 10), (10, 11), (10, -1)])
def test_bad_parameters_are_rejected(size, overlap):
    with pytest.raises(ValueError, match="overlap"):
        chunk_segments([Segment(text="x")], size=size, overlap=overlap)


@pytest.mark.parametrize("size", [30, 60, 120, 400])
def test_chunks_never_exceed_size(size):
    text = (UK + "\n\n") * 20 + "слово " * 200

    pieces = split_text(text, size=size, overlap=size // 4)

    assert pieces
    assert max(len(p) for p in pieces) <= size


def test_no_text_is_lost():
    text = " ".join(f"слово{i}." for i in range(200))

    pieces = split_text(text, size=80, overlap=20)

    assert set(text.split()) <= set(" ".join(pieces).split())


def test_overlap_repeats_whole_sentences():
    text = "Перше речення тут. Друге речення тут. Третє речення тут. Четверте речення тут."

    pieces = split_text(text, size=45, overlap=20)

    assert pieces == [
        "Перше речення тут. Друге речення тут.",
        "Друге речення тут. Третє речення тут.",
        "Третє речення тут. Четверте речення тут.",
    ]


def test_zero_overlap_means_no_repeats():
    text = "Перше речення тут. Друге речення тут. Третє речення тут."

    pieces = split_text(text, size=30, overlap=0)

    assert pieces == ["Перше речення тут.", "Друге речення тут.", "Третє речення тут."]


def test_long_paragraph_without_punctuation_is_split_by_words():
    text = "слово " * 100

    pieces = split_text(text, size=50, overlap=10)

    assert len(pieces) > 5
    assert all(len(p) <= 50 for p in pieces)
    assert all(not p.startswith(("лово", "ово")) for p in pieces)  # слова не рвутся


def test_word_longer_than_size_is_hard_cut():
    pieces = split_text("x" * 250, size=100, overlap=10)

    assert [len(p) for p in pieces] == [100, 100, 50]


def test_paragraphs_become_separate_units_and_are_packed():
    text = "Абзац один.\n\nАбзац два.\n\nАбзац три."

    assert split_text(text, size=1000, overlap=0) == ["Абзац один. Абзац два. Абзац три."]


def test_indexes_are_sequential_and_address_is_kept():
    segments = [
        Segment(text="Перша сторінка. " * 30, page=1),
        Segment(text="Друга сторінка. " * 30, page=2),
    ]

    chunks = chunk_segments(segments, size=100, overlap=20)

    assert [c.index for c in chunks] == list(range(len(chunks)))
    assert {c.page for c in chunks} == {1, 2}
    assert all("Перша" in c.text for c in chunks if c.page == 1)
    assert all("Друга" in c.text for c in chunks if c.page == 2)


def test_segments_with_same_address_are_merged():
    segments = [
        Segment(text="Заголовок", section="Зима"),
        Segment(text="Перший абзац.", section="Зима"),
        Segment(text="Другий абзац.", section="Зима"),
    ]

    chunks = chunk_segments(segments, size=1000, overlap=100)

    assert len(chunks) == 1
    assert chunks[0].section == "Зима"
    assert chunks[0].text == "Заголовок Перший абзац. Другий абзац."


def test_chunk_does_not_cross_section_boundary():
    segments = [
        Segment(text="Коротко про зиму.", section="Зима"),
        Segment(text="Коротко про весну.", section="Весна"),
    ]

    chunks = chunk_segments(segments, size=1000, overlap=100)

    assert [(c.section, c.text) for c in chunks] == [
        ("Зима", "Коротко про зиму."),
        ("Весна", "Коротко про весну."),
    ]


def test_deterministic():
    text = UK * 30

    assert split_text(text, size=90, overlap=30) == split_text(text, size=90, overlap=30)
