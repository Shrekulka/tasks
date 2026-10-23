# forest-advice-bot/scripts/list_free_models.py

"""Печатает бесплатные модели провайдера после того же fail-closed фильтра, что и бот.

Запуск из корня проекта: python -m scripts.list_free_models
По списку выбирайте значения для LLM_MODELS. Конфигу нужен LLM_API_KEYS, но каталог моделей
публичный, и ключ в этом запросе не отправляется.
"""

import asyncio
import sys

import aiohttp

from config_data.config import LLMConfig, load_llm_config
from external_services.llm.base import ModelInfo, ProviderTransportError
from external_services.llm.free_filter import select_free_models
from external_services.llm.registry import create_provider


async def fetch_free_models(config: LLMConfig, session: aiohttp.ClientSession) -> list[ModelInfo]:
    bundle = create_provider(config, session)
    catalog = await bundle.provider.list_models(timeout_seconds=config.timeout_seconds)
    return select_free_models(
        catalog,
        bundle.free_policy,
        pinned=config.models,
        blocklist=config.models_blocklist,
    )


async def main() -> int:
    config = load_llm_config()
    async with aiohttp.ClientSession() as session:
        try:
            models = await fetch_free_models(config, session)
        except ProviderTransportError as exc:
            print(f"Каталог недоступен: {exc}", file=sys.stderr)
            return 1

    if not models:
        print("Бесплатных моделей, прошедших фильтр, нет.")
        return 2
    print(f"Бесплатные модели ({config.provider}), контекст по убыванию:")
    for info in models:
        print(f"  {info.id}  контекст={info.context_length}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
