import zipfile
from io import BytesIO

import pytest

from errors.errors import EmptyDocument, UnreadableDocument, UnsupportedFormat
from services.parsers import (
    ParseLimits,
    Segment,
    detect_format,
    extract_segments,
    supported_extensions,
)
from tests.pdf_factory import blank_pdf, docx_bytes, text_pdf

LIMITS = ParseLimits(max_pdf_pages=50, max_zip_entries=500, max_unpacked_bytes=50 * 1024 * 1024)


def _limits(**overrides) -> ParseLimits:
    values = {"max_pdf_pages": 9, "max_zip_entries": 999, "max_unpacked_bytes": 10**9, **overrides}
    return ParseLimits(**values)


def _parse(name: str, data: bytes, limits: ParseLimits = LIMITS) -> list[Segment]:
    return extract_segments(name, data, limits)[1]


# ---------------------------------------------------------------------- определение формата


def test_supported_extensions():
    assert supported_extensions() == (".docx", ".pdf", ".txt")


@pytest.mark.parametrize("name", ["a.exe", "a", "archive.tar.gz", "a.doc", "a.PNG"])
def test_unknown_extension_is_rejected(name):
    with pytest.raises(UnsupportedFormat) as caught:
        detect_format(name, b"data")

    assert caught.value.reason == "extension"


def test_extension_is_case_insensitive():
    assert detect_format("GUIDE.PDF", text_pdf("x")).name == "pdf"


@pytest.mark.parametrize(
    ("name", "data"),
    [
        ("renamed.txt", text_pdf("hello")),  # PDF под видом TXT
        ("renamed.pdf", b"plain text, not a pdf"),
        ("renamed.docx", b"plain text"),
        ("renamed.pdf", docx_bytes(lambda d: d.add_paragraph("x"))),  # DOCX под видом PDF
        ("renamed.txt", docx_bytes(lambda d: d.add_paragraph("x"))),  # DOCX под видом TXT
        ("binary.txt", b"abc\x00\x01\x02def"),
    ],
)
def test_content_must_match_extension(name, data):
    with pytest.raises(UnsupportedFormat) as caught:
        detect_format(name, data)

    assert caught.value.reason == "mismatch"


def test_zip_without_word_document_is_not_docx():
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("readme.txt", "x")

    with pytest.raises(UnsupportedFormat):
        detect_format("fake.docx", buffer.getvalue())


# ---------------------------------------------------------------------- TXT


def test_txt_utf8():
    segments = _parse("a.txt", "Їжак ховає ґрунтові горіхи.".encode())

    assert segments == [Segment(text="Їжак ховає ґрунтові горіхи.")]


def test_txt_utf8_with_bom():
    assert _parse("a.txt", "\ufeffПривіт".encode("utf-8"))[0].text == "Привіт"


def test_txt_cp1251_fallback():
    data = "Привіт, ліс! Їжак і ґанок.".encode("cp1251")

    assert _parse("a.txt", data)[0].text == "Привіт, ліс! Їжак і ґанок."


def test_txt_whitespace_is_normalized():
    data = b"line one   with   spaces\r\n\r\n\r\n\r\nline two\t\tend\r\n"

    assert _parse("a.txt", data)[0].text == "line one with spaces\n\nline two end"


@pytest.mark.parametrize("data", [b"", b"   \n\n\t  "])
def test_empty_txt_is_rejected(data):
    with pytest.raises(EmptyDocument):
        _parse("a.txt", data)


def test_txt_with_undecodable_bytes_is_unreadable():
    with pytest.raises(UnreadableDocument):
        _parse("a.txt", b"\x98\x98\xff text")  # не UTF-8, а 0x98 не определён в cp1251


# ---------------------------------------------------------------------- PDF


def test_pdf_pages_keep_numbers():
    segments = _parse("a.pdf", text_pdf("First page text", "Second page text"))

    assert segments == [
        Segment(text="First page text", page=1),
        Segment(text="Second page text", page=2),
    ]


