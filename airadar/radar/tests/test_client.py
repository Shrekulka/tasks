# airadar/radar/tests/test_client.py

from unittest.mock import patch

import requests
from django.core.cache import cache
from django.test import SimpleTestCase

from radar.services import freeserp
from radar.services.freeserp import FreeSerpError, lookup_domain, make_cache_key, search_sites


class FakeResponse:
    def __init__(self, status=200, payload=None):
        self.status_code = status
        self._payload = payload if payload is not None else {"ok": True, "total": 0, "results": []}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))


@patch("radar.services.freeserp.time.sleep", lambda s: None)
class ClientTests(SimpleTestCase):
    def setUp(self):
        cache.clear()

    def test_cache_key_ignores_order_and_agent(self):
        a = make_cache_key({"q": "x", "size": 5, "agent": "A"})
        b = make_cache_key({"size": 5, "q": "x", "agent": "B"})
        self.assertEqual(a, b)

    def test_cache_key_keeps_case(self):
        self.assertNotEqual(make_cache_key({"q": "ChatGPT"}), make_cache_key({"q": "chatgpt"}))

    def test_retry_then_success(self):
        with patch("radar.services.freeserp.requests.get",
                   side_effect=[FakeResponse(502), FakeResponse(200)]) as get:
            result = search_sites()
        self.assertEqual(get.call_count, 2)
        self.assertEqual(result.total, 0)

    def test_two_failures_raise(self):
        with patch("radar.services.freeserp.requests.get", return_value=FakeResponse(502)):
            with self.assertRaises(FreeSerpError):
                search_sites()

    def test_timeout_raises_after_retry(self):
        with patch("radar.services.freeserp.requests.get", side_effect=requests.Timeout()) as get:
            with self.assertRaises(FreeSerpError):
                search_sites()
        self.assertEqual(get.call_count, 2)

    def test_ok_false_raises(self):
        with patch("radar.services.freeserp.requests.get",
                   return_value=FakeResponse(200, {"ok": False, "detail": "bad"})):
            with self.assertRaises(FreeSerpError):
                search_sites()

    def test_params_sent(self):
        with patch("radar.services.freeserp.requests.get", return_value=FakeResponse()) as get:
            search_sites(q="x", niche="chat", dr_min=10, from_date="2026-09-01", page=3, size=20)
        p = get.call_args.kwargs["params"]
        self.assertEqual(p["ai_startups"], 1)
        self.assertEqual(p["index"], "sites")
        self.assertEqual(p["from"], 40)
        self.assertEqual(p["ai_categories"], "chat")
        self.assertEqual(p["dr_min"], 10)

    def test_page_clamped_to_window(self):
        with patch("radar.services.freeserp.requests.get", return_value=FakeResponse()) as get:
            search_sites(page=10**9, size=100)
        p = get.call_args.kwargs["params"]
        self.assertLessEqual(p["from"] + p["size"], freeserp.MAX_WINDOW)

    def test_second_call_uses_cache(self):
        with patch("radar.services.freeserp.requests.get", return_value=FakeResponse()) as get:
            search_sites(q="x")
            search_sites(q="x")
        self.assertEqual(get.call_count, 1)

    def test_lookup_finds_match_not_first(self):
        payload = {"ok": True, "results": [{"domain": "other.com"}, {"domain": "Example.com", "title": "E"}]}
        with patch("radar.services.freeserp.requests.get", return_value=FakeResponse(200, payload)):
            site = lookup_domain("example.com")
        self.assertEqual(site.title, "E")

    def test_lookup_none_when_absent(self):
        payload = {"ok": True, "results": [{"domain": "other.com"}]}
        with patch("radar.services.freeserp.requests.get", return_value=FakeResponse(200, payload)):
            self.assertIsNone(lookup_domain("example.com"))

    def test_lookup_invalid_domain_makes_no_request(self):
        with patch("radar.services.freeserp.requests.get") as get:
            self.assertIsNone(lookup_domain("a b"))
        get.assert_not_called()

    def test_total_dict_shape(self):
        payload = {"ok": True, "total": {"value": 7}, "results": []}
        with patch("radar.services.freeserp.requests.get", return_value=FakeResponse(200, payload)):
            self.assertEqual(search_sites().total, 7)

class AggregateCacheTests(SimpleTestCase):
    def test_count_sites_uses_stats_ttl(self):
        from django.conf import settings
        from radar.services import freeserp
        with patch("radar.services.freeserp._request", return_value={"total": 5, "results": []}) as req:
            self.assertEqual(freeserp.count_sites(dr_min=0, dr_max=9), 5)
        self.assertEqual(req.call_args.args[1], settings.FREESERP_TTL_STATS)
