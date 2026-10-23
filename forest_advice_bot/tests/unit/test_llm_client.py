"""Тесты клиента LLM: настоящий HTTP до поддельного сервера, управляемое время."""

from datetime import UTC, datetime

import pytest

from errors.errors import LLMAccountBlocked, LLMQuotaExceeded, LLMUnavailable
from external_services.llm.llm_client import LLMClient
from external_services.llm.registry import create_provider
from tests.fake_openrouter import FakeTime, make_llm_config, model_entry

CHAT = "/chat/completions"
MESSAGES = [{"role": "user", "content": "Питання"}]
PLATFORM_429 = {"X-RateLimit-Limit": "20", "X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1"}


@pytest.fixture
def ftime():
    return FakeTime()


@pytest.fixture
def build(fake_api, http_session, ftime):
    def factory(**overrides) -> LLMClient:
        config = make_llm_config(fake_api.base_url, **overrides)
        bundle = create_provider(config, http_session)
        return LLMClient(
            bundle,
            config,
            clock=ftime.clock,
            sleep=ftime.sleep,
            utc_now=lambda: datetime(2026, 10, 20, 22, 0, tzinfo=UTC),
        )

    return factory


def _catalog(fake_api, *ids, quota=40):
    """Модели с убывающим контекстом: первая в списке пробуется первой."""
    entries = [model_entry(m, context=100_000 - i * 1000) for i, m in enumerate(ids)]
    fake_api.catalog(*entries)
    fake_api.key_info(remaining=quota)


# ---------------------------------------------------------------- базовый путь


async def test_first_model_answers(fake_api, build):
    _catalog(fake_api, "a/one:free", "b/two:free")
    fake_api.answer("Порада")

    result = await build().ask(MESSAGES)

    assert (result.text, result.model, result.key_index) == ("Порада", "a/one:free", 0)
    assert fake_api.chat_models() == ["a/one:free"]


async def test_options_are_forwarded_and_validator_sees_text(fake_api, build):
    _catalog(fake_api, "a/one:free")
    fake_api.answer("Порада")
    seen = []

    await build().ask(
        MESSAGES, validate=lambda t: seen.append(t) or True, options={"temperature": 0.1}
    )

    assert seen == ["Порада"]
    assert fake_api.calls("POST", CHAT)[0].json["temperature"] == 0.1


async def test_pinned_model_goes_first(fake_api, build):
    _catalog(fake_api, "big/model:free", "small/pinned:free")
    fake_api.answer()

    result = await build(models=("small/pinned:free",)).ask(MESSAGES)

    assert result.model == "small/pinned:free"


async def test_paid_models_are_never_used(fake_api, build):
    fake_api.catalog(model_entry("paid/model:free", price="0.001"), model_entry("ok/model:free"))
    fake_api.key_info(remaining=40)
    fake_api.answer()

    result = await build(models=("paid/model:free",)).ask(MESSAGES)

    assert result.model == "ok/model:free"
    assert "paid/model:free" not in fake_api.chat_models()


async def test_no_free_models_means_unavailable_without_chat_calls(fake_api, build):
    fake_api.catalog(model_entry("paid/model:free", price="0.001"))

    with pytest.raises(LLMUnavailable):
        await build().ask(MESSAGES)

    assert fake_api.calls("POST", CHAT) == []


async def test_empty_free_list_is_diagnosed_in_log(fake_api, build, caplog):
    import logging

    fake_api.catalog(model_entry("paid/model:free", price="0.001"))
    caplog.set_level(logging.WARNING, logger="external_services.llm.llm_client")

    with pytest.raises(LLMUnavailable):
        await build().ask(MESSAGES)

    assert "ни одна не прошла фильтр бесплатных" in caplog.text


async def test_zero_deadline_makes_no_network_calls(fake_api, build):
    with pytest.raises(LLMUnavailable):
        await build(deadline_seconds=0).ask(MESSAGES)

    assert fake_api.requests == []


# ---------------------------------------------------------------- перебор моделей


