# forest-advice-bot/external_services/llm/registry.py

"""Реестр провайдеров. Новый провайдер подключается одной записью в _FACTORIES."""

from collections.abc import Callable
from dataclasses import dataclass

import aiohttp

from config_data.config import ConfigError, LLMConfig
from external_services.llm.base import JsonModeSpec, LLMProvider
from external_services.llm.error_policy import ErrorPolicy
from external_services.llm.free_filter import FreeModelPolicy
from external_services.llm.openrouter import (
    ERROR_POLICY,
    JSON_MODE,
    OpenRouterProvider,
    build_free_policy,
)


@dataclass(frozen=True, slots=True)
class ProviderBundle:
    """Всё, что клиенту нужно знать о провайдере: адаптер и его политики."""

    provider: LLMProvider
    free_policy: FreeModelPolicy
    error_policy: ErrorPolicy
    json_mode: JsonModeSpec | None = None  # None: провайдер JSON-режим не предоставляет


ProviderFactory = Callable[[aiohttp.ClientSession, LLMConfig], ProviderBundle]


def _openrouter(session: aiohttp.ClientSession, config: LLMConfig) -> ProviderBundle:
    return ProviderBundle(
        provider=OpenRouterProvider(session, config.base_url),
        free_policy=build_free_policy(config.min_context),
        error_policy=ERROR_POLICY,
        json_mode=JSON_MODE,
    )


_FACTORIES: dict[str, ProviderFactory] = {"openrouter": _openrouter}


def supported_providers() -> tuple[str, ...]:
    return tuple(sorted(_FACTORIES))


def create_provider(config: LLMConfig, session: aiohttp.ClientSession) -> ProviderBundle:
    factory = _FACTORIES.get(config.provider)
    if factory is None:
        raise ConfigError(
            {
                "LLM_PROVIDER": [
                    f"неизвестный провайдер «{config.provider}», доступны: "
                    f"{', '.join(supported_providers())}"
                ]
            }
        )
    return factory(session, config)
