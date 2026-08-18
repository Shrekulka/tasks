from __future__ import annotations

import re


def normalize_text(text: str) -> str:
    """Нормалізація тексту: нижній регістр та згортання внутрішніх пробілів."""
    return re.sub(r"\s+", " ", text.strip().lower())