async def test_upstream_429_moves_to_next_model_without_punishing_first(fake_api, build):
    _catalog(fake_api, "a/one:free", "b/two:free")
    fake_api.reply("POST", CHAT, status=429, json={"error": {"message": "upstream"}})
    fake_api.answer("з другої")
    fake_api.answer("знову з першої")
    client = build(upstream_quarantine_seconds=0.0)

    first = await client.ask(MESSAGES)
    second = await client.ask(MESSAGES)

    assert first.model == "b/two:free"
    assert second.model == "a/one:free"  # первая модель не в карантине


async def test_upstream_429_stops_after_limit_and_starts_global_pause(fake_api, build):
    _catalog(fake_api, "a/one:free", "b/two:free", "c/three:free")
    for _ in range(3):
        fake_api.reply("POST", CHAT, status=429, json={})
    client = build(max_models_tried=3, max_limited_attempts=2)

    with pytest.raises(LLMQuotaExceeded):
        await client.ask(MESSAGES)

    assert len(fake_api.chat_models()) == 2  # D30: не больше двух попыток на разных моделях
    with pytest.raises(LLMQuotaExceeded):  # общая пауза: новые запросы к провайдеру не уходят
        await client.ask(MESSAGES)
    assert len(fake_api.chat_models()) == 2


async def test_upstream_429_without_pause_when_pause_is_zero(fake_api, build):
    _catalog(fake_api, "a/one:free", "b/two:free")
    for _ in range(2):
        fake_api.reply("POST", CHAT, status=429, json={})
    fake_api.answer("пізніше")
    client = build(
        max_models_tried=2,
        max_limited_attempts=2,
        limit_pause_seconds=0.0,
        upstream_quarantine_seconds=0.0,
    )

    with pytest.raises(LLMQuotaExceeded):
        await client.ask(MESSAGES)

    assert (await client.ask(MESSAGES)).text == "пізніше"


async def test_json_mode_is_sent_only_to_models_that_support_it(fake_api, build):
    with_json = {
        **model_entry("a/json:free", context=100_000),
        "supported_parameters": ["response_format"],
    }
    fake_api.catalog(with_json, model_entry("b/plain:free", context=90_000))
    fake_api.key_info(remaining=40)
    fake_api.reply("POST", CHAT, status=503, json={})
    fake_api.answer("ok")

    await build().ask(MESSAGES)

    first, second = [r.json for r in fake_api.calls("POST", CHAT)]
    assert first["response_format"] == {"type": "json_object"}
    assert "response_format" not in second


async def test_400_without_marker_does_not_quarantine_model(fake_api, build):
    _catalog(fake_api, "a/one:free", "b/two:free")
    fake_api.reply("POST", CHAT, status=400, json={"error": {"message": "invalid json in request"}})
    fake_api.answer("ok")
    fake_api.answer("ok")
    client = build()

    await client.ask(MESSAGES)
    await client.ask(MESSAGES)

    assert fake_api.chat_models() == ["a/one:free", "b/two:free", "a/one:free"]


async def test_404_model_not_found_quarantines_model(fake_api, build):
    _catalog(fake_api, "a/one:free", "b/two:free")
    fake_api.reply("POST", CHAT, status=404, json={"error": {"message": "No endpoints found"}})
    fake_api.answer("ok")
    fake_api.answer("ok")
    client = build()

    await client.ask(MESSAGES)
    await client.ask(MESSAGES)

    assert fake_api.chat_models() == ["a/one:free", "b/two:free", "b/two:free"]


async def test_quarantine_expires(fake_api, build, ftime):
    _catalog(fake_api, "a/one:free", "b/two:free")
    fake_api.reply("POST", CHAT, status=404, json={"error": {"message": "no endpoints found"}})
    fake_api.answer("ok")
    fake_api.answer("ok")
    client = build(model_quarantine_seconds=600)

    await client.ask(MESSAGES)
    ftime.now += 601
    await client.ask(MESSAGES)

    assert fake_api.chat_models()[-1] == "a/one:free"


