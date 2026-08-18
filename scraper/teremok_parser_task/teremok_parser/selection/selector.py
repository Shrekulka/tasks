from __future__ import annotations

from teremok_parser.models import ListingCandidate, SelectionMode
from teremok_parser.parser.date import sort_key


def select_candidates(
    all_candidates: list[ListingCandidate],
    mode: SelectionMode,
    target_total: int,
    min_per_category: int,
) -> tuple[list[ListingCandidate], dict[str, list[ListingCandidate]]]:
    by_category: dict[str, list[ListingCandidate]] = {}
    for item in all_candidates:
        by_category.setdefault(item.preview_category, []).append(item)
    for cat in by_category:
        by_category[cat].sort(key=lambda x: sort_key(x.preview_dt), reverse=True)

    selected: list[ListingCandidate] = []
    selected_ids: set[str] = set()

    if mode == SelectionMode.QUOTA_COVERAGE:
        for cat, items in by_category.items():
            for item in items[:min_per_category]:
                if item.id not in selected_ids:
                    selected.append(item)
                    selected_ids.add(item.id)
    remaining = sorted(
        (i for i in all_candidates if i.id not in selected_ids),
        key=lambda x: sort_key(x.preview_dt),
        reverse=True,
    )
    for item in remaining:
        if len(selected) >= target_total:
            break
        selected.append(item)
        selected_ids.add(item.id)

    reserve_pool: dict[str, list[ListingCandidate]] = {}
    for item in all_candidates:
        if item.id not in selected_ids:
            reserve_pool.setdefault(item.preview_category, []).append(item)

    selected.sort(key=lambda x: sort_key(x.preview_dt), reverse=True)
    return selected, reserve_pool
