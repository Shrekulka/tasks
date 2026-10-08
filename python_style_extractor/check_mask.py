# python_style_extractor/check_mask.py

"""Диагностика кэшированных ответов Figma API (`data/raw/*.json`).

Одноразовый скрипт из двух независимых частей. Ключи файлов и id узлов зашиты в код:

    1. Показывает свойства маски (`isMask`, `maskType`, `blendMode`) одного узла.
    2. Находит все эффекты DROP_SHADOW в поддереве узла и печатает их параметры.

Запуск из корня проекта: `python check_mask.py`.
"""

import glob
import json
from typing import Any

# --- Часть 1: свойства маски узла ---
path = sorted(glob.glob("data/raw/tIsKpTW3zj9Hg09adBX5ZH_*.json"))[-1]
print(f"Файл: {path}")
data = json.loads(open(path, encoding="utf-8").read())


def find_node(node: dict[str, Any], target_id: str) -> dict[str, Any] | None:
    """Рекурсивно ищет узел с заданным `id` в поддереве.

    Args:
        node: Корень поддерева (узел Figma API).
        target_id: Искомый идентификатор узла.

    Returns:
        dict[str, Any] | None: Найденный узел; `None`, если узла нет.
    """
    if node.get("id") == target_id:
        return node
    for child in node.get("children", []):
        found = find_node(child, target_id)
        if found:
            return found
    return None


doc = data["nodes"]["1122:6758"]["document"]
node = find_node(doc, "1122:6765")

if node:
    print("name:", node.get("name"))
    print("isMask:", node.get("isMask"))
    print("maskType:", node.get("maskType"))
    print("blendMode:", node.get("blendMode"))
else:
    print("Узел 1122:6765 не найден")

# --- Часть 2: все эффекты DROP_SHADOW в поддереве узла ---
import json
import glob

path = sorted(glob.glob("data/raw/DOHrSfiqMosu9hdVeW6CEd_*.json"))[-1]
print(f"Файл: {path}")
data = json.loads(open(path, encoding="utf-8").read())


def find_all_drop_shadows(node: dict[str, Any], results: list[dict[str, Any]]) -> None:
    """Рекурсивно собирает эффекты DROP_SHADOW поддерева.

    Для каждого такого эффекта (в том числе невидимого) в `results` добавляется
    словарь с полями `name`, `id`, `visible`, `radius`, `color`, `offset`.

    Args:
        node: Корень поддерева (узел Figma API).
        results: Список, который дополняется найденными эффектами (изменяется на месте).
    """
    for effect in node.get("effects", []):
        if effect.get("type") == "DROP_SHADOW":
            results.append({
                "name": node.get("name"),
                "id": node.get("id"),
                "visible": effect.get("visible"),
                "radius": effect.get("radius"),
                "color": effect.get("color"),
                "offset": effect.get("offset"),
            })
    for child in node.get("children", []):
        find_all_drop_shadows(child, results)


doc = data["nodes"]["2196:13914"]["document"]
found = []
find_all_drop_shadows(doc, found)

for item in found:
    print(item)

print(f"\nВсего DROP_SHADOW найдено: {len(found)}")