@pytest.mark.parametrize(
    "bad_reply",
    [
        {"json": {"choices": [{"message": {"content": "   "}}]}},
        {"status": 503, "json": {}},
        {"drop": True},
    ],
)
async def test_bad_replies_move_to_next_model(fake_api, build, bad_reply):
    _catalog(fake_api, "a/one:free", "b/two:free")
    fake_api.reply("POST", CHAT, **bad_reply)
    fake_api.answer("добре")

    result = await build().ask(MESSAGES)

    assert (result.text, result.model) == ("добре", "b/two:free")


async def test_answer_failing_validation_moves_to_next_model(fake_api, build):
    _catalog(fake_api, "a/one:free", "b/two:free")
    fake_api.answer("поганий")
    fake_api.answer("добрий")

    client = build(invalid_response_strategy="next_model")

    result = await client.ask(MESSAGES, validate=lambda text: text == "добрий")

    assert result.model == "b/two:free"


async def test_attempts_are_limited_by_max_models_tried(fake_api, build):
    _catalog(fake_api, "a:free", "b:free", "c:free")
    for _ in range(3):
        fake_api.reply("POST", CHAT, status=503, json={})

    with pytest.raises(LLMUnavailable):
        await build(max_models_tried=2).ask(MESSAGES)

    assert len(fake_api.chat_models()) == 2


# ---------------------------------------------------------------- ключи


async def test_401_quarantines_key_and_next_key_is_used(fake_api, build):
    _catalog(fake_api, "a/one:free")
    fake_api.reply("POST", CHAT, status=401, json={})
    fake_api.answer("ok")
    client = build()

    result = await client.ask(MESSAGES)

    assert result.key_index == 1
    assert [c.headers["authorization"] for c in fake_api.calls("POST", CHAT)] == [
        "Bearer key-1",
        "Bearer key-2",
    ]
    fake_api.answer("ok")
    second = await client.ask(MESSAGES)
    assert second.key_index == 1  # первый ключ ещё в карантине


async def test_all_keys_rejected_means_unavailable_then_fast_fail(fake_api, build):
    _catalog(fake_api, "a/one:free")
    fake_api.reply("POST", CHAT, status=401, json={}, times=2)
    client = build()

    with pytest.raises(LLMUnavailable):
        await client.ask(MESSAGES)
    calls_before = len(fake_api.requests)
    with pytest.raises(LLMUnavailable, match="ключ"):
        await client.ask(MESSAGES)

    assert len(fake_api.requests) == calls_before


async def test_402_with_key_limit_switches_key(fake_api, build):
    _catalog(fake_api, "a/one:free")
    fake_api.reply(
        "POST",
        CHAT,
        status=402,
        json={"error": {"metadata": {"limit_source": "openrouter_key_limit"}}},
    )
    fake_api.answer("ok")

    result = await build().ask(MESSAGES)

    assert result.key_index == 1


async def test_403_quarantines_pair_then_model_when_all_keys_refuse(fake_api, build):
    _catalog(fake_api, "a/one:free", "b/two:free")
    fake_api.reply("POST", CHAT, status=403, json={}, times=2)
    fake_api.answer("ok")
    fake_api.answer("ok")
    client = build()

    first = await client.ask(MESSAGES)
    second = await client.ask(MESSAGES)

    assert first.model == "b/two:free"
    assert fake_api.chat_models() == ["a/one:free", "a/one:free", "b/two:free", "b/two:free"]
    assert second.model == "b/two:free"  # a/one в карантине целиком


# ---------------------------------------------------------------- паузы и лимиты


async def test_platform_429_pauses_everything(fake_api, build, ftime):
    _catalog(fake_api, "a/one:free")
    fake_api.reply("POST", CHAT, status=429, headers={**PLATFORM_429, "Retry-After": "30"}, json={})
    fake_api.answer("після паузи")
    client = build()

    with pytest.raises(LLMQuotaExceeded) as first:
        await client.ask(MESSAGES)
    assert first.value.retry_after == 30.0

    calls = len(fake_api.requests)
    with pytest.raises(LLMQuotaExceeded):
        await client.ask(MESSAGES)
    assert len(fake_api.requests) == calls  # во время паузы в сеть не ходим

    ftime.now += 31
    assert (await client.ask(MESSAGES)).text == "після паузи"


