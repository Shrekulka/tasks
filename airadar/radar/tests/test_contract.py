# airadar/radar/tests/test_contract.py
"""Контрактні тести на справжніх відповідях API (tests/fixtures), а не на вигаданих формах."""

import json
from pathlib import Path
from unittest.mock import patch

from django.conf import settings
from django.core.cache import cache
from django.test import SimpleTestCase

from radar.services.freeserp import normalize_site, parse_stats, search_sites

FIX = Path(__file__).parent / "fixtures"


def load(name):
    return json.loads((FIX / name).read_text(encoding="utf-8"))


class StatsContractTests(SimpleTestCase):
    def setUp(self):
        self.raw = load("sample_stats.json")
        self.stats = parse_stats(self.raw)

    def test_kpis(self):
        self.assertEqual(self.stats.ai_total, self.raw["ai_startups"]["total"])
        self.assertEqual(self.stats.new_today, self.raw["new"]["today"])

    def test_all_niches_available_for_filter(self):
        """Фільтр каталогу не обрізається до кількості, яку показано на головній."""
        self.assertEqual(len(self.stats.niche_options), len(self.raw["top_ai_categories"]))
        self.assertLessEqual(len(self.stats.top_niches), settings.NICHES_ON_HOME)
        self.assertGreater(len(self.stats.niche_options), len(self.stats.top_niches))

    def test_by_day_ascending_and_30_points(self):
        labels = [d[0] for d in self.stats.by_day]
        self.assertEqual(len(labels), min(30, len(self.raw["by_day"])))
        keys = sorted(x["key"] for x in self.raw["by_day"])[-len(labels):]
        self.assertEqual(labels[0], keys[0][8:10] + "." + keys[0][5:7])

    def test_dr_histogram_sorted_buckets(self):
        starts = [b[0] for b in self.stats.dr_histogram]
        self.assertEqual(starts, sorted(starts))
        self.assertTrue(starts)

    def test_by_day_is_whole_index_not_ai(self):
        """Регресія: by_day збігається з new.*, тобто це весь індекс. Графік не можна підписувати як «AI»."""
        self.assertEqual(self.raw["by_day"][0]["count"], self.raw["new"]["today"])
        self.assertGreater(self.raw["by_day"][0]["count"], self.raw["ai_startups"]["total"] // 10)


class SearchContractTests(SimpleTestCase):
    def setUp(self):
        cache.clear()
        self.raw = load("sample_ai.json")

    def test_every_fixture_site_is_routable(self):
        for item in self.raw["results"]:
            site = normalize_site(item)
            self.assertTrue(site.domain, item.get("domain"))

    def test_unroutable_domains_are_dropped_not_crashing(self):
        bad = [{"domain": "a_b.com"}, {"domain": None}, {"domain": ""}, {"domain": "ok.ai"}]
        with patch("radar.services.freeserp._request", return_value={"ok": True, "total": 4, "results": bad}):
            res = search_sites()
        self.assertEqual([s.domain for s in res.sites], ["ok.ai"])

    def test_idn_domain_converted_to_punycode(self):
        with patch("radar.services.freeserp._request", return_value={"ok": True, "total": 1, "results": [{"domain": "мир.рф"}]}):
            res = search_sites()
        self.assertEqual(res.sites[0].domain, "xn--h1ahn.xn--p1ai")
