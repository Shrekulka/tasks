# airadar/radar/tests/test_views.py

from unittest.mock import patch

from django.core.cache import cache
from django.test import SimpleTestCase
from django.urls import reverse

from radar.schemas import SearchResult, Site, Stats
from radar.services.freeserp import FreeSerpError


def make_site(**kw):
    base = dict(domain="a.ai", url="https://a.ai", title="A", summary="", niches=(), builder=None,
                dr=None, first_live=None, http_status=None)
    return Site(**{**base, **kw})


def make_result(sites=(), total=0):
    return SearchResult(total=total, sites=tuple(sites), page=1, size=20, max_window=10_000)


@patch("radar.views.get_stats", return_value=Stats())
class CatalogViewTests(SimpleTestCase):
    def setUp(self):
        cache.clear()

    def test_ok(self, _):
        with patch("radar.views.search_sites", return_value=make_result([make_site()], 1)):
            r = self.client.get(reverse("radar:catalog"))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "a.ai")

    def test_empty(self, _):
        with patch("radar.views.search_sites", return_value=make_result()):
            r = self.client.get(reverse("radar:catalog"))
        self.assertContains(r, "Нічого не знайдено")

    def test_upstream_error_is_503(self, _):
        with patch("radar.views.search_sites", side_effect=FreeSerpError("down")):
            r = self.client.get(reverse("radar:catalog"))
        self.assertEqual(r.status_code, 503)

    def test_garbage_params_do_not_crash(self, _):
        with patch("radar.views.search_sites", return_value=make_result()) as s:
            r = self.client.get(reverse("radar:catalog"), {"dr_min": "abc", "sort": "evil", "page": "-5", "period": "x"})
        self.assertEqual(r.status_code, 200)
        kw = s.call_args.kwargs
        self.assertIsNone(kw["dr_min"])
        self.assertEqual(kw["sort"], "went_live")
        self.assertEqual(kw["page"], 1)

    def test_xss_is_escaped(self, _):
        evil = make_site(title="<script>alert(1)</script>", summary="<img src=x onerror=alert(1)>")
        with patch("radar.views.search_sites", return_value=make_result([evil], 1)):
            r = self.client.get(reverse("radar:catalog"))
        self.assertNotContains(r, "<script>alert(1)</script>")
        self.assertNotContains(r, "<img src=x")

    def test_ajax_returns_fragment(self, _):
        with patch("radar.views.search_sites", return_value=make_result([make_site()], 1)):
            r = self.client.get(reverse("radar:catalog"), HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertNotContains(r, "<html")


class CsvTests(SimpleTestCase):
    def test_formula_injection_escaped(self):
        evil = make_site(title="=HYPERLINK(\"http://evil\")", summary="+cmd")
        with patch("radar.views.search_sites", return_value=make_result([evil], 1)):
            r = self.client.get(reverse("radar:export"))
        body = r.content.decode("utf-8-sig")
        self.assertIn("'=HYPERLINK", body)
        self.assertIn("'+cmd", body)

    def test_uses_same_filters_and_limit(self):
        with patch("radar.views.search_sites", return_value=make_result()) as s:
            self.client.get(reverse("radar:export"), {"q": "x", "dr_min": "30"})
        kw = s.call_args.kwargs
        self.assertEqual(kw["q"], "x")
        self.assertEqual(kw["dr_min"], 30)
        self.assertEqual(kw["size"], 100)


class DetailAndCompareTests(SimpleTestCase):
    def test_site_404_when_absent(self):
        with patch("radar.views.lookup_domain", return_value=None):
            self.assertEqual(self.client.get("/site/nope.com/").status_code, 404)

    def test_site_bad_domain_404(self):
        self.assertEqual(self.client.get("/site/..evil/").status_code, 404)

    def test_site_error_503(self):
        with patch("radar.views.lookup_domain", side_effect=FreeSerpError("x")):
            self.assertEqual(self.client.get("/site/a.ai/").status_code, 503)

    def test_compare_limited_to_three(self):
        with patch("radar.views.lookup_domain", side_effect=lambda d: make_site(domain=d)) as lk:
            r = self.client.get(reverse("radar:compare"), {"d": ["a.ai", "b.ai", "c.ai", "d.ai"]})
        self.assertEqual(lk.call_count, 3)
        self.assertEqual(r.status_code, 200)

    def test_compare_ignores_invalid_and_duplicates(self):
        with patch("radar.views.lookup_domain", side_effect=lambda d: make_site(domain=d)) as lk:
            self.client.get(reverse("radar:compare"), {"d": ["a.ai", "a.ai", "bad domain", "javascript:1"]})
        self.assertEqual(lk.call_count, 1)

class RegressionTests(SimpleTestCase):
    """Помилки, знайдені під час рев'ю."""

    def test_catalog_renders_when_api_returns_unroutable_domains(self):
        raw = {"ok": True, "total": 3, "results": [{"domain": "a_b.com"}, {"domain": None}, {"domain": "ok.ai"}]}
        cache.clear()
        with patch("radar.services.freeserp._request", return_value=raw), \
                patch("radar.views.get_stats", return_value=Stats()):
            r = self.client.get(reverse("radar:catalog"))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "ok.ai")

    def test_compare_table_is_built_from_field_list(self):
        sites = {"a.ai": make_site(domain="a.ai", dr=10), "b.ai": make_site(domain="b.ai", dr=40)}
        with patch("radar.views.lookup_domain", side_effect=lambda d: sites[d]):
            r = self.client.get(reverse("radar:compare"), {"d": ["a.ai", "b.ai"]})
        self.assertContains(r, "Вперше live")
        self.assertNotContains(r, "First live")
        self.assertContains(r, "is-best")

    def test_site_page_shows_similar_without_self(self):
        me = make_site(domain="me.ai", niches=("Chat",))
        found = make_result([make_site(domain="me.ai"), make_site(domain="other.ai")], 2)
        with patch("radar.views.lookup_domain", return_value=me), patch("radar.views.search_sites", return_value=found):
            r = self.client.get("/site/me.ai/")
        self.assertContains(r, "Схожі сайти")
        self.assertContains(r, "other.ai")

    def test_custom_404_page(self):
        with patch("radar.views.lookup_domain", return_value=None):
            r = self.client.get("/site/nope.com/")
        self.assertContains(r, "404", status_code=404)