async def test_platform_429_without_retry_after_uses_configured_pause(fake_api, build):
    _catalog(fake_api, "a/one:free")
    fake_api.reply("POST", CHAT, status=429, headers=PLATFORM_429, json={})

    with pytest.raises(LLMQuotaExceeded) as caught:
        await build(rate_pause_seconds=77).ask(MESSAGES)

    assert caught.value.retry_after == 77


async def test_402_blocks_account_and_pauses(fake_api, build, ftime):
    _catalog(fake_api, "a/one:free")
    fake_api.reply("POST", CHAT, status=402, json={"error": {"message": "insufficient credits"}})
    client = build(account_pause_seconds=900)

    with pytest.raises(LLMAccountBlocked) as caught:
        await client.ask(MESSAGES)
    assert caught.value.retry_after == 900

    calls = len(fake_api.requests)
    with pytest.raises(LLMAccountBlocked):
        await client.ask(MESSAGES)
    assert len(fake_api.requests) == calls


async def test_rpm_limiter_waits_for_a_slot(fake_api, build, ftime):
    _catalog(fake_api, "a/one:free")
    fake_api.answer("1")
    fake_api.answer("2")
    client = build(max_rpm=1, deadline_seconds=120)

    await client.ask(MESSAGES)
    second = await client.ask(MESSAGES)

    assert second.text == "2"
    assert ftime.sleeps == [pytest.approx(60.0)]


async def test_rpm_limiter_gives_up_at_deadline(fake_api, build):
    _catalog(fake_api, "a/one:free")
    fake_api.answer("1")
    client = build(max_rpm=1, deadline_seconds=45)

    await client.ask(MESSAGES)
    with pytest.raises(LLMQuotaExceeded) as caught:
        await client.ask(MESSAGES)

    assert caught.value.retry_after == pytest.approx(60.0)
    assert len(fake_api.chat_models()) == 1


# ---------------------------------------------------------------- квота


async def test_exhausted_daily_quota_stops_before_chat(fake_api, build):
    _catalog(fake_api, "a/one:free", quota=0)

    with pytest.raises(LLMQuotaExceeded) as caught:
        await build().ask(MESSAGES)

    assert caught.value.retry_after == 2 * 3600  # до полуночи UTC осталось 2 часа
    assert fake_api.calls("POST", CHAT) == []


async def test_quota_is_consumed_locally_between_refreshes(fake_api, build, ftime):
    _catalog(fake_api, "a/one:free", quota=1)
    fake_api.answer("ok")
    client = build(quota_cache_seconds=30)

    await client.ask(MESSAGES)
    with pytest.raises(LLMQuotaExceeded):
        await client.ask(MESSAGES)

    assert len(fake_api.calls("GET", "/key")) == 1  # второй раз остаток взят из кэша


async def test_quota_is_refreshed_after_ttl(fake_api, build, ftime):
    _catalog(fake_api, "a/one:free", quota=40)
    fake_api.answer("1")
    fake_api.answer("2")
    client = build(quota_cache_seconds=30)

    await client.ask(MESSAGES)
    ftime.now += 31
    await client.ask(MESSAGES)

    assert len(fake_api.calls("GET", "/key")) == 2


async def test_fallback_daily_limit_when_provider_is_silent(fake_api, build):
    fake_api.catalog(model_entry("a/one:free"))
    fake_api.reply("GET", "/key", status=404, json={}, times=None)
    fake_api.answer("1")
    client = build(daily_request_limit=1)

    await client.ask(MESSAGES)
    with pytest.raises(LLMQuotaExceeded):
        await client.ask(MESSAGES)


# ---------------------------------------------------------------- каталог


async def test_catalog_is_cached(fake_api, build):
    _catalog(fake_api, "a/one:free")
    fake_api.answer("1")
    fake_api.answer("2")
    client = build()

    await client.ask(MESSAGES)
    await client.ask(MESSAGES)

    assert len(fake_api.calls("GET", "/models")) == 1