def test_pdf_scan_without_text_is_empty():
    with pytest.raises(EmptyDocument):
        _parse("scan.pdf", blank_pdf(pages=3))


def test_pdf_with_password_is_unreadable():
    with pytest.raises(UnreadableDocument, match="парол"):
        _parse("secret.pdf", blank_pdf(password="pw"))


def test_pdf_page_limit():
    limits = ParseLimits(max_pdf_pages=1, max_zip_entries=500, max_unpacked_bytes=10**7)

    with pytest.raises(UnreadableDocument, match="страниц"):
        _parse("big.pdf", text_pdf("a", "b"), limits)


@pytest.mark.parametrize("data", [b"%PDF-1.4 this is not really a pdf", b"%PDF-"])
def test_corrupt_pdf_is_unreadable(data):
    with pytest.raises(UnreadableDocument):
        _parse("broken.pdf", data)


# ---------------------------------------------------------------------- DOCX


def _guide(document):
    document.add_heading("Зима", level=1)
    document.add_paragraph("Тримайтеся разом.")
    table = document.add_table(rows=2, cols=2)
    table.cell(0, 0).text = "Звір"
    table.cell(0, 1).text = "Запас"
    table.cell(1, 0).text = "Їжак"
    table.cell(1, 1).text = "Яблука"
    document.add_heading("Весна", level=1)
    document.add_paragraph("Стежте за повенями.")
    document.sections[0].footer.paragraphs[0].text = "КОЛОНТИТУЛ-НЕ-ВКЛЮЧАТИ"
    document.sections[0].header.paragraphs[0].text = "ВЕРХНІЙ-НЕ-ВКЛЮЧАТИ"


def test_docx_keeps_order_sections_and_tables():
    segments = _parse("guide.docx", docx_bytes(_guide))

    assert segments == [
        Segment(text="Зима", section="Зима"),
        Segment(text="Тримайтеся разом.", section="Зима"),
        Segment(text="Звір | Запас", section="Зима"),
        Segment(text="Їжак | Яблука", section="Зима"),
        Segment(text="Весна", section="Весна"),
        Segment(text="Стежте за повенями.", section="Весна"),
    ]


def test_docx_headers_and_footers_are_ignored():
    joined = " ".join(s.text for s in _parse("guide.docx", docx_bytes(_guide)))

    assert "НЕ-ВКЛЮЧАТИ" not in joined


def test_docx_merged_cells_are_not_repeated():
    def build(document):
        table = document.add_table(rows=1, cols=3)
        merged = table.cell(0, 0).merge(table.cell(0, 1))
        merged.text = "Об'єднана"
        table.cell(0, 2).text = "Окрема"

    assert _parse("t.docx", docx_bytes(build))[0].text == "Об'єднана | Окрема"


def test_docx_without_text_is_empty():
    with pytest.raises(EmptyDocument):
        _parse("empty.docx", docx_bytes(lambda d: d.add_paragraph("   ")))


def test_docx_zip_limits():
    data = docx_bytes(_guide)

    with pytest.raises(UnreadableDocument, match="записей"):
        _parse(
            "a.docx",
            data,
            _limits(max_zip_entries=1),
        )
    with pytest.raises(UnreadableDocument, match="розпаков|распаков"):
        _parse(
            "a.docx",
            data,
            _limits(max_unpacked_bytes=100),
        )


def test_docx_with_broken_xml_is_unreadable():
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("word/document.xml", "<not-closed")

    with pytest.raises(UnreadableDocument):
        _parse("broken.docx", buffer.getvalue())


def test_limits_from_config():
    from types import SimpleNamespace

    rag = SimpleNamespace(max_pdf_pages=7, max_zip_entries=8, max_unpacked_mb=2)

    assert ParseLimits.from_config(rag) == ParseLimits(7, 8, 2 * 1024 * 1024)  # type: ignore[arg-type]
