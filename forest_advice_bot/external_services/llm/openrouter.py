# forest-advice-bot/external_services/llm/openrouter.py

"""Адаптер OpenRouter. Все особенности этого провайдера собраны здесь."""

from collections.abc import Mapping, Sequence
from typing import Any

import aiohttp

from external_services.llm.base import JsonModeSpec, ProviderResponse, ProviderTransportError
from external_services.llm.error_policy import ErrorPolicy
from external_services.llm.free_filter import FreeModelPolicy
from external_services.llm.quota import DailyQuota, parse_daily_quota

DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"
QUOTA_FIELD = "free_model_daily_requests"

# Стартовые значения признаков ошибок. Маркеры сообщений по 400 и 404 уточните по реальным
# ответам, которые залогируете: формат текста провайдер документирует не полностью.
ERROR_POLICY = ErrorPolicy(
    platform_rate_headers=("x-ratelimit-limit", "x-ratelimit-remaining", "x-ratelimit-reset"),
    key_limit_sources=frozenset({"openrouter_key_limit"}),
    model_missing_markers=("no endpoints found", "not a valid model", "model not found"),
    model_unsupported_markers=("not supported",),
    # Живой ответ 403: «<модель> is only available on agentic harnesses…»
    model_restricted_markers=("only available on",),
    # Документация OpenRouter: in-flight budget временный и несёт Retry-After; кредиты и
    # бюджеты guardrail/workspace означают, что повторять запросы бесполезно.
    transient_budget_sources=frozenset({"openrouter_in_flight_budget"}),
    account_blocked_sources=frozenset(
        {"openrouter_credits", "openrouter_guardrail_budget", "openrouter_workspace_budget"}
    ),
    rate_reset_header="x-ratelimit-reset",
)


JSON_MODE = JsonModeSpec("response_format", {"response_format": {"type": "json_object"}})


def build_free_policy(min_context: int) -> FreeModelPolicy:
    return FreeModelPolicy(
        free_id_suffix=":free",
        min_context=min_context,
        required_input=frozenset({"text"}),
        required_output=frozenset({"text"}),
        required_prices=("prompt", "completion"),
    )


class OpenRouterProvider:
    name = "openrouter"

    def __init__(self, session: aiohttp.ClientSession, base_url: str = "") -> None:
        self._session = session
        self._base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")

    async def list_models(self, *, timeout_seconds: float) -> Sequence[Mapping[str, Any]]:
        response = await self._request("GET", "/models", timeout_seconds=timeout_seconds)
        data = response.body.get("data")
        if response.status != 200 or not isinstance(data, list):
            raise ProviderTransportError(f"каталог моделей недоступен (HTTP {response.status})")
        return [item for item in data if isinstance(item, Mapping)]

    async def complete(
        self,
        model: str,
        messages: Sequence[Mapping[str, str]],
        *,
        api_key: str,
        timeout_seconds: float,
        options: Mapping[str, Any],
    ) -> ProviderResponse:
        # model и messages задаются здесь и не могут быть перекрыты options.
        payload = {**options, "model": model, "messages": list(messages)}
        response = await self._request(
            "POST",
            "/chat/completions",
            api_key=api_key,
            json=payload,
            timeout_seconds=timeout_seconds,
        )
        return self._unwrap_error_in_body(response)

    async def fetch_quota(self, *, api_key: str, timeout_seconds: float) -> DailyQuota | None:
        response = await self._request(
            "GET", "/key", api_key=api_key, timeout_seconds=timeout_seconds
        )
        if response.status != 200:
            return None
        return parse_daily_quota(response.body, QUOTA_FIELD)

    def extract_text(self, body: Mapping[str, Any]) -> str | None:
        choices = body.get("choices")
        if not isinstance(choices, list) or not choices:
            return None
        message = choices[0].get("message") if isinstance(choices[0], Mapping) else None
        content = message.get("content") if isinstance(message, Mapping) else None
        if not isinstance(content, str):
            return None
        return content.strip() or None

    # ------------------------------------------------------------------ внутреннее

    async def _request(
        self,
        method: str,
        path: str,
        *,
        timeout_seconds: float,
        api_key: str | None = None,
        json: Mapping[str, Any] | None = None,
    ) -> ProviderResponse:
        headers = {"Accept": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        try:
            async with self._session.request(
                method,
                f"{self._base_url}{path}",
                headers=headers,
                json=json,
                timeout=aiohttp.ClientTimeout(total=timeout_seconds),
            ) as response:
                body = await self._read_json(response)
                return ProviderResponse(
                    status=response.status, headers=dict(response.headers), body=body
                )
        except (aiohttp.ClientError, TimeoutError) as exc:
            # Текст исключения может содержать адрес, но не ключ; в лог идёт только тип.
            raise ProviderTransportError(type(exc).__name__) from exc

    @staticmethod
    async def _read_json(response: aiohttp.ClientResponse) -> Mapping[str, Any]:
        try:
            data = await response.json(content_type=None)
        except (ValueError, aiohttp.ClientError):
            return {}
        return data if isinstance(data, dict) else {}

    @staticmethod
    def _unwrap_error_in_body(response: ProviderResponse) -> ProviderResponse:
        """Защитная мера: ошибка внутри тела при статусе 200 считается ошибкой, а не ответом."""
        error = response.body.get("error")
        if response.status != 200 or not isinstance(error, Mapping):
            return response
        code = error.get("code")
        status = code if isinstance(code, int) and 400 <= code <= 599 else 502
        return ProviderResponse(status=status, headers=response.headers, body=response.body)
