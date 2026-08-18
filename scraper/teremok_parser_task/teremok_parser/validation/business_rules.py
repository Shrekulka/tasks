from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from teremok_parser.constants import MISSING
from teremok_parser.models import Listing, SelectionMode
from teremok_parser.validation.metrics import RunMetrics


def _build_category_quota(
        category_counts: Counter,
        min_per_category: int,
        category_pool_size: dict[str, int],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    all_categories = set(category_pool_size.keys()) | set(category_counts.keys())
    shortfalls: list[dict[str, Any]] = []

    for cat in sorted(all_categories):
        count = category_counts.get(cat, 0)
        available = category_pool_size.get(cat, 0)
        if count >= min_per_category:
            continue
        reason = "insufficient_pool" if available < min_per_category else "pipeline_shortfall"
        shortfalls.append({
            "category": cat,
            "count": count,
            "required": min_per_category,
            "candidates_available_in_pool": available,
            "reason": reason,
        })

    pipeline_shortfalls = [s for s in shortfalls if s["reason"] == "pipeline_shortfall"]

    return {
        "min_per_category": min_per_category,
        "distribution": dict(category_counts),
        "shortfalls": shortfalls,
        "passed": len(shortfalls) == 0,
        "pipeline_passed": len(pipeline_shortfalls) == 0,
    }, pipeline_shortfalls


def validate_invariants(
        listings: list[Listing],
        mode: SelectionMode,
        target_total: int,
        min_categories: int,
        fail_fast: bool,
        metrics: RunMetrics,
        base_url: str,
        run_id: str,
        debug: bool,
        min_per_category: Optional[int] = None,
        category_pool_size: Optional[dict[str, int]] = None,
        raw_dir: Optional[Path] = None,
        data_dir: Optional[Path] = None,
        report_file: Optional[Path] = None,
        log_file: Optional[Path] = None,
) -> dict[str, Any]:
    category_counts = Counter(item.source_category for item in listings)
    covered_cats = len([c for c in category_counts if category_counts[c] > 0])
    hard_gate_errors: list[str] = []

    if len(listings) != target_total:
        hard_gate_errors.append(f"Кількість записів не відповідає цілі: {len(listings)}/{target_total}")

    if covered_cats < min_categories:
        hard_gate_errors.append(f"Покрито лише {covered_cats} категорій із {min_categories}.")

    category_quota: Optional[dict[str, Any]] = None
    if mode == SelectionMode.QUOTA_COVERAGE and min_per_category is not None:
        category_quota, pipeline_shortfalls = _build_category_quota(
            category_counts, min_per_category, category_pool_size or {}
        )
        for s in pipeline_shortfalls:
            hard_gate_errors.append(
                f"Категорія '{s['category']}' не досягла квоти {s['required']} "
                f"(отримано {s['count']}), хоча в пулі було доступно {s['candidates_available_in_pool']}."
            )

    telemetry_invariants = metrics.verify_invariants()
    if not all(telemetry_invariants.values()):
        hard_gate_errors.append(
            f"Порушення інваріантів телеметрії: лічильники неузгоджені ({telemetry_invariants})."
        )

    final_hashes = [item.content_hash for item in listings]
    if len(final_hashes) != len(set(final_hashes)):
        hard_gate_errors.append("Фінальний датасет містить exact-дублікати.")

    hard_gate_passed = len(hard_gate_errors) == 0
    action = "exported" if hard_gate_passed else ("export_blocked" if fail_fast else "exported_with_warning")
    dates_present = [i.published_at for i in listings if i.published_at]

    dedup_input = metrics.detail_successful + metrics.reserve_successful
    dedup_output = len(listings)
    duplicates_removed = (metrics.exact_duplicates_detected + metrics.near_duplicates_detected)
    dedup_rate = round(
        duplicates_removed / dedup_input,
        4,
    ) if dedup_input else 0.0

    report: dict[str, Any] = {
        "source": base_url,
        "run_metadata": {
            "run_id": run_id,
            "run_at_utc": datetime.now(timezone.utc).isoformat(),
            "mode": mode.value,
            "debug": debug,
            "artifacts": {
                "data_dir": str(data_dir) if data_dir else None,
                "report_file": str(report_file) if report_file else None,
                "log_file": str(log_file) if log_file else None,
                "raw_dir": str(raw_dir) if debug and raw_dir else None,
            },
        },
        "selection_metadata": {
            "candidate_pool": {
                "raw": metrics.raw_candidates_count,
                "unique": metrics.unique_candidates_count,
            },
            "detail_parsing": {
                "attempted": metrics.detail_attempted,
                "successful": metrics.detail_successful,
                "failed": metrics.detail_failed,
            },
            "reserve_backfill": {
                "attempted": metrics.reserve_attempted,
                "successful": metrics.reserve_successful,
                "failed": metrics.reserve_failed,
            },
            "telemetry_invariants": telemetry_invariants,
            "date_semantics": {
                "discovery": "preview_dt (картки пагінації) для швидкого первинного відбору свіжих кандидатів",
                "final_export": "published_at (повна сторінка) для точного сортування результуючого датасету",
            },
        },
        "category_quota": category_quota,
        "deduplication": {
            "basis": "exact: content_hash; near: seller + normalized title",
            "parsed_before_dedup": dedup_input,
            "exact_duplicates_removed": metrics.exact_duplicates_detected,
            "near_duplicates_removed": metrics.near_duplicates_detected,
            "total_duplicates_removed": duplicates_removed,
            "final_unique": dedup_output,
            "deduplication_rate": dedup_rate,
        },
        "seller_concentration": metrics.seller_concentration(top_n=5),
        "dataset_status": "CONFORMING" if hard_gate_passed else "NON_CONFORMING",
        "collected_total": len(listings),
        "categories_distribution": dict(category_counts),
        "categories_covered": covered_cats,
        "min_categories_required": min_categories,
        "date_range_utc": {
            "newest": max(dates_present).isoformat() if dates_present else None,
            "oldest": min(dates_present).isoformat() if dates_present else None,
        },
        "hard_gate": {
            "passed": hard_gate_passed,
            "enforced": fail_fast,
            "action": action,
            "errors": hard_gate_errors,
        },
        "missing_fields": {
            "price": sum(1 for i in listings if i.price is None),
            "seller": sum(1 for i in listings if i.seller == MISSING),
            "images": sum(1 for i in listings if not i.images),
            "published_at": sum(1 for i in listings if i.published_at is None),
        },
        "missing_price_note": (
            "price is None whenever the source itself provided no numeric value "
            "(e.g. price_raw == 'Ціна не вказана' / 'договірна'); this reflects "
            "source data, not a parser failure."
        ),
        "errors_by_status": dict(metrics.error_counts),
    }
    return report
