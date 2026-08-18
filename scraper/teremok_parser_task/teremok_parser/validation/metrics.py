from __future__ import annotations

import logging
from collections import Counter
from typing import Optional

from teremok_parser.models import ParseStatus

logger = logging.getLogger("teremok_parser.metrics")


class RunMetrics:
    def __init__(self) -> None:
        self.errors: list[dict[str, str]] = []
        self.error_counts: Counter = Counter()

        self.raw_candidates_count: int = 0
        self.unique_candidates_count: int = 0

        self.detail_attempted: int = 0
        self.detail_successful: int = 0
        self.detail_failed: int = 0

        self.reserve_attempted: int = 0
        self.reserve_successful: int = 0
        self.reserve_failed: int = 0

        self.exact_duplicates_detected: int = 0
        self.near_duplicates_detected: int = 0

        self._seller_counts: Counter = Counter()
        self._duplicate_seller_counts: Counter = Counter()

    def record_error(self, status: ParseStatus, url: str, message: Optional[str]) -> None:
        self.error_counts[status.value] += 1
        self.errors.append({
            "status": status.value,
            "url": url,
            "message": (message or "")[:300],
        })

    def record_seller(self, seller: str) -> None:
        self._seller_counts[seller] += 1

    def record_duplicate_seller(self, seller: str) -> None:
        self._duplicate_seller_counts[seller] += 1

    def seller_concentration(self, top_n: int = 5) -> dict:
        total = sum(self._seller_counts.values())
        top_sellers = [
            {
                "seller": seller,
                "count": count,
                "share": round(count / total, 4) if total else 0.0,
            }
            for seller, count in self._seller_counts.most_common(top_n)
        ]
        return {
            "unique_sellers": len(self._seller_counts),
            "top_sellers": top_sellers,
        }

    def verify_invariants(self) -> dict[str, bool]:
        return {
            "detail_phase_balanced": self.detail_attempted == (self.detail_successful + self.detail_failed),
            "reserve_phase_balanced": self.reserve_attempted == (self.reserve_successful + self.reserve_failed),
        }


class ConsecutiveErrorGuard:
    def __init__(self, limit: int = 8, label: str = "") -> None:
        self.limit = limit
        self.label = label
        self._streak = 0

    def record_success(self) -> None:
        self._streak = 0

    def record_failure(self) -> bool:
        self._streak += 1
        if self._streak >= self.limit:
            logger.error(
                f"  [Guard: {self.label}] {self._streak} помилок поспіль (ліміт {self.limit}). "
                f"Призупиняємо цю фазу для захисту від блокувань."
            )
            return True
        return False
