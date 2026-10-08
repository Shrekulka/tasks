# python_style_extractor/scripts/utils.py

"""Общие утилиты pipeline «Figma → Landing».

Модуль содержит функции, которыми пользуются и `figma_extractor.py`, и
`generate_landing.py`: нормализацию идентификаторов Figma, маскирование секретов
для логов, загрузку JSON-конфигов, получение списка бесплатных моделей и поиск
ключевых слов в тексте.
"""

import json
import logging
import re
from pathlib import Path
from typing import Any

import requests

logger = logging.getLogger(__name__)


def normalize_figma_id(node_id: str) -> str:
    """Приводит идентификатор узла Figma к формату, который API использует как ключ в ответах.

    Args:
        node_id: Идентификатор узла (в URL Figma он записывается через дефис,
            например `2196-13915`).

    Returns:
        str: Идентификатор через двоеточие (`2196:13915`).
    """
    return node_id.replace("-", ":")


def mask_secret(secret: str, prefix: int = 6, suffix: int = 4) -> str:
    """Маскирует секрет (API-ключ, токен) для безопасного логирования.

    Единая точка маскирования для токенов Figma и API-ключей LLM. Секрет,
    который не длиннее `prefix + suffix + 3`, скрывается целиком.

    Args:
        secret: Секрет.
        prefix: Сколько первых символов оставить.
        suffix: Сколько последних символов оставить. Значение должно быть
            больше 0: при `suffix=0` срез `secret[-0:]` равен всей строке, и
            секрет попал бы в результат целиком.

    Returns:
        str: `<prefix>...<suffix>` либо `"***"`, если секрет слишком короткий.
    """
    min_len = prefix + suffix + 3  # +3 под "..."
    if len(secret) <= min_len:
        return "***"
    return f"{secret[:prefix]}...{secret[-suffix:]}"


class ConfigLoader:
    """Единая точка загрузки JSON-конфигов.

    Используется и `figma_extractor.py`, и `generate_landing.py`, чтобы поведение
    загрузки (и будущие форматы конфигов) не расходилось между скриптами.
    """

    @staticmethod
    def load(path: Path) -> dict[str, Any]:
        """Читает JSON-файл конфигурации.

        Args:
            path: Путь к файлу в кодировке UTF-8.

        Returns:
            dict[str, Any]: Содержимое файла.

        Raises:
            FileNotFoundError: Если файла нет (причина пишется в лог).
            json.JSONDecodeError: Если содержимое не является корректным JSON
                (причина пишется в лог).
        """
        if not path.exists():
            logger.error(f"Файл конфигурации не найден: {path.resolve()}")
            raise FileNotFoundError(f"Файл конфигурации не найден: {path}")
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            logger.error(f"Ошибка синтаксиса JSON в файле {path.name}: {e}")
            raise


def get_free_models(base_url: str, api_key: str) -> list[str]:
    """Получает список бесплатных моделей OpenAI-совместимого провайдера.

    Бесплатными считаются модели, `id` которых заканчивается на `:free`
    (например, у OpenRouter). Запрашивается `<base_url>/models`. Результат
    используется для логирования и диагностики.

    Args:
        base_url: Базовый URL провайдера без завершающего `/`.
        api_key: API-ключ (передаётся как `Bearer`-токен).

    Returns:
        list[str]: Отсортированные `id` бесплатных моделей. Пустой список, если
        `base_url` или `api_key` не заданы либо запрос не удался (причина пишется
        в лог как предупреждение).
    """
    if not base_url or not api_key:
        return []
    try:
        resp = requests.get(
            f"{base_url}/models",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=30
        )
        resp.raise_for_status()
        data = resp.json().get("data", [])
        free_models = [m["id"] for m in data if m.get("id", "").endswith(":free")]
        return sorted(free_models)
    except Exception as e:
        logger.warning(f"⚠️ Не удалось получить список моделей: {e}")
        return []


def keyword_match(text: str, keywords: list[str]) -> bool:
    """Проверяет, содержит ли текст хотя бы одно из ключевых слов как отдельное слово.

    Совпадение идёт по границе слова, а не по вхождению подстроки: иначе `start`
    находило бы `started` и `start earning`, а `box` находило бы `checkbox`.
    Регистр не учитывается, спецсимволы в ключевых словах экранируются.

    Args:
        text: Проверяемый текст.
        keywords: Ключевые слова.

    Returns:
        bool: `True`, если найдено хотя бы одно слово; `False` для пустого списка.
    """
    return any(re.search(rf"\b{re.escape(w)}\b", text, re.IGNORECASE) for w in keywords)
