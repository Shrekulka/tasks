# airadar/radar/tests/test_normalize.py

from django.test import SimpleTestCase

from radar.services.freeserp import clean_domain, normalize_site, normalize_url, parse_stats


class NormalizeSiteTests(SimpleTestCase):
    def test_empty_summary_and_dr_none(self):
        s = normalize_site({"domain": "a.ai", "title": "A", "ai_summary": None, "dr": None})
        self.assertEqual(s.summary, "")
        self.assertIsNone(s.dr)

    def test_categories_string_becomes_tuple(self):
        s = normalize_site({"domain": "a.ai", "ai_categories": "chatbots"})
        self.assertEqual(s.niches, ("chatbots",))

    def test_missing_ai_source(self):
        self.assertIsNone(normalize_site({"domain": "a.ai"}).builder)

    def test_title_falls_back_to_domain(self):
        self.assertEqual(normalize_site({"domain": "a.ai", "title": ""}).title, "a.ai")

    def test_dr_string_and_garbage(self):
        self.assertEqual(normalize_site({"domain": "a.ai", "dr": "42"}).dr, 42)
        self.assertIsNone(normalize_site({"domain": "a.ai", "dr": "n/a"}).dr)

    def test_empty_dict_does_not_crash(self):
        s = normalize_site({})
        self.assertEqual(s.domain, "")
        self.assertIsNone(s.url)


class NormalizeUrlTests(SimpleTestCase):
    def test_javascript_rejected_falls_back_to_domain(self):
        self.assertEqual(normalize_url("javascript:alert(1)", "a.ai"), "https://a.ai")

    def test_empty_domain_and_bad_url(self):
        self.assertIsNone(normalize_url("javascript:alert(1)", ""))

    def test_userinfo_rejected(self):
        self.assertEqual(normalize_url("https://a.ai@evil.com", "a.ai"), "https://a.ai")

    def test_normal(self):
        self.assertEqual(normalize_url(" https://a.ai/x ", "a.ai"), "https://a.ai/x")

    def test_garbage_ipv6(self):
        self.assertIsNone(normalize_url("https://[bad", ""))


class CleanDomainTests(SimpleTestCase):
    def test_cases(self):
        self.assertEqual(clean_domain(" Example.COM "), "example.com")
        self.assertIsNone(clean_domain("a b.com"))
        self.assertIsNone(clean_domain("example.com:8080"))
        self.assertIsNone(clean_domain("../etc/passwd"))
        self.assertIsNone(clean_domain("a" * 300))


class ParseStatsTests(SimpleTestCase):
    def test_unknown_shape_does_not_crash(self):
        s = parse_stats({"weird": 1})
        self.assertIsNone(s.ai_total)
        self.assertEqual(s.top_niches, ())

    def test_real_api_shape(self):
        """Формат з docs і фікстури: список {"key", "count"}; by_day приходить від нових до старих."""
        s = parse_stats({
            "generated_at": "2026-10-02T16:30:11+00:00",
            "ai_startups": {"total": 10, "today": 2},
            "top_ai_categories": [{"key": "chat", "count": 5}, {"key": "seo", "count": 2}],
            "by_day": [
                {"key": "2026-09-02T00:00:00.000Z", "count": 8},
                {"key": "2026-09-01T00:00:00.000Z", "count": 4},
            ],
        })
        self.assertEqual(s.ai_total, 10)
        self.assertEqual(s.ai_today, 2)
        self.assertEqual(s.top_niches, (("chat", 5, 100), ("seo", 2, 40)))
        self.assertEqual(s.niche_options, ("chat", "seo"))
        self.assertEqual(s.by_day, (("01.09", 4, 50), ("02.09", 8, 100)))   # від старих до нових
        self.assertEqual(s.generated_at, "2026-10-02 16:30")
