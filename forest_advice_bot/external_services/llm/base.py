# forest-advice-bot/external_services/llm/base.py

"""Интерфейс LLM-провайдера. Клиент моделей зависит только от него."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from external_services.llm.quota import DailyQuota


class ProviderTransportError(Exception):
    """Ответа провайдера нет: сеть, таймаут, недоступный каталог. Секретов в тексте нет."""


@dataclass(frozen=True, slots=True)
class ModelInfo:
    id: str
    context_length: int
    supported_parameters: frozenset[str] = frozenset()  # что модель умеет (response_format ...)

    def supports(self, parameter: str) -> bool:
        return parameter in self.supported_parameters


@dataclass(frozen=True, slots=True)
class JsonModeSpec:
    """Как провайдер включает JSON-режим: параметр в каталоге и добавка к запросу."""

    capability: str  # имя в supported_parameters модели
    options: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class ProviderResponse:
    """Сырой ответ провайдера: статус, заголовки, JSON. Без интерпретации."""

    status: int
    headers: Mapping[str, str] = field(default_factory=dict)
    body: Mapping[str, Any] = field(default_factory=dict)


class LLMProvider(Protocol):
    name: str

    async def list_models(self, *, timeout_seconds: float) -> Sequence[Mapping[str, Any]]:
        """Сырой каталог провайдера (до фильтрации). Ошибка: ProviderTransportError."""

    async def complete(
        self,
        model: str,
        messages: Sequence[Mapping[str, str]],
        *,
        api_key: str,
        timeout_seconds: float,
        options: Mapping[str, Any],
    ) -> ProviderResponse:
        """Один запрос без ретраев. Ретраи и карантины решает клиент.

        Нет ответа (сеть, таймаут): ProviderTransportError.
        """

    async def fetch_quota(self, *, api_key: str, timeout_seconds: float) -> DailyQuota | None:
        """Остаток дневной квоты или None, если провайдер его не сообщает."""

    def extract_text(self, body: Mapping[str, Any]) -> str | None:
        """Текст ответа модели из тела успешного ответа."""
