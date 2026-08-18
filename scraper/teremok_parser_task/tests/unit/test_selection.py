from datetime import datetime, timezone

from teremok_parser.models import ListingCandidate, SelectionMode
from teremok_parser.selection.selector import select_candidates


def _make_cand(cid: str, cat: str, ts: int) -> ListingCandidate:
    return ListingCandidate(
        id=cid,
        title=f"Item {cid}",
        url=f"https://teremok.org.ua/ad/{cid}",
        preview_category=cat,
        preview_dt=datetime.fromtimestamp(ts, tz=timezone.utc),
    )


def test_selection_strict_newest_global_freshness():
    """Перевірка строгого відбору за датою без спотворення квотами категорій."""
    pool = [
        _make_cand("A1", "CatA", 1000),
        _make_cand("A2", "CatA", 900),
        _make_cand("A3", "CatA", 800),
        _make_cand("B1", "CatB", 1200),
        _make_cand("B2", "CatB", 1100),
        _make_cand("B3", "CatB", 700),
    ]
    # При target_total=4 очікуємо 4 глобально найновіших: B1 (1200), B2 (1100), A1 (1000), A2 (900)
    selected, reserve = select_candidates(pool, SelectionMode.STRICT_NEWEST, target_total=4, min_per_category=2)
    selected_ids = [c.id for c in selected]
    assert selected_ids == ["B1", "B2", "A1", "A2"]


def test_selection_quota_coverage_guarantees_each_category():
    pool = [
        _make_cand("1", "CatA", 500),
        _make_cand("2", "CatA", 400),
        _make_cand("3", "CatA", 300),
        _make_cand("4", "CatB", 100),
    ]
    selected, _ = select_candidates(pool, SelectionMode.QUOTA_COVERAGE, target_total=3, min_per_category=1)
    selected_cats = {c.preview_category for c in selected}
    assert "CatA" in selected_cats
    assert "CatB" in selected_cats
