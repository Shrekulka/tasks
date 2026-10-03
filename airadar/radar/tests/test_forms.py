# airadar/radar/tests/test_forms.py

from django.http import QueryDict
from django.test import SimpleTestCase

from radar.forms import parse_filters


def qd(s):
    return QueryDict(s)


class FiltersTests(SimpleTestCase):
    def test_auto_sort_uses_api_relevance_when_query_given(self):
        self.assertEqual(parse_filters(qd("q=chatbot")).search_kwargs()["sort"], "")

    def test_auto_sort_is_newest_without_query(self):
        self.assertEqual(parse_filters(qd("")).search_kwargs()["sort"], "went_live")

    def test_explicit_sort_wins(self):
        self.assertEqual(parse_filters(qd("q=x&sort=dr")).search_kwargs()["sort"], "dr")

    def test_unknown_period_falls_back_to_all(self):
        self.assertEqual(parse_filters(qd("period=999")).period, "all")

    def test_new_filters_reach_search_kwargs(self):
        kw = parse_filters(qd("ai_source=lovable&tld=ai&niche=Code+%26+Dev+Tools")).search_kwargs()
        self.assertEqual((kw["ai_source"], kw["tld"], kw["niche"]), ("lovable", "ai", "Code & Dev Tools"))

    def test_chips_and_remove_links(self):
        f = parse_filters(qd("q=x&tld=ai&dr_min=10"))
        chips = {c["label"]: c["remove_qs"] for c in f.chips()}
        self.assertEqual(set(chips), {"Пошук", "TLD", "DR від"})
        self.assertNotIn("tld", chips["TLD"])
        self.assertIn("q=x", chips["TLD"])

    def test_garbage_does_not_crash(self):
        f = parse_filters(qd("tld=<script>&ai_source=%00&dr_min=abc&page=-1"))
        self.assertEqual((f.tld, f.ai_source, f.dr_min, f.page), ("", "", None, 1))


class DrRangeTests(SimpleTestCase):
    def test_dr_max_reaches_search_kwargs_and_url(self):
        f = parse_filters(qd("dr_min=20&dr_max=29"))
        self.assertEqual((f.search_kwargs()["dr_min"], f.search_kwargs()["dr_max"]), (20, 29))
        self.assertIn("dr_max=29", f.querystring())

    def test_bad_dr_max_is_ignored(self):
        self.assertIsNone(parse_filters(qd("dr_max=abc")).dr_max)
        self.assertIsNone(parse_filters(qd("dr_max=500")).dr_max)
