# airadar/radar/tests/test_llm.py

import os
from unittest.mock import patch

from django.core.cache import cache
from django.test import SimpleTestCase

from radar.services import llm


class Resp:
    def __init__(self, status=200, text="Відповідь"):
        self.status_code = status
        self._text = text

    def json(self):
        return {"choices": [{"message": {"content": self._text}}]}


MSGS = [{"role": "user", "content": "hi"}]


@patch.dict(os.environ, {"OPENROUTER_API_KEYS": "k1,k2"})
@patch("radar.services.llm.candidate_models", return_value=["m1:free", "m2:free"])
class FallbackTests(SimpleTestCase):
    def setUp(self):
        cache.clear()

    def test_no_keys_returns_none(self, _):
        with patch.dict(os.environ, {"OPENROUTER_API_KEYS": ""}):
            self.assertIsNone(llm.ask_with_fallback(MSGS))
            self.assertFalse(llm.llm_enabled())

    def test_success_first_pair(self, _):
        with patch("radar.services.llm.requests.post", return_value=Resp()) as post:
            self.assertEqual(llm.ask_with_fallback(MSGS), "Відповідь")
        self.assertEqual(post.call_count, 1)
        self.assertEqual(post.call_args.kwargs["headers"]["Authorization"], "Bearer k1")

    def test_429_switches_key_not_model(self, _):
        with patch("radar.services.llm.requests.post", side_effect=[Resp(429), Resp()]) as post:
            self.assertEqual(llm.ask_with_fallback(MSGS), "Відповідь")
        self.assertEqual(post.call_args_list[1].kwargs["headers"]["Authorization"], "Bearer k2")
        self.assertEqual(post.call_args_list[1].kwargs["json"]["model"], "m1:free")

    def test_404_quarantines_model_and_goes_to_next_model(self, _):
        with patch("radar.services.llm.requests.post", side_effect=[Resp(404), Resp()]) as post:
            self.assertEqual(llm.ask_with_fallback(MSGS), "Відповідь")
        self.assertEqual(post.call_count, 2)                     # другий ключ для поганої моделі не пробуємо
        self.assertEqual(post.call_args_list[1].kwargs["json"]["model"], "m2:free")
        self.assertTrue(cache.get("llm:bad:m1:free"))

    def test_all_fail_returns_none(self, _):
        with patch("radar.services.llm.requests.post", return_value=Resp(500)):
            self.assertIsNone(llm.ask_with_fallback(MSGS))

    def test_empty_answer_is_skipped(self, _):
        with patch("radar.services.llm.requests.post", side_effect=[Resp(text="  "), Resp()]):
            self.assertEqual(llm.ask_with_fallback(MSGS), "Відповідь")

    def test_403_on_all_keys_quarantines_model(self, _):
        with patch("radar.services.llm.requests.post", side_effect=[Resp(403), Resp(403), Resp()]) as post:
            self.assertEqual(llm.ask_with_fallback(MSGS), "Відповідь")
        self.assertEqual(post.call_args_list[2].kwargs["json"]["model"], "m2:free")
        self.assertTrue(cache.get("llm:bad:m1:free"))

    def test_403_on_one_key_does_not_quarantine_model(self, _):
        with patch("radar.services.llm.requests.post", side_effect=[Resp(403), Resp()]):
            self.assertEqual(llm.ask_with_fallback(MSGS), "Відповідь")
        self.assertFalse(cache.get("llm:bad:m1:free"))

    def test_foreign_script_answer_is_skipped(self, _):
        with patch("radar.services.llm.requests.post", side_effect=[Resp(text="Привіт 엔terprise"), Resp()]):
            self.assertEqual(llm.ask_with_fallback(MSGS), "Відповідь")


@patch.dict(os.environ, {"OPENROUTER_API_KEYS": "k1,k2"})
@patch("radar.services.llm.candidate_models", return_value=["m1:free", "m2:free"])
class DeadlineTests(SimpleTestCase):
    def setUp(self):
        cache.clear()

    def test_stops_when_deadline_exceeded(self, _):
        with self.settings(OPENROUTER_DEADLINE_SECONDS=0), \
                patch("radar.services.llm.requests.post") as post:
            self.assertIsNone(llm.ask_with_fallback(MSGS))
        post.assert_not_called()
