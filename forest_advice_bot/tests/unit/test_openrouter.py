import pytest

from external_services.llm.base import ProviderTransportError
from external_services.llm.openrouter import DEFAULT_BASE_URL, OpenRouterProvider
from tests.fake_openrouter import model_entry


@pytest.fixture
def provider(fake_api, http_session):
    return OpenRouterProvider(http_session, fake_api.base_url)


async def test_list_models_returns_only_mappings(provider, fake_api):
    fake_api.reply(
        "GET", "/models", json={"data": [model_entry("a:free"), "junk", model_entry("b:free")]}
    )

    models = await provider.list_models(timeout_seconds=5)

    assert [m["id"] for m in models] == ["a:free", "b:free"]


async def test_list_models_does_not_send_a_key(provider, fake_api):
    fake_api.reply("GET", "/models", json={"data": []})

    await provider.list_models(timeout_seconds=5)

    assert "authorization" not in fake_api.calls("GET", "/models")[0].headers


@pytest.mark.parametrize("status", [500, 404])
async def test_list_models_error_status_raises(provider, fake_api, status):
    fake_api.reply("GET", "/models", status=status, json={})

    with pytest.raises(ProviderTransportError):
        await provider.list_models(timeout_seconds=5)


async def test_list_models_dropped_connection_raises(provider, fake_api):
    fake_api.reply("GET", "/models", drop=True)

    with pytest.raises(ProviderTransportError):
        await provider.list_models(timeout_seconds=5)


async def test_list_models_timeout_raises(provider, fake_api):
    fake_api.reply("GET", "/models", json={"data": []}, delay=1.0)

    with pytest.raises(ProviderTransportError):
        await provider.list_models(timeout_seconds=0.05)


async def test_complete_sends_key_and_merges_options(provider, fake_api):
    fake_api.answer("ok")

    response = await provider.complete(
        "vendor/model:free",
        [{"role": "user", "content": "привіт"}],
        api_key="sk-or-secret",
        timeout_seconds=5,
        options={"temperature": 0.1, "model": "evil/paid", "messages": []},
    )

    (call,) = fake_api.calls("POST", "/chat/completions")
    assert call.headers["authorization"] == "Bearer sk-or-secret"
    assert call.json["temperature"] == 0.1
    assert call.json["model"] == "vendor/model:free"  # options не перекрывают model
    assert call.json["messages"] == [{"role": "user", "content": "привіт"}]
    assert response.status == 200
    assert provider.extract_text(response.body) == "ok"


async def test_complete_keeps_status_and_headers_of_errors(provider, fake_api):
    fake_api.reply(
        "POST",
        "/chat/completions",
        status=429,
        headers={"X-RateLimit-Limit": "20"},
        json={"error": {"message": "rl"}},
    )

    response = await provider.complete("m:free", [], api_key="k", timeout_seconds=5, options={})

    assert response.status == 429
    assert response.headers["X-RateLimit-Limit"] == "20"


async def test_complete_non_json_body_becomes_empty(provider, fake_api):
    fake_api.reply("POST", "/chat/completions", status=502, body="<html>bad gateway</html>")

    response = await provider.complete("m:free", [], api_key="k", timeout_seconds=5, options={})

    assert response.status == 502
    assert response.body == {}


@pytest.mark.parametrize(
    ("error", "expected_status"),
    [
        ({"code": 429, "message": "rate limited"}, 429),
        ({"message": "weird"}, 502),
        ({"code": 200, "message": "not an error code"}, 502),
    ],
)
async def test_error_inside_200_body_is_an_error(provider, fake_api, error, expected_status):
    fake_api.reply("POST", "/chat/completions", json={"error": error})

    response = await provider.complete("m:free", [], api_key="k", timeout_seconds=5, options={})

    assert response.status == expected_status


async def test_complete_dropped_connection_raises(provider, fake_api):
    fake_api.reply("POST", "/chat/completions", drop=True)

    with pytest.raises(ProviderTransportError):
        await provider.complete("m:free", [], api_key="k", timeout_seconds=5, options={})


async def test_fetch_quota_parses_daily_requests(provider, fake_api):
    fake_api.key_info(remaining=46)

    quota = await provider.fetch_quota(api_key="k", timeout_seconds=5)

    assert quota is not None
    assert (quota.limit, quota.remaining) == (50, 46)


async def test_fetch_quota_none_on_error_or_missing_field(provider, fake_api):
    fake_api.reply("GET", "/key", status=401, json={})
    fake_api.reply("GET", "/key", json={"data": {"label": "x"}})

    assert await provider.fetch_quota(api_key="k", timeout_seconds=5) is None
    assert await provider.fetch_quota(api_key="k", timeout_seconds=5) is None


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ({"choices": [{"message": {"content": "  текст \n"}}]}, "текст"),
        ({"choices": [{"message": {"content": "   "}}]}, None),
        ({"choices": [{"message": {"content": None}}]}, None),
        ({"choices": []}, None),
        ({"choices": ["x"]}, None),
        ({}, None),
    ],
)
def test_extract_text(body, expected):
    assert OpenRouterProvider(session=None).extract_text(body) == expected  # type: ignore[arg-type]


def test_base_url_defaults_and_is_normalized():
    custom = OpenRouterProvider(session=None, base_url="https://proxy.example/v1/")  # type: ignore[arg-type]
    default = OpenRouterProvider(session=None)  # type: ignore[arg-type]

    assert custom._base_url == "https://proxy.example/v1"
    assert default._base_url == DEFAULT_BASE_URL
