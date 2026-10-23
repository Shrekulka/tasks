"""T15 должен демонстрироваться на образцах из репозитория при настройках по умолчанию."""

from pathlib import Path

from config_data.config import load_rag_config
from services.parsers import ParseLimits, extract_segments

SAMPLE = Path("samples/archive/velykyi-putivnyk.docx")


def test_bundled_long_guide_counts_as_large_with_default_settings(valid_env, no_dotenv):
    rag = load_rag_config(no_dotenv)
    _, segments = extract_segments(SAMPLE.name, SAMPLE.read_bytes(), ParseLimits.from_config(rag))

    total = sum(len(segment.text) for segment in segments)

    assert rag.large_document_chars > 0
    assert total >= rag.large_document_chars, (
        f"образец ({total} символов) меньше порога {rag.large_document_chars}: "
        "сообщения «обробляю/готовий» (T15) нельзя показать на образцах"
    )
