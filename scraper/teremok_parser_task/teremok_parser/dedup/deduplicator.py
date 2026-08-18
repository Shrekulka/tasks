from __future__ import annotations

from teremok_parser.constants import MISSING
from teremok_parser.models import Listing
from teremok_parser.text_utils import normalize_text
from teremok_parser.validation.metrics import RunMetrics


class Deduplicator:
    """Дворівнева дедуплікація (Exact + Near duplicates)."""

    def __init__(self, metrics: RunMetrics) -> None:
        self.metrics = metrics
        self._exact_hashes: set[str] = set()
        self._near_keys: dict[tuple[str, str], int] = {}

    def process(self, listing: Listing) -> bool:
        h = listing.content_hash

        # 1. Сначала проверяем exact duplicate.
        if h in self._exact_hashes:
            self.metrics.exact_duplicates_detected += 1
            self.metrics.record_duplicate_seller(listing.seller)
            return False

        # 2. Затем проверяем near duplicate.
        if listing.seller != MISSING:
            near_key = (
                listing.seller.strip().lower(),
                normalize_text(listing.title),
            )
            seen_count = self._near_keys.get(near_key, 0)

            if seen_count >= 1:
                self.metrics.near_duplicates_detected += 1
                self.metrics.record_duplicate_seller(listing.seller)
                return False

            self._near_keys[near_key] = seen_count + 1

        # 3. Только реально принятый listing добавляем
        #    в набор exact hashes.
        self._exact_hashes.add(h)
        self.metrics.record_seller(listing.seller)

        return True

    @property
    def unique_count(self) -> int:
        return len(self._exact_hashes)