class CompareBarTests(SimpleTestCase):
    def test_site_page_has_compare_bar_and_script(self):
        me = make_site(domain="me.ai", niches=("Chat",))
        with patch("radar.views.lookup_domain", return_value=me), \
                patch("radar.views.search_sites", return_value=make_result([make_site(domain="o.ai")], 1)):
            r = self.client.get("/site/me.ai/")
        self.assertContains(r, 'id="cmp-bar"')
        self.assertContains(r, "radar/catalog.js")

    def test_about_shows_ttl_from_settings(self):
        with self.settings(FREESERP_TTL_SEARCH=77):
            r = self.client.get("/about/")
        self.assertContains(r, "77 с")


class CompareSelectionTests(SimpleTestCase):
    """Вибір для порівняння: очищення панелі, кнопка «Очистити вибір», стійкість до «Назад»."""

    def _compare(self, *domains):
        sites = {d: make_site(domain=d) for d in domains}
        with patch("radar.views.lookup_domain", side_effect=lambda d: sites[d]), \
                patch("radar.views.llm_enabled", return_value=True):
            return self.client.get(reverse("radar:compare"), {"d": list(domains)})

    def test_selection_cleared_after_successful_compare(self):
        self.assertContains(self._compare("a.ai", "b.ai"), 'sessionStorage.removeItem("airadar:compare")')

    def test_selection_kept_when_only_one_site(self):
        self.assertNotContains(self._compare("a.ai"), "removeItem")

    def test_verdict_panel_shown_for_two_sites_when_llm_enabled(self):
        self.assertContains(self._compare("a.ai", "b.ai"), 'id="verdict-btn"')

    def test_clear_button_in_compare_bar(self):
        with patch("radar.views.get_stats", return_value=Stats()), \
                patch("radar.views.search_sites", return_value=make_result([make_site()], 1)):
            r = self.client.get(reverse("radar:catalog"))
        self.assertContains(r, 'id="cmp-clear"')

    def test_card_checkbox_is_not_restored_by_browser(self):
        with patch("radar.views.get_stats", return_value=Stats()), \
                patch("radar.views.search_sites", return_value=make_result([make_site()], 1)):
            r = self.client.get(reverse("radar:catalog"))
        self.assertContains(r, 'class="cmp" autocomplete="off"')