async def test_stale_catalog_is_used_when_refresh_fails(fake_api, build, ftime):
    fake_api.reply("GET", "/models", json={"data": [model_entry("a/one:free")]})
    fake_api.reply("GET", "/models", status=500, json={})
    fake_api.key_info(remaining=40)
    fake_api.answer("1")
    fake_api.answer("2")
    client = build(catalog_ttl_seconds=60)

    await client.ask(MESSAGES)
    ftime.now += 61
    second = await client.ask(MESSAGES)

    assert second.model == "a/one:free"


async def test_catalog_failure_without_cache_is_unavailable(fake_api, build):
    fake_api.reply("GET", "/models", status=500, json={})

    with pytest.raises(LLMUnavailable):
        await build().ask(MESSAGES)


async def test_unknown_provider_is_a_config_error(fake_api, http_session):
    from config_data.config import ConfigError

    config = make_llm_config(fake_api.base_url, provider="nope")

    with pytest.raises(ConfigError):
        create_provider(config, http_session)


async def test_model_forbidden_for_several_keys_is_disabled_without_trying_every_key(
    fake_api, build
):
    _catalog(fake_api, "a/closed:free", "b/open:free")
    fake_api.reply("POST", CHAT, status=403, json={"error": {"message": "forbidden"}}, times=2)
    fake_api.answer("ok")
    fake_api.answer("ok")
    client = build(api_keys=("key-1", "key-2", "key-3"), pair_failures_before_model_quarantine=2)

    result = await client.ask(MESSAGES)

    assert result.model == "b/open:free"
    assert fake_api.chat_models() == ["a/closed:free", "a/closed:free", "b/open:free"]  # 2, не 3
    again = await client.ask(MESSAGES)  # закрытая модель отключена на время карантина
    assert again.model == "b/open:free"


async def test_error_message_from_provider_is_logged(fake_api, build, caplog):
    import logging

    _catalog(fake_api, "a/one:free", "b/two:free")
    fake_api.reply(
        "POST", CHAT, status=403, json={"error": {"message": "Region   not supported"}}, times=2
    )
    fake_api.answer("ok")
    caplog.set_level(logging.WARNING, logger="external_services.llm.llm_client")

    await build().ask(MESSAGES)

    assert "Region not supported" in caplog.text  # пробелы нормализованы
    assert "key-1" not in caplog.text  # значение ключа в лог не попадает


async def test_model_restricted_to_agentic_harnesses_is_dropped_after_one_request(fake_api, build):
    """Живой случай: бесплатные модели thinkingmachines/inkling* отвечают 403 на любой ключ."""
    _catalog(fake_api, "a/harness-only:free", "b/open:free")
    fake_api.reply(
        "POST",
        CHAT,
        status=403,
        json={"error": {"message": "a/harness-only:free is only available on agentic harnesses."}},
    )
    fake_api.answer("ok")
    fake_api.answer("ok")
    client = build(api_keys=("key-1", "key-2", "key-3"))

    first = await client.ask(MESSAGES)
    second = await client.ask(MESSAGES)

    assert first.model == second.model == "b/open:free"
    # один запрос к закрытой модели на все ключи, затем она на карантине
    assert fake_api.chat_models() == ["a/harness-only:free", "b/open:free", "b/open:free"]


# ------------------------------------------- D30, ТЗ §12 и документация OpenRouter


async def test_upstream_429_quarantines_that_model_briefly_and_next_question_skips_it(
    fake_api, build, ftime
):
    _catalog(fake_api, "a/one:free", "b/two:free")
    fake_api.reply("POST", CHAT, status=429, json={}, headers={"Retry-After": "30"})
    fake_api.answer("з другої")
    fake_api.answer("знову з другої")
    fake_api.answer("знову з першої")
    client = build()

    await client.ask(MESSAGES)
    second = await client.ask(MESSAGES)
    ftime.now += 61  # карантин (60 с, не короче Retry-After 30) истёк
    third = await client.ask(MESSAGES)

    assert (second.model, third.model) == ("b/two:free", "a/one:free")


