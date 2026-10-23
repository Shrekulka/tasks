"""Поддельный сервер OpenRouter для тестов: настоящий HTTP на локальном порту.

Почему не aioresponses: его последняя версия (0.7.9) несовместима с aiohttp 3.14. Сервер на
aiohttp.test_utils не зависит от внутренностей клиента, а адаптер проверяется по-настоящему:
заголовки, JSON, обрыв соединения, таймаут.
"""

import asyncio
import json
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from aiohttp import web
from aiohttp.test_utils import TestServer

from config_data.config import LLMConfig

API_PREFIX = "/api/v1"


@dataclass(slots=True)
class Reply:
    status: int = 200
    json: Any = None
    body: str | None = None  # сырое тело (не JSON)
    headers: dict[str, str] = field(default_factory=dict)
    drop: bool = False  # оборвать соединение без ответа
    delay: float = 0.0
    times: int | None = 1  # None: отвечать так всегда


@dataclass(frozen=True, slots=True)
class Recorded:
    method: str
    path: str
    headers: dict[str, str]
    json: Any


class FakeOpenRouter:
    def __init__(self) -> None:
        self._queues: dict[tuple[str, str], deque[Reply]] = {}
        self.requests: list[Recorded] = []
        self.unexpected: list[tuple[str, str]] = []
        self._server: TestServer | None = None

    # ------------------------------------------------------------------ настройка

    def reply(self, method: str, path: str, **kwargs: Any) -> None:
        self._queues.setdefault((method, path), deque()).append(Reply(**kwargs))

    def catalog(self, *models: dict[str, Any]) -> None:
        self.reply("GET", "/models", json={"data": list(models)}, times=None)

    def key_info(self, *, remaining: int, limit: int = 50) -> None:
        used = limit - remaining
        payload = {
            "data": {
                "free_model_daily_requests": {"limit": limit, "used": used, "remaining": remaining}
            }
        }
        self.reply("GET", "/key", json=payload, times=None)

    def answer(self, text: str = "Порада", **kwargs: Any) -> None:
        self.reply("POST", "/chat/completions", json=chat_payload(text), **kwargs)

    # ------------------------------------------------------------------ чтение

    def calls(self, method: str, path: str) -> list[Recorded]:
        return [r for r in self.requests if r.method == method and r.path == path]

    def chat_models(self) -> list[str]:
        return [r.json["model"] for r in self.calls("POST", "/chat/completions")]

    @property
    def base_url(self) -> str:
        assert self._server is not None
        return str(self._server.make_url(API_PREFIX))

    # ------------------------------------------------------------------ сервер

    async def start(self) -> None:
        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", self._handle)
        self._server = TestServer(app)
        await self._server.start_server()

    async def stop(self) -> None:
        if self._server is not None:
            await self._server.close()

    async def _handle(self, request: web.Request) -> web.StreamResponse:
        path = request.path.removeprefix(API_PREFIX)
        raw = await request.text()
        payload = json.loads(raw) if raw else None
        self.requests.append(
            Recorded(
                request.method, path, {k.lower(): v for k, v in request.headers.items()}, payload
            )
        )

        queue = self._queues.get((request.method, path))
        if not queue:
            self.unexpected.append((request.method, path))
            return web.json_response({"error": {"message": "unexpected request"}}, status=500)

        reply = queue[0]
        if reply.times is not None:
            reply.times -= 1
            if reply.times <= 0:
                queue.popleft()

        if reply.delay:
            await asyncio.sleep(reply.delay)
        if reply.drop:
            assert request.transport is not None
            request.transport.close()
            return web.Response()
        if reply.body is not None:
            return web.Response(
                status=reply.status,
                text=reply.body,
                content_type="text/html",
                headers=reply.headers,
            )
        return web.json_response(reply.json or {}, status=reply.status, headers=reply.headers)


# ---------------------------------------------------------------------- данные


def chat_payload(text: str) -> dict[str, Any]:
    return {"choices": [{"message": {"content": text}}]}


def model_entry(model_id: str, *, context: int = 32000, price: str = "0") -> dict[str, Any]:
    return {
        "id": model_id,
        "pricing": {"prompt": price, "completion": price},
        "architecture": {"input_modalities": ["text"], "output_modalities": ["text"]},
        "context_length": context,
    }


def make_llm_config(base_url: str, **overrides: Any) -> LLMConfig:
    values: dict[str, Any] = {
        "provider": "openrouter",
        "api_keys": ("key-1", "key-2"),
        "models": (),
        "models_blocklist": (),
        "base_url": base_url,
        "timeout_seconds": 5.0,
        "deadline_seconds": 45.0,
        "max_models_tried": 3,
        "max_rpm": 15,
        "min_context": 8000,
        "catalog_ttl_seconds": 3600,
        "rate_pause_seconds": 60.0,
        "account_pause_seconds": 900.0,
        "key_quarantine_seconds": 3600.0,
        "model_quarantine_seconds": 600.0,
        "quota_cache_seconds": 30.0,
        "daily_request_limit": 0,
        "pair_failures_before_model_quarantine": 2,
        "temperature": 0.1,
        "max_tokens": 600,
        "max_limited_attempts": 2,
        "limit_pause_seconds": 60.0,
        "json_mode": "auto",
        "invalid_response_strategy": "strict_retry",
        "upstream_quarantine_seconds": 60.0,
        "count_failed_requests": True,
        "daily_reset_threshold_seconds": 3600.0,
    }
    values.update(overrides)
    return LLMConfig(**values)


class FakeTime:
    """Управляемое время: клиент не ждёт по-настоящему."""

    def __init__(self, now: float = 1000.0) -> None:
        self.now = now
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds
