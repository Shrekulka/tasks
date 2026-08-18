from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

_TRANSLIT_MAP = {
    "а": "a", "б": "b", "в": "v", "г": "h", "ґ": "g", "д": "d", "е": "e", "є": "ie",
    "ж": "zh", "з": "z", "и": "y", "і": "i", "ї": "i", "й": "i", "к": "k", "л": "l",
    "м": "m", "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "kh", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "shch", "ь": "",
    "ю": "iu", "я": "ia", "ъ": "", "э": "e", "ы": "y", "'": "",
}


def _transliterate(value: str) -> str:
    """Пословна транслітерація кирилиці (UA/RU) в латиницю за спрощеною
    версією офіційної української транслітераційної таблиці (КМУ, 2010)."""
    return "".join(_TRANSLIT_MAP.get(ch, ch) for ch in value)


def _slugify(value: str) -> str:
    transliterated = _transliterate(value.strip().lower())
    slug = re.sub(r"[^a-z0-9\-_]+", "_", transliterated)
    return slug.strip("_") or "unknown"


class RawHtmlStorage:
    """
    Збереження сирих HTML-сторінок (raw layer) виключно при DEBUG=True.
    Організовано за схемою: output/raw/<run_id>/{categories, listings}/.
    """

    def __init__(self, run_raw_dir: Path, enabled: bool = False) -> None:
        self.run_raw_dir = run_raw_dir
        self.enabled = enabled
        self.categories_dir = self.run_raw_dir / "categories"
        self.listings_dir = self.run_raw_dir / "listings"
        self._saved_listings: set[str] = set()
        self._saved_category_pages: set[tuple[str, int]] = set()

    def save_category_page(self, category_name: str, page_num: int, html: str) -> Optional[Path]:
        if not self.enabled or not html:
            return None
        key = (category_name, page_num)
        if key in self._saved_category_pages:
            return None
        self._saved_category_pages.add(key)

        cat_slug = _slugify(category_name)
        target_dir = self.categories_dir / cat_slug
        target_dir.mkdir(parents=True, exist_ok=True)
        file_path = target_dir / f"page_{page_num:03d}.html"
        file_path.write_text(html, encoding="utf-8")
        return file_path

    def save_listing_page(self, listing_id: str, html: str) -> Optional[Path]:
        if not self.enabled or not html:
            return None
        if listing_id in self._saved_listings:
            return None
        self._saved_listings.add(listing_id)

        self.listings_dir.mkdir(parents=True, exist_ok=True)
        safe_id = _slugify(listing_id)
        file_path = self.listings_dir / f"{safe_id}.html"
        file_path.write_text(html, encoding="utf-8")
        return file_path