async def test_strict_retry_repeats_same_model_with_stricter_messages(fake_api, build):
    _catalog(fake_api, "a/one:free", "b/two:free")
    fake_api.answer("не json")
    fake_api.answer("добрий")
    strict = [{"role": "user", "content": "СТРОГО"}]

    result = await build().ask(MESSAGES, validate=lambda t: t == "добрий", retry_messages=strict)

    assert result.model == "a/one:free"
    sent = [r.json for r in fake_api.calls("POST", CHAT)]
    assert [m["model"] for m in sent] == ["a/one:free", "a/one:free"]
    assert sent[1]["messages"] == strict


async def test_strict_retry_that_fails_gives_up_instead_of_trying_more_models(fake_api, build):
    _catalog(fake_api, "a/one:free", "b/two:free", "c/three:free")
    for _ in range(3):
        fake_api.answer("не json")

    with pytest.raises(LLMUnavailable):
        await build().ask(
            MESSAGES, validate=lambda t: False, retry_messages=[{"role": "user", "content": "x"}]
        )

    assert len(fake_api.chat_models()) == 2  # одна попытка и один строгий повтор


async def test_in_flight_budget_402_pauses_for_retry_after_without_blocking_account(
    fake_api, build
):
    _catalog(fake_api, "a/one:free", "b/two:free")
    fake_api.reply(
        "POST",
        CHAT,
        status=402,
        json={
            "error": {
                "message": "budget",
                "metadata": {"limit_source": "openrouter_in_flight_budget"},
            }
        },
        headers={"Retry-After": "9"},
    )
    client = build()

    with pytest.raises(LLMQuotaExceeded) as info:
        await client.ask(MESSAGES)

    assert info.value.retry_after == 9.0
    assert not isinstance(info.value, LLMAccountBlocked)
    assert len(fake_api.chat_models()) == 1  # другие модели не тратим: бюджет общий


async def test_credits_402_blocks_account(fake_api, build):
    _catalog(fake_api, "a/one:free")
    fake_api.reply(
        "POST",
        CHAT,
        status=402,
        json={"error": {"message": "credits", "metadata": {"limit_source": "openrouter_credits"}}},
    )

    with pytest.raises(LLMAccountBlocked):
        await build().ask(MESSAGES)


async def test_400_after_json_mode_retries_without_it_and_keeps_model(fake_api, build):
    entry = {
        **model_entry("a/json:free", context=100_000),
        "supported_parameters": ["response_format"],
    }
    fake_api.catalog(entry)
    fake_api.key_info(remaining=40)
    fake_api.reply("POST", CHAT, status=400, json={"error": {"message": "bad param"}})
    fake_api.answer("ok")
    fake_api.answer("ok")
    client = build()

    await client.ask(MESSAGES)
    await client.ask(MESSAGES)

    bodies = [r.json for r in fake_api.calls("POST", CHAT)]
    assert "response_format" in bodies[0]
    assert all("response_format" not in b for b in bodies[1:])  # модель не отключена


async def test_failed_requests_count_toward_local_quota_when_enabled(fake_api, build):
    fake_api.catalog(
        model_entry("a/one:free", context=100_000)
    )  # /key недоступен: счётчик локальный
    fake_api.reply("POST", CHAT, status=503, json={})
    fake_api.reply("POST", CHAT, status=503, json={})
    client = build(daily_request_limit=2, max_models_tried=1)

    for _ in range(2):
        with pytest.raises(LLMUnavailable):
            await client.ask(MESSAGES)

    with pytest.raises(LLMQuotaExceeded) as info:
        await client.ask(MESSAGES)
    assert info.value.daily is True


async def test_failed_requests_are_not_counted_when_disabled(fake_api, build):
    fake_api.catalog(model_entry("a/one:free", context=100_000))
    fake_api.reply("POST", CHAT, status=503, json={})
    fake_api.reply("POST", CHAT, status=503, json={})
    fake_api.answer("ok")
    client = build(daily_request_limit=2, max_models_tried=1, count_failed_requests=False)

    for _ in range(2):
        with pytest.raises(LLMUnavailable):
            await client.ask(MESSAGES)

    assert (await client.ask(MESSAGES)).text == "ok"
