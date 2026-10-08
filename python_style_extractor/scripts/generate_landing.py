# python_style_extractor/scripts/generate_landing.py

"""Оркестрация pipeline «Figma → Landing»: сборка HTML-лендинга из двух Figma-источников.

Модуль объединяет два источника: дизайн-систему (визуальный стиль) и композицию
(структура и контент). Визуальный стиль переносится на композицию, после чего
результат проходит многоуровневую QA-проверку.

Стадии, выполняемые поверх готового IR (`figma_extractor.py`, `models.py`):

    загрузка конфигурации -> загрузка spec -> семантический анализ
    (`SemanticResolver`, `ComponentResolver`) -> перенос стиля
    (`StyleTransferEngine`) -> рендер (`WebRenderer`) -> QA (`QAEngine`)

Входные данные:
    * манифест лендинга (аргумент `--config`, по умолчанию `configs/landing_manifest.json`);
    * spec, собранный `figma_extractor.py` (файл `<имя манифеста>_spec.json` в `SPECS_DIR`);
    * правила pipeline из `configs/*.json` (читаются один раз при импорте модуля);
    * файл `.env` в корне проекта с API-ключами LLM.

Результат: каталог `output/run_<timestamp>/` с `index.html`, скриншотами,
диагностическими HTML-файлами и отчётом `qa_report.json`.

Запуск:
    python -m scripts.generate_landing --config configs/landing_manifest.json
"""

import argparse
import asyncio
import base64
import datetime
import hashlib
import html
import json
import logging
import re
import shutil
import time
from pathlib import Path
from typing import Any, Literal
from typing import get_args

from PIL import Image, ImageChops
from environs import Env
from langchain_core.messages import HumanMessage
from langchain_openai import ChatOpenAI
from playwright.async_api import async_playwright, ViewportSize
from pydantic import SecretStr

from scripts.color_resolution import (
    sample_dominant_colors_from_png,
    sample_accent_color_from_asset,
    collect_background_candidates,
    parse_color_to_rgb_tuple,
    alpha_from_rgba,
    analyze_opaque_region,
    sample_transparent_backing_rgb,
    retint_bg_color_toward_token,
    compute_retint_filter,
    apply_retint_to_rgba,
    parse_rgba_channels,
    resolve_reliable_accent_token,
    rgba_lightness,
    pick_lightest_token,
    pick_contrasting_text_color,
)
from scripts.component_heuristics import ComponentHeuristics
from scripts.figma_extractor import parse_rgba
from scripts.ir_fidelity import IRFidelityChecker
from scripts.models import (
    LandingSpec,
    ResolvedSectionSpec,
    SectionSpec,
    DesignSystemSpec,
    DesignTokens,
    IRNode,
    SemanticMap,
    QAConfig,
    RenderConfig,
    TypographyToken,
    ResolutionResult,
    SemanticRole,
    HTMLTag,
)
from scripts.paths import PROJECT_ROOT, CONFIGS_DIR, SPECS_DIR, OUTPUT_BASE, ASSETS_DIR
from scripts.utils import ConfigLoader, mask_secret, get_free_models, keyword_match

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger("generate_landing")
env = Env()
env.read_env(PROJECT_ROOT / ".env")


def _build_schema_hint() -> str:
    """Формирует JSON-образец ответа LLM для семантической разметки узлов.

    Допустимые значения `semantic_role` и `html_tag` берутся из типов
    `SemanticRole` и `HTMLTag`, поэтому образец в промпте не расходится
    с моделями данных.

    Returns:
        str: JSON-строка с отступами: список `decisions` с полями `id`,
        `semantic_role`, `html_tag` и `confidence`.
    """
    roles = "|".join(get_args(SemanticRole))
    tags = "|".join(get_args(HTMLTag))
    return json.dumps({"decisions": [
        {"id": "<node_id>", "semantic_role": f"one of: {roles}", "html_tag": f"one of: {tags}",
         "confidence": "float 0.0-1.0"}]}, indent=2)


# Образец структуры ответа LLM; подставляется в промпт
# `SemanticResolver.resolve_ambiguities_with_llm`.
SEMANTIC_MAP_SCHEMA_HINT = _build_schema_hint()

# Правила pipeline читаются один раз при импорте модуля из JSON-файлов в `configs/`.
# Источник значений (пороги, ключевые слова, маппинги ролей) — эти файлы, поэтому
# поведение pipeline меняется правкой JSON без изменения кода. Литералы в вызовах
# `.get(ключ, значение)` ниже — запасные значения на случай отсутствия ключа.
#   defaults.json          - значения по умолчанию для запасных сценариев (цвета и т.п.)
#   font_registry.json     - соответствие Figma-шрифта веб-шрифту и его @import
#   semantic_rules.json    - пороги и ключевые слова для ролей текста (stat, button и т.д.)
#   analysis_rules.json    - пороги детекции компонентов, декоративности и адаптивности
#   render_rules.json      - маппинг ролей на токены цвета и типографики, CSS-константы
#   pipeline_settings.json - настройки pipeline (LLM, QA, хранение результатов)
# Ошибка загрузки любого файла фатальна: без корректных правил результат
# непредсказуем, поэтому pipeline падает сразу при импорте.
try:
    DEFAULTS = ConfigLoader.load(CONFIGS_DIR / "defaults.json")
    FONT_REGISTRY = ConfigLoader.load(CONFIGS_DIR / "font_registry.json")
    SEMANTIC_RULES = ConfigLoader.load(CONFIGS_DIR / "semantic_rules.json")
    ANALYSIS_RULES = ConfigLoader.load(CONFIGS_DIR / "analysis_rules.json")
    RENDER_RULES = ConfigLoader.load(CONFIGS_DIR / "render_rules.json")
    PIPELINE_SETTINGS = ConfigLoader.load(CONFIGS_DIR / "pipeline_settings.json")
except Exception as e:
    logger.critical(f"Ошибка загрузки конфигураций: {e}")
    raise

# Регулярное выражение для числовых метрик (роль `stat`) компилируется один раз.
# Если паттерн в `semantic_rules.json` не задан, правило `stat` не применяется.
_STAT_REGEX_PATTERN = SEMANTIC_RULES.get("regex_patterns", {}).get("stat")
try:
    STAT_REGEX = re.compile(_STAT_REGEX_PATTERN, re.IGNORECASE) if _STAT_REGEX_PATTERN else None
except re.error as exc:
    logger.critical(f"Некорректный stat regex в semantic_rules.json: {exc}")
    raise ValueError("Некорректный regex regex_patterns.stat") from exc

# Шрифты, для которых замена уже записана в лог: сообщение выводится один раз
# за запуск, а не для каждого текстового узла.
_font_substitution_logged: set[str] = set()

# Порог яркости (0-255). В областях темнее SSIM ненадёжен: слагаемое яркости
# в его формуле резко нелинейно около нуля. Значение подобрано эмпирически.
# Используется в `QAEngine.compute_masked_metrics`.
LOW_LUMINANCE_THRESHOLD = 30

# JS для `page.evaluate()`: ждёт загрузки всех веб-шрифтов, использованных на
# странице. Без этого скриншот может быть снят со шрифтом-заменителем.
FONT_WAIT_SCRIPT = """
async () => {
    if (!document.fonts) return;
    try {
        const families = new Set();
        for (const el of document.querySelectorAll('*')) {
            const ff = getComputedStyle(el).fontFamily;
            if (ff) families.add(ff);
        }
        await Promise.all(
            Array.from(families).map(f =>
                document.fonts.load(`16px ${f}`).catch(() => {})
            )
        );
        await document.fonts.ready;
    } catch (e) {}
}
"""

# JS для `page.evaluate()` и для вставки в HTML: уменьшает font-size текстов,
# которые не помещаются в отведённое им место. Два прохода описаны внутри скрипта.
AUTOFIT_SCRIPT = """
() => {
    const MIN_FONT_SIZE = 8;
    const MAX_ITERATIONS = 40;

    // Проход 1: тексты с фиксированной колонкой (атрибут data-autofit).
    // Сравниваем scrollWidth и clientWidth самого элемента: это работает только
    // тогда, когда у текста есть собственная фиксированная ширина.
    document.querySelectorAll('[data-autofit]').forEach((el) => {
        let iterations = MAX_ITERATIONS;
        while (el.scrollWidth > el.clientWidth + 1 && iterations > 0) {
            const currentSize = parseFloat(getComputedStyle(el).fontSize);
            if (!currentSize || currentSize <= MIN_FONT_SIZE) break;
            el.style.fontSize = (currentSize - 0.5) + 'px';
            iterations -= 1;
        }
    });

    // Проход 2: текст с шириной по содержимому (HUG) внутри Figma Auto Layout,
    // за которым следует соседний элемент (иконка, бейдж, счётчик).
    // Собственной колонки у такого текста нет (width: max-content), поэтому
    // scrollWidth и clientWidth не подходят: сравниваем getBoundingClientRect()
    // самого текста и следующего соседа в DOM. Отступ берётся из атрибута
    // data-autofit-guard-gap: это itemSpacing родительского Auto Layout-фрейма,
    // переданный из Python.
    document.querySelectorAll('[data-autofit-guard-gap]').forEach((el) => {
        const parent = el.parentElement;
        if (!parent) return;
        const siblings = Array.from(parent.children);
        const next = siblings[siblings.indexOf(el) + 1];
        if (!next) return;

        const gap = parseFloat(el.dataset.autofitGuardGap) || 0;
        let iterations = MAX_ITERATIONS;
        while (iterations > 0) {
            const elRight = el.getBoundingClientRect().right;
            const nextLeft = next.getBoundingClientRect().left;
            if (elRight + gap <= nextLeft + 0.5) break;
            const currentSize = parseFloat(getComputedStyle(el).fontSize);
            if (!currentSize || currentSize <= MIN_FONT_SIZE) break;
            el.style.fontSize = (currentSize - 0.5) + 'px';
            iterations -= 1;
        }
    });
}
"""

# Порядок серьёзности замечаний AI Visual QA (`low` < `medium` < `high`).
# Сравнивается с порогом `gate_min_severity` в `evaluate_ai_visual_gate`.
_AI_QA_SEVERITY_RANK = {"low": 0, "medium": 1, "high": 2}


# ---------------------------------------------------------------------------
# Шрифты
# ---------------------------------------------------------------------------
def resolve_font(family: str) -> tuple[str, str]:
    """Разрешает Figma-шрифт в доступный веб-шрифт и CSS-строку его подключения.

    Обрабатываются три случая:
      1. `family` — оригинальное имя из Figma (ключ в `FONT_REGISTRY`).
      2. `family` — уже разрешённое имя (значение `family` в записи реестра).
         Так бывает после первого прохода `StyleTransferEngine`: в дереве лежит,
         например, `Inter`, и повторный вызов должен найти `import_url`.
      3. Шрифта нет в реестре: возвращается исходное имя и пустой `@import`,
         в лог пишется предупреждение (браузер применит системный стек).

    Args:
        family: Имя шрифта: оригинальное из Figma либо уже разрешённое.

    Returns:
        tuple[str, str]: Пара (итоговое имя семейства, строка `@import url(...)`).
        Строка пустая, если `import_url` не задан или шрифт не найден в реестре.
    """
    entry = FONT_REGISTRY.get(family) if isinstance(FONT_REGISTRY, dict) else None
    if isinstance(entry, dict):
        resolved_family = str(entry.get("family", family)).strip()
        import_url = entry.get("import_url")
        import_stmt = f"@import url('{import_url}');" if import_url else ""
        if resolved_family != family and family not in _font_substitution_logged:
            _font_substitution_logged.add(family)
            logger.info(
                f"ℹ️ Шрифт Figma '{family}' заменён на визуально похожий доступный web-шрифт '{resolved_family}' (font_registry.json). Это ЗАМЕНА, а не точный перенос оригинального файла шрифта."
            )

        return resolved_family, import_stmt

    if isinstance(FONT_REGISTRY, dict):
        for registry_entry in FONT_REGISTRY.values():
            if not isinstance(registry_entry, dict):
                continue
            registered_family = str(registry_entry.get("family", "")).strip()
            if registered_family != family:
                continue
            import_url = registry_entry.get("import_url")
            import_stmt = f"@import url('{import_url}');" if import_url else ""
            return family, import_stmt

    logger.warning(f"Шрифт '{family}' не зарегистрирован в font_registry.json. Будет применён системный стек.")
    return family, ""


def resolve_font_weight_offset(family: str) -> int:
    """Возвращает поправку жирности для пары «Figma-шрифт -> веб-заменитель».

    Поправка (может быть отрицательной) хранится в `font_registry.json`.
    Она компенсирует разницу визуальной насыщенности: у разных гарнитур
    один и тот же числовой `font-weight` выглядит по-разному. Поправка
    зависит только от пары шрифтов, а не от конкретного узла или секции.

    Args:
        family: Имя семейства шрифта (оригинальное из Figma или уже
            разрешённое веб-имя) — ключ поиска в `font_registry.json`.

    Returns:
        int: Поправка веса; 0, если семейство не найдено в реестре.
    """
    entry = FONT_REGISTRY.get(family) if isinstance(FONT_REGISTRY, dict) else None
    if isinstance(entry, dict):
        return int(entry.get("weight_offset", 0))

    if isinstance(FONT_REGISTRY, dict):
        for registry_entry in FONT_REGISTRY.values():
            if not isinstance(registry_entry, dict):
                continue
            if str(registry_entry.get("family", "")).strip() == family:
                return int(registry_entry.get("weight_offset", 0))

    return 0


def apply_font_weight_offset(base_weight: int, family: str) -> int:
    """Применяет поправку веса и округляет результат до градации 100..900.

    Округление до сотен нужно, потому что именно эти градации подключаются
    через `@import` из `font_registry.json`. Значения ровно посередине
    (например, 450) округляются по правилу `round()` — к чётному.

    Args:
        base_weight: Исходный вес шрифта из Figma (например, 400 или 600).
        family: Имя семейства шрифта для поиска поправки
            (см. `resolve_font_weight_offset`).

    Returns:
        int: Итоговый вес шрифта, округлённый до градации 100..900.
    """
    offset = resolve_font_weight_offset(family)
    adjusted = base_weight + offset
    adjusted = max(100, min(900, adjusted))
    return round(adjusted / 100) * 100


# ---------------------------------------------------------------------------
# Геометрия для QA: bounding box'ы узлов
# ---------------------------------------------------------------------------
def collect_text_bboxes(section: ResolvedSectionSpec) -> list[tuple[float, float, float, float]]:
    """Собирает bounding box'ы всех TEXT-узлов секции с ненулевой площадью.

    Координаты заданы в пикселях референса, то есть в той же системе, что
    `extraction_*.png` и reference PNG: отсчёт от `rel_geometry` корня секции,
    смещение сбрасывается при `clips_content` (как в
    `ReferenceRenderer.render_original_tree`). Признак «это текст» берётся из
    `node.type`, а не из имени узла, поэтому функция не зависит от секции,
    файла и языка.

    Args:
        section: Секция, чьё дерево `original_root` обходится.

    Returns:
        list[tuple[float, float, float, float]]: Список bbox `(x0, y0, x1, y1)`,
        по одному на каждый TEXT-узел с положительными шириной и высотой.
    """
    root = section.original_root
    root_x = root.rel_geometry.x
    root_y = root.rel_geometry.y
    boxes: list[tuple[float, float, float, float]] = []

    def walk(n: IRNode, p_x: float, p_y: float) -> None:
        """Обходит поддерево `n`, добавляя bbox TEXT-узлов в `boxes`.

        Args:
            n: Текущий узел.
            p_x: Накопленное смещение по X от предков, px.
            p_y: Накопленное смещение по Y от предков, px.
        """
        abs_x = p_x + n.rel_geometry.x
        abs_y = p_y + n.rel_geometry.y
        if n.type == "TEXT" and n.rel_geometry.width > 0 and n.rel_geometry.height > 0:
            boxes.append((abs_x, abs_y, abs_x + n.rel_geometry.width, abs_y + n.rel_geometry.height))

        needs_clip_wrapper = n.layout.clips_content and n.rel_geometry.width > 0 and n.rel_geometry.height > 0
        if needs_clip_wrapper:
            for ch in n.children:
                walk(ch, 0.0, 0.0)
        else:
            for ch in n.children:
                walk(ch, abs_x, abs_y)

    walk(root, p_x=-root_x, p_y=-root_y)
    return boxes


def collect_raster_composite_bboxes(section: ResolvedSectionSpec) -> list[tuple[float, float, float, float]]:
    """Собирает bounding box'ы узлов с `render_strategy == "raster_composite"`.

    Это узлы, которые Figma заранее растеризовала в PNG из-за режимов
    наложения (например, COLOR_DODGE). Растровая текстура таких узлов не может
    совпасть попиксельно между рендерерами Figma и Chromium, поэтому QA
    проверяет их отдельным, более мягким порогом (см. `ssim_threshold_raster_composite`).
    Система координат и правило сброса смещения — те же, что в
    `collect_text_bboxes`. Критерий берётся из IR (`render_strategy`),
    поэтому функция работает на любой Figma-странице.

    Args:
        section: Секция, чьё дерево `original_root` обходится.

    Returns:
        list[tuple[float, float, float, float]]: Список bbox `(x0, y0, x1, y1)`,
        по одному на каждый raster_composite-узел с положительными размерами.
    """
    root = section.original_root
    root_x = root.rel_geometry.x
    root_y = root.rel_geometry.y
    boxes: list[tuple[float, float, float, float]] = []

    def walk(n: IRNode, p_x: float, p_y: float) -> None:
        """Обходит поддерево `n`, добавляя bbox raster_composite-узлов в `boxes`.

        Args:
            n: Текущий узел.
            p_x: Накопленное смещение по X от предков, px.
            p_y: Накопленное смещение по Y от предков, px.
        """
        abs_x = p_x + n.rel_geometry.x
        abs_y = p_y + n.rel_geometry.y
        if n.render_strategy == "raster_composite" and n.rel_geometry.width > 0 and n.rel_geometry.height > 0:
            boxes.append((abs_x, abs_y, abs_x + n.rel_geometry.width, abs_y + n.rel_geometry.height))

        needs_clip_wrapper = n.layout.clips_content and n.rel_geometry.width > 0 and n.rel_geometry.height > 0
        if needs_clip_wrapper:
            for ch in n.children:
                walk(ch, 0.0, 0.0)
        else:
            for ch in n.children:
                walk(ch, abs_x, abs_y)

    walk(root, p_x=-root_x, p_y=-root_y)
    return boxes


# ---------------------------------------------------------------------------
# Валидация конфигурации и разбор ответов LLM
# ---------------------------------------------------------------------------
def validate_color_tokens(ds_spec: "DesignSystemSpec", render_rules: dict) -> None:
    """Проверяет, что роли из `render_rules.json` есть в токенах дизайн-системы.

    Рассинхронизация конфига со spec должна приводить к понятной ошибке
    на старте, а не к `NoneType`-исключению в глубине рендерера.

    Args:
        ds_spec: Дизайн-система с доступными токенами цвета и типографики.
        render_rules: Содержимое `render_rules.json`; используются
            `role_color_map` и `typography_role_map`.

    Raises:
        ValueError: Если правила ссылаются на цветовой или типографический
            токен, которого нет в `ds_spec`.
    """
    role_color_map = render_rules.get("role_color_map", {})
    available_colors = set(ds_spec.tokens.colors.keys())
    missing_colors = set(role_color_map.values()) - available_colors
    if missing_colors:
        raise ValueError(
            f"render_rules.json ссылается на цветовые токены, которых нет в style_spec: {sorted(missing_colors)}. Доступные токены: {sorted(available_colors)}.")

    typography_role_map = render_rules.get("typography_role_map", {})
    available_typo = set(ds_spec.tokens.typography.keys())
    missing_typo = set(typography_role_map.values()) - available_typo
    if missing_typo:
        raise ValueError(
            f"render_rules.json ссылается на роли типографики, которых нет в style_spec: {sorted(missing_typo)}. Доступные роли: {sorted(available_typo)}.")

    logger.info(
        f"✓ Валидация токенов пройдена (цвета: {len(role_color_map)}, типографика: {len(typography_role_map)}).")


def clean_and_parse_json(raw_text: str) -> SemanticMap:
    """Извлекает из сырого ответа LLM JSON и приводит его к `SemanticMap`.

    Терпима к типичным отклонениям моделей от запрошенного формата:
    markdown-блокам кода, рассуждениям в `<think>...</think>`, альтернативным
    именам полей (`role`/`semantic_role`, `tag`/`html_tag`, `node_id`/`id`) и
    словарю верхнего уровня вместо списка `decisions`.

    Args:
        raw_text: Текстовый ответ модели как есть.

    Returns:
        SemanticMap: Проверенная карта решений по узлам.

    Raises:
        json.JSONDecodeError: Если после очистки текст не является JSON.
        ValueError: Если JSON не удалось привести ни к одному из
            поддерживаемых форматов. Текст ошибки содержит фразу
            «could not parse»: по ней `SemanticResolver._is_parse_or_validation_error`
            определяет, что виновата модель, и переходит к следующей.
        pydantic.ValidationError: Если структура распознана, но значения не
            проходят валидацию `SemanticMap`. В тексте ошибки есть
            «validation error»: по нему `_is_parse_or_validation_error`
            тоже переходит к следующей модели.
    """
    text = raw_text.strip()
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
    if "```" in text:
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text).strip()

    # Границы JSON зависят от того, массив это или объект верхнего уровня.
    stripped = text.lstrip()
    if stripped.startswith("["):
        start_idx, end_idx = text.find("["), text.rfind("]")
    else:
        start_idx, end_idx = text.find("{"), text.rfind("}")
    if start_idx != -1 and end_idx != -1 and end_idx > start_idx:
        text = text[start_idx: end_idx + 1]

    parsed = json.loads(text)
    # Алиас "type" не поддерживается намеренно: он конфликтует с типом узла Figma.
    ROLE_ALIASES = ("semantic_role", "role")
    TAG_ALIASES = ("html_tag", "tag", "element")
    ID_ALIASES = ("id", "node_id")
    CONF_ALIASES = ("confidence", "score")

    def _pick(d: dict, keys: tuple, default=None) -> Any:
        """Возвращает значение первого ключа из `keys`, найденного в `d`.

        Args:
            d: Словарь-источник.
            keys: Допустимые имена поля в порядке приоритета.
            default: Значение, если ни один ключ не найден.

        Returns:
            Найденное значение либо `default`.
        """
        for k in keys:
            if k in d:
                return d[k]
        return default

    if isinstance(parsed, dict) and "decisions" in parsed:
        return SemanticMap.model_validate(parsed)

    if isinstance(parsed, list):
        repaired = []
        for item in parsed:
            if not isinstance(item, dict):
                continue
            nid = _pick(item, ID_ALIASES)
            role = _pick(item, ROLE_ALIASES)
            tag = _pick(item, TAG_ALIASES)
            if nid and role and tag:
                repaired.append(
                    {"id": nid, "semantic_role": role, "html_tag": tag, "confidence": _pick(item, CONF_ALIASES, 0.7)})
        if repaired:
            return SemanticMap.model_validate({"decisions": repaired})

    if isinstance(parsed, dict):
        repaired = []
        for node_id, fields in parsed.items():
            if not isinstance(fields, dict):
                continue
            role = _pick(fields, ROLE_ALIASES)
            tag = _pick(fields, TAG_ALIASES)
            if role and tag:
                repaired.append({"id": node_id, "semantic_role": role, "html_tag": tag,
                                 "confidence": _pick(fields, CONF_ALIASES, 0.7)})
        if repaired:
            return SemanticMap.model_validate({"decisions": repaired})

    # Фраза "could not parse" обязательна: по ней `_is_parse_or_validation_error`
    # определяет проблему модели и переключается на следующую модель.
    raise ValueError(
        f"Could not parse model response into any known format. Raw JSON (first 300 chars): {json.dumps(parsed, ensure_ascii=False)[:300]}")


def auto_repair_nested_buttons(sections: list[ResolvedSectionSpec]) -> list[str]:
    """Разжалует вложенные `<button>` в `<span>`, не останавливая pipeline.

    Вызывается, если после `ComponentResolver.resolve_components` всё же
    осталась вложенность кнопок (например, из-за нового паттерна в Figma-файле).
    Самый глубокий узел-нарушитель получает роль `badge` и тег `span` с той же
    визуальной стилизацией. Это запасной механизм, а не основной.
    Изменяет `resolved_root` секций на месте.

    Args:
        sections: Секции после переноса стиля.

    Returns:
        list[str]: Сообщения о выполненных исправлениях (пустой список, если
        исправлять было нечего).
    """
    repaired = []

    def walk(node: IRNode, ancestor_button: IRNode | None, section_id: str) -> None:
        """Разжалует `node` из button в badge/span, если у него уже есть кнопка-предок.

        Args:
            node: Текущий узел.
            ancestor_button: Ближайший предок-кнопка или `None`.
            section_id: Идентификатор секции (для сообщений о правках).
        """
        is_btn = node.component_role == "button"
        if is_btn and ancestor_button is not None:
            node.component_role = "badge"
            node.html_tag = "span"
            repaired.append(f"[{section_id}] '{node.name}' разжалован из button в span (вложенность)")
            # После разжалования узел не считается кнопкой для своих потомков.
            is_btn = False
        for ch in node.children:
            walk(ch, node if is_btn else ancestor_button, section_id)

    for sec in sections:
        walk(sec.resolved_root, None, sec.id)
    return repaired


# ---------------------------------------------------------------------------
# Семантический анализ секции
# ---------------------------------------------------------------------------
async def process_one_section(sec: SectionSpec, h1_size: float, llm_cfg: dict, semaphore: asyncio.Semaphore) -> tuple[
    SectionSpec, dict[str, int]]:
    """Определяет семантическую роль и HTML-тег текстовых узлов одной секции.

    Сначала применяются детерминированные правила
    (`SemanticResolver.resolve_rules_first`). Узлы, которые правила не смогли
    классифицировать уверенно, уходят в LLM; если LLM отключён или недоступен,
    используется нейтральное fallback-решение. В конце проставляются роли
    компонентов (`ComponentResolver`). Изменяет `sec.root_node` на месте.

    Args:
        sec: Секция с IR-деревом (`root_node`) и метаданными текстовых узлов (`texts`).
        h1_size: Размер шрифта заголовка h1 дизайн-системы, px. Опорная точка
            типографических правил.
        llm_cfg: Раздел `llm` манифеста (`enabled`, `required`, модели, ключи).
        semaphore: Ограничитель числа одновременных запросов к LLM.

    Returns:
        tuple[SectionSpec, dict[str, int]]: Обработанная секция и телеметрия
        с ключами `total_nodes`, `rules_resolved`, `llm_resolved`, `fallback`.

    Raises:
        RuntimeError: Если в конфигурации `llm.required=true` при `llm.enabled=false`
            либо LLM обязателен, но недоступен (см. `resolve_ambiguities_with_llm`).
    """
    logger.info(f"[{sec.id}] Обработка секции '{sec.name}' ({len(sec.texts)} текстовых нод)...")
    node_index: dict[str, IRNode] = {}
    parent_map: dict[str, str] = {}

    def build_idx(n: IRNode, owner_id: str | None = None):
        """Заполняет `node_index` и `parent_map` для поддерева.

        Args:
            n: Текущий узел.
            owner_id: Идентификатор родителя `n`; `None` у корня.
        """
        node_index[n.id] = n
        if owner_id is not None:
            parent_map[n.id] = owner_id
        for c in n.children:
            build_idx(c, owner_id=n.id)

    build_idx(sec.root_node)
    ambiguous: dict[str, Any] = {}
    all_resolutions: list[ResolutionResult] = []
    local_telemetry = {"total_nodes": 0, "rules_resolved": 0, "llm_resolved": 0, "fallback": 0}
    section_area = max(sec.geometry.width, 0.0) * max(sec.geometry.height, 0.0)
    for nid, meta in sec.texts.items():
        local_telemetry["total_nodes"] += 1
        parent_id = parent_map.get(nid)
        parent_node = node_index.get(parent_id) if parent_id else None
        current_node = node_index.get(nid)
        res = SemanticResolver.resolve_rules_first(meta, h1_size, nid, parent_node=parent_node, node=current_node,
                                                   section_area=section_area)
        if res:
            all_resolutions.append(res)
        else:
            ambiguous[nid] = meta

    logger.info(
        f"[{sec.id}]   Правилами разрешено: {len(all_resolutions)} нод, амбивалентных для LLM: {len(ambiguous)} нод")
    if ambiguous:
        llm_enabled = llm_cfg.get("enabled", True) is not False
        llm_required = bool(llm_cfg.get("required", False))
        # Требовать LLM и одновременно отключить его — противоречие конфигурации:
        # лучше упасть сразу, чем молча работать без обязательного компонента.
        if not llm_enabled and llm_required:
            raise RuntimeError(
                f"[{sec.id}] Конфликт конфигурации: llm.required=true, но llm.enabled=false. Включите LLM (enabled=true) или снимите required=true в манифесте.")

        if llm_enabled:
            ai_resolutions = await SemanticResolver.resolve_ambiguities_with_llm(ambiguous, llm_cfg, semaphore)
            all_resolutions.extend(ai_resolutions.values())
        else:
            logger.info(f"[{sec.id}] 🧠 LLM отключён конфигурацией. Fallback для {len(ambiguous)} нод.")
            all_resolutions.extend(SemanticResolver.make_fallback(nid) for nid in ambiguous)

    for r in all_resolutions:
        if r.source == "rules":
            local_telemetry["rules_resolved"] += 1
        elif r.source == "llm":
            local_telemetry["llm_resolved"] += 1
        elif r.source == "fallback":
            local_telemetry["fallback"] += 1

        if r.node_id in node_index:
            target_node = node_index[r.node_id]
            target_node.semantic_role = r.semantic_role
            target_node.html_tag = r.html_tag

    ComponentResolver.resolve_components(sec.root_node)
    return sec, local_telemetry


# ---------------------------------------------------------------------------
# LLM: фабрика клиентов
# ---------------------------------------------------------------------------
class LLMFactory:
    """Создаёт LLM-клиент для любого OpenAI-совместимого эндпоинта.

    Провайдер (OpenRouter, OpenAI, Moonshot/Kimi, локальный vLLM и т.д.)
    определяется только значениями `base_url` и `model` из конфигурации,
    ветвления по названию провайдера в коде нет. Список API-ключей для ротации
    читается из переменной окружения, имя которой задано в `llm_cfg`.
    """

    @staticmethod
    def get_keys(llm_cfg: dict) -> list[str]:
        """Читает API-ключи из переменной окружения `llm_cfg["api_key_env"]`.

        Значение переменной может быть JSON-массивом строк либо списком через
        запятую, поэтому несколько ключей для ротации задаются только в `.env`.

        Args:
            llm_cfg: Настройки LLM. Обязательное поле `api_key_env` — имя
                переменной окружения с ключами.

        Returns:
            list[str]: Непустые ключи. Пустой список, если поле `api_key_env`
            не задано или переменная окружения пуста.
        """
        env_var = llm_cfg.get("api_key_env")
        if not isinstance(env_var, str) or not env_var:
            logger.error("В llm_cfg отсутствует обязательное поле 'api_key_env'.")
            return []

        raw_val = env.str(env_var, "")
        if not raw_val:
            return []

        raw_val = raw_val.strip()
        if raw_val.startswith("[") and raw_val.endswith("]"):
            try:
                parsed = json.loads(raw_val)
                if isinstance(parsed, list):
                    return [str(k).strip() for k in parsed if str(k).strip()]
            except (json.JSONDecodeError, TypeError) as err:
                logger.debug(f"Не удалось распарсить API-ключи как JSON-массив: {err}")

        # Запасной формат: ключи через запятую (key1, key2, key3).
        return [k.strip().strip("'\"") for k in raw_val.split(",") if k.strip().strip("'\"")]

    @staticmethod
    def create(llm_cfg: dict, api_key: str, pipeline_settings: dict):
        """Создаёт клиент `ChatOpenAI` для OpenAI-совместимого API.

        Args:
            llm_cfg: Настройки модели: `model` и `base_url` (обязательны),
                `temperature`, `timeout_seconds`, `supports_json_mode` (необязательны).
            api_key: API-ключ провайдера.
            pipeline_settings: Содержимое `pipeline_settings.json`; из раздела
                `llm` берётся `client_max_retries`.

        Returns:
            ChatOpenAI: Настроенный клиент. Если `supports_json_mode=true`,
            включается ответ в формате `json_object`.

        Raises:
            ValueError: Если не заданы `model`, `base_url` или `api_key`.
        """
        model_name = llm_cfg.get("model", "")
        if not model_name:
            raise ValueError("В llm_cfg отсутствует обязательное поле 'model'.")

        base_url = llm_cfg.get("base_url")
        if not base_url:
            raise ValueError("В llm_cfg отсутствует обязательное поле 'base_url'.")

        if not api_key:
            raise ValueError("API-ключ не передан для OpenAI-совместимого провайдера!")

        temperature = float(llm_cfg.get("temperature", 0.0))
        llm_settings = pipeline_settings.get("llm", {})
        timeout = float(llm_cfg.get("timeout_seconds", 60))
        max_retries = int(llm_settings.get("client_max_retries", 1))
        kwargs = {}
        if llm_cfg.get("supports_json_mode", False):
            kwargs["model_kwargs"] = {"response_format": {"type": "json_object"}}

        return ChatOpenAI(model=model_name, api_key=SecretStr(api_key), base_url=base_url, temperature=temperature,
                          timeout=timeout, max_retries=max_retries, **kwargs)


# ---------------------------------------------------------------------------
# LLM: визуальная QA-проверка (advisory)
# ---------------------------------------------------------------------------
class AIVisualQAReviewer:
    """Advisory-проверка финального рендера vision-моделью.

    Работает только с финальным скриншотом после переноса стиля
    (`final_view_<viewport>.png`): это единственный артефакт, отражающий
    результат переноса. Extraction QA сравнивает дотрансферное дерево
    (`ResolvedSectionSpec.original_root`) и поэтому не видит ошибок перекраски.

    Design Contract передаётся в промпт явным текстом, чтобы модель не путала
    нарушение с ожидаемым поведением: секции-получатели стиля должны отличаться
    от собственного референса по цвету и эффектам, это и есть цель переноса.
    Проверяется другое: не остались ли в финальном рендере акценты и эффекты
    из оригинала секции-получателя там, где должен быть стиль источника.

    Роли «источник композиции» и «источник стиля» формируются из данных
    манифеста, поэтому класс работает для любой пары Figma-файлов.
    """

    # Ожидаемая структура JSON-ответа модели; подставляется в промпт.
    RESULT_SCHEMA_HINT = (
        '{"status": "ok"|"warning"|"fail", "confidence": 0.0-1.0, '
        '"foreign_style_elements": [{"description": str, '
        '"category": "foreign_color"|"foreign_effect"|"foreign_asset_style"|"overall_coherence", '
        '"observed_in_section": str, '
        '"evidence": str, "severity": "low"|"medium"|"high"}]}'
    )

    @staticmethod
    def _encode_image_b64(path: Path) -> str:
        """Кодирует файл изображения в base64-строку для передачи в промпт.

        Args:
            path: Путь к файлу изображения.

        Returns:
            str: Содержимое файла в base64 (UTF-8).
        """
        return base64.b64encode(path.read_bytes()).decode("utf-8")

    @classmethod
    def build_design_contract(cls, composition_authority: str, visual_authority: str) -> str:
        """Формирует текст Design Contract для промпта визуального QA.

        Контракт разделяет ответственность: кто владеет композицией и
        контентом, а кто — визуальным языком.

        Args:
            composition_authority: Источник композиции и контента (секции-получатели стиля).
            visual_authority: Источник визуального стиля.

        Returns:
            str: Текст контракта: что сохранить, что унаследовать, что запрещено.
        """
        return (
            f"Composition/content authority (секции-получатели стиля) = {composition_authority}\n"
            f"Visual authority (источник стиля) = {visual_authority}\n\n"
            "Must preserve у секций-получателей: их собственный текст, контент, "
            "порядок и композицию элементов относительно ИХ ЖЕ reference-изображения.\n"
            "Must inherit у секций-получателей от visual authority: палитру, "
            "типографику, язык компонентов, радиусы, тени/эффекты, декоративный акцентный язык.\n\n"
            "Forbidden: у секций-получателей — акцентный цвет/glow/стиль компонентов ИЗ ИХ "
            "СОБСТВЕННОГО оригинального reference там, где по контракту должен был "
            "примениться акцент visual authority.\n\n"
            "НЕ считать нарушением: то, что финальный рендер секции-получателя НЕ похож "
            "на её собственный reference по цвету — так и должно быть, это и есть style transfer."
        )

    @classmethod
    async def review(
            cls,
            final_screenshot: Path,
            reference_style_source: Path,
            reference_composition_sources: list[Path],
            composition_authority: str,
            visual_authority: str,
            llm_cfg: dict,
            semaphore: asyncio.Semaphore,
    ) -> dict[str, Any]:
        """Просит vision-модель найти в финальном рендере нарушения Design Contract.

        Перебирает модели из `llm_cfg["models"]` (для каждой — все ключи) до
        первого успешного ответа. Политика отказоустойчивости та же, что у
        `SemanticResolver.resolve_ambiguities_with_llm`: при `required=false`
        недоступность LLM не блокирует pipeline, при `required=true` вызывает ошибку.

        Args:
            final_screenshot: Скриншот финальной страницы (первое изображение в промпте).
            reference_style_source: Референс источника стиля (второе изображение).
            reference_composition_sources: Оригинальные референсы секций-получателей;
                нужны только для проверки композиции, а не цвета.
            composition_authority: Идентификаторы секций-получателей стиля.
            visual_authority: Идентификатор источника стиля.
            llm_cfg: Настройки LLM (`models` или `model`, `api_key_env`, `required`
                и параметры клиента).
            semaphore: Ограничитель числа одновременных запросов к LLM.

        Returns:
            dict[str, Any]: Ответ модели по схеме `RESULT_SCHEMA_HINT` с
            дополнительным полем `_model_used`. Если проверка не выполнена:
            `{"status": "not_reviewed", "reason": ...}`.

        Raises:
            RuntimeError: Если `required=true`, а ключей или моделей нет либо
                все модели и ключи исчерпаны.
        """
        contract = cls.build_design_contract(composition_authority, visual_authority)
        prompt_text = (
            "You are a visual QA reviewer for a generated landing page.\n\n"
            f"{contract}\n\n"
            "The FIRST image is the FINAL rendered page. "
            "The SECOND image is the visual-authority style reference. "
            "All remaining images are the ORIGINAL (pre-style-transfer) references of the "
            "composition-authority sections, in their own original visual style — "
            "used ONLY to verify composition/content/ordering was preserved, "
            "NOT to expect matching colors.\n\n"
            "Answer ONLY the question: are there visual elements in the FINAL image that "
            "violate the Forbidden/Must-inherit/Must-preserve rules above?\n\n"
            f"Return ONLY JSON matching exactly this structure, no markdown fences:\n"
            f"{cls.RESULT_SCHEMA_HINT}"
        )
        keys = LLMFactory.get_keys(llm_cfg)
        models_to_try = [str(m).strip() for m in (llm_cfg.get("models") or [llm_cfg.get("model", "")]) if
                         str(m).strip()]
        llm_required = bool(llm_cfg.get("required", False))
        if not keys or not models_to_try:
            if llm_required:
                raise RuntimeError("AI Visual QA: required=true, но нет ключей/моделей.")
            return {"status": "not_reviewed", "reason": "no_keys_or_models"}

        images_b64 = [cls._encode_image_b64(final_screenshot), cls._encode_image_b64(reference_style_source)]
        images_b64.extend(cls._encode_image_b64(p) for p in reference_composition_sources)
        async with semaphore:
            for model_name in models_to_try:
                for api_key in keys:
                    try:
                        llm = LLMFactory.create(llm_cfg | {"model": model_name}, api_key, PIPELINE_SETTINGS)
                        message = HumanMessage(content=[{"type": "text", "text": prompt_text}, *[
                            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}} for b64 in
                            images_b64]])
                        response = await llm.ainvoke([message])
                        raw = response.content if isinstance(response.content, str) else str(response.content)
                        cleaned = raw.strip()
                        # Модель может обернуть JSON в markdown-блок: оставляем только сам объект.
                        if cleaned.startswith("```"):
                            cleaned = cleaned.strip("`")
                            cleaned = cleaned[cleaned.find("{"):]
                        parsed = json.loads(cleaned)
                        parsed["_model_used"] = model_name
                        return parsed
                    except Exception as err:
                        logger.warning(f"AI Visual QA: модель {model_name} не сработала ({err}), пробуем следующую.")
                        continue

        if llm_required:
            raise RuntimeError("AI Visual QA: required=true, но все модели/ключи исчерпаны.")
        return {"status": "not_reviewed", "reason": "all_models_exhausted"}


def evaluate_ai_visual_gate(ai_visual_review: dict[str, Any] | None, ai_qa_cfg: dict) -> tuple[bool, str]:
    """Переводит advisory-вердикт `AIVisualQAReviewer` в решение gate.

    Политика «что считать основанием отклонить релиз» вынесена из
    `AIVisualQAReviewer.review`: это решение команды (сколько ложных
    срабатываний допустимо), а не свойство модели. Все пороги берутся из
    `pipeline_settings.json` -> `visual_qa_ai`.

    Релиз блокируется, только если выполнены все три условия:
      1. `status == "fail"`;
      2. `confidence >= gate_min_confidence`;
      3. среди `foreign_style_elements` есть элемент с `severity >= gate_min_severity`.

    Результат считается и логируется всегда, но влияет на `report["passed"]`
    и `vp_passed` только при `ai_qa_cfg["enforce"] == True` (по умолчанию `False`).

    Args:
        ai_visual_review: Результат `AIVisualQAReviewer.review` либо `None`,
            если проверка не запускалась.
        ai_qa_cfg: Раздел `visual_qa_ai` из `pipeline_settings.json`
            (`gate_min_confidence`, `gate_min_severity`).

    Returns:
        tuple[bool, str]: `(passed, reason)`. `passed=False` означает, что gate
        обнаружил нарушение; `reason` — человекочитаемое объяснение решения.
    """
    if not ai_visual_review:
        return True, "ai_visual_review отсутствует (AI QA пропущен или ещё не запускался)"

    if ai_visual_review.get("status") != "fail":
        return True, f"status={ai_visual_review.get('status')}"

    min_confidence = float(ai_qa_cfg.get("gate_min_confidence", 0.9))
    confidence = float(ai_visual_review.get("confidence", 0.0) or 0.0)
    if confidence < min_confidence:
        return True, f"confidence={confidence} < gate_min_confidence={min_confidence}"

    min_severity_rank = _AI_QA_SEVERITY_RANK.get(str(ai_qa_cfg.get("gate_min_severity", "high")), 2)
    elements = ai_visual_review.get("foreign_style_elements") or []
    offending = [el for el in elements if
                 _AI_QA_SEVERITY_RANK.get(str(el.get("severity", "low")), 0) >= min_severity_rank]
    if not offending:
        return True, "нет foreign_style_elements с severity >= gate_min_severity"

    reasons = "; ".join(el.get("description", "") for el in offending[:3])
    return False, f"AI Visual QA обнаружил {len(offending)} нарушение(й): {reasons}"


# ---------------------------------------------------------------------------
# Семантический резолвер: роли текстовых узлов (правила + LLM)
# ---------------------------------------------------------------------------
class SemanticResolver:
    """Определяет `semantic_role` и `html_tag` для текстовых узлов IR-дерева.

    Двухступенчатая стратегия. Сначала работают быстрые детерминированные
    правила (`resolve_rules_first`); их пороги и ключевые слова берутся из
    `analysis_rules.json` и `semantic_rules.json`. Только узлы, которые правила
    не классифицировали уверенно, отправляются в LLM
    (`resolve_ambiguities_with_llm`, с ротацией ключей и моделей и переходом
    на нейтральную роль при полной недоступности LLM).
    """

    @staticmethod
    def make_fallback(node_id: str) -> ResolutionResult:
        """Создаёт нейтральное решение для узла, который не удалось классифицировать.

        Роль, тег и уверенность берутся из `semantic_rules.json` -> `fallback`.

        Args:
            node_id: Идентификатор узла.

        Returns:
            ResolutionResult: Решение с `source="fallback"`.
        """
        fb = SEMANTIC_RULES.get("fallback", {})
        return ResolutionResult(node_id=node_id, semantic_role=fb.get("semantic_role", "body"),
                                html_tag=fb.get("html_tag", "p"), source="fallback",
                                confidence=fb.get("confidence", 0.5))

    @classmethod
    def resolve_rules_first(
            cls, text_meta: dict, h1_size: float, node_id: str, parent_node: IRNode | None = None,
            node: IRNode | None = None, section_area: float = 0.0
    ) -> ResolutionResult | None:
        """Определяет роль текстового узла детерминированными правилами.

        Правила применяются каскадом, первое сработавшее возвращает результат:
        подпись кнопки -> числовая метрика (`stat`, с вето для декоративного
        текста) -> типографика (h1/h2/h3/caption по отношению к `h1_size`) ->
        ключевые слова. Пороги читаются из `analysis_rules.json` и
        `semantic_rules.json`.

        Args:
            text_meta: Метаданные текстового узла из spec (`text`, `name`,
                `font_size`, `font_weight`).
            h1_size: Размер шрифта h1 дизайн-системы, px. Опорная точка
                типографических правил.
            node_id: Идентификатор узла.
            parent_node: Родительский IR-узел; нужен, чтобы распознать
                подпись кнопки по форме родителя.
            node: Сам IR-узел; нужен для расчёта эффективной прозрачности и площади.
            section_area: Площадь секции, px². Используется для оценки доли
                узла (мелкий декоративный текст).

        Returns:
            ResolutionResult | None: Решение правил либо `None`, если ни одно
            правило не сработало уверенно. Такой узел уходит в LLM
            (см. `resolve_ambiguities_with_llm`).
        """
        txt = str(text_meta.get("text") or "").strip()
        name = str(text_meta.get("name") or "").strip().lower()
        try:
            size = float(text_meta.get("font_size") or 16.0)
        except (TypeError, ValueError):
            size = 16.0

        try:
            weight = int(text_meta.get("font_weight") or 400)
        except (TypeError, ValueError):
            weight = 400

        # Шаг 1. Подпись кнопки: родитель похож на кнопку (flex, отступы, ограниченная высота).
        if parent_node is not None:
            btn_cfg = ANALYSIS_RULES.get("button_detection", {})
            min_padding_vertical = float(btn_cfg.get("min_padding_vertical", 6.0))
            min_padding_horizontal = float(btn_cfg.get("min_padding_horizontal", 12.0))
            max_button_height = float(btn_cfg.get("max_height", 68.0))
            parent_text_children = [child for child in parent_node.children if child.type == "TEXT"]

            # Бейдж проверяется раньше кнопки: у него приоритет.
            is_badge = ComponentHeuristics.looks_like_badge(parent_node, parent_text_children)
            if not is_badge:
                is_button_shaped = (
                        parent_node.layout.is_flex
                        and parent_node.layout.padding_top >= min_padding_vertical
                        and parent_node.layout.padding_bottom >= min_padding_vertical
                        and parent_node.layout.padding_left >= min_padding_horizontal
                        and parent_node.layout.padding_right >= min_padding_horizontal
                        and 0 < parent_node.layout.height <= max_button_height
                )
                if is_button_shaped:
                    return ResolutionResult(node_id=node_id, semantic_role="button_label", html_tag="span",
                                            source="rules", confidence=0.95)

        # Шаг 2. Признаки декоративного текста: низкая непрозрачность или подходящее имя узла.
        # Результат используется как вето в шаге 3.
        dec_cfg = SEMANTIC_RULES.get("decorative_text_detection", {})
        low_alpha_threshold = float(dec_cfg.get("low_alpha_threshold", 0.35))
        secondary_alpha_threshold = float(dec_cfg.get("secondary_alpha_threshold", 0.65))
        small_area_ratio = float(dec_cfg.get("small_area_ratio", 0.005))
        if node is not None:
            if node.style.text_color:
                effective_alpha = alpha_from_rgba(node.style.text_color)

            else:
                effective_alpha = max(0.0, min(float(node.style.opacity), 1.0))

        else:
            effective_alpha = 1.0

        node_area = 0.0
        if node is not None:
            node_area = max(float(node.rel_geometry.width), 0.0) * max(float(node.rel_geometry.height), 0.0)

        area_ratio = node_area / section_area if section_area > 0.0 else 0.0
        decorative_name_keywords = dec_cfg.get("name_keywords", [])
        name_says_decorative = bool(decorative_name_keywords) and keyword_match(name, decorative_name_keywords)
        strongly_decorative = effective_alpha < low_alpha_threshold or name_says_decorative
        weakly_decorative = effective_alpha < secondary_alpha_threshold and 0.0 < area_ratio < small_area_ratio

        # Шаг 3. Числовая метрика (`stat`): текст целиком похож на число, проценты или сумму.
        stat_cfg = SEMANTIC_RULES.get("stat_detection", {})
        regex_matches_stat = bool(STAT_REGEX and STAT_REGEX.fullmatch(txt))
        if regex_matches_stat:
            # Декоративное число (полупрозрачное или мелкое) — это подпись, а не метрика.
            if strongly_decorative or weakly_decorative:
                return ResolutionResult(node_id=node_id, semantic_role="caption", html_tag="span", source="rules",
                                        confidence=0.88)

            # Взвешенный скоринг: regex + видимость + размер + вес шрифта + имя узла.
            weights = stat_cfg.get("weights", {})
            regex_weight = float(weights.get("regex", 0.35))
            visibility_weight = float(weights.get("visibility", 0.20))
            typography_weight = float(weights.get("typography", 0.20))
            font_weight_score = float(weights.get("font_weight", 0.10))
            name_weight = float(weights.get("name_hint", 0.15))
            score = regex_weight
            min_visible_alpha = float(stat_cfg.get("min_visible_alpha", 0.65))
            if effective_alpha >= min_visible_alpha:
                score += visibility_weight

            min_h1_ratio = float(stat_cfg.get("min_h1_size_ratio", 0.28))
            if h1_size > 0 and (size / h1_size) >= min_h1_ratio:
                score += typography_weight

            min_font_weight = int(stat_cfg.get("min_font_weight", 500))
            if weight >= min_font_weight:
                score += font_weight_score

            metric_keywords = stat_cfg.get("name_keywords", [])
            if metric_keywords and keyword_match(name, metric_keywords):
                score += name_weight

            threshold = float(stat_cfg.get("score_threshold", 0.60))
            if score >= threshold:
                return ResolutionResult(node_id=node_id, semantic_role="stat", html_tag="span", source="rules",
                                        confidence=min(score, 1.0))

            return ResolutionResult(node_id=node_id, semantic_role="body", html_tag="span", source="rules",
                                    confidence=0.70)

        # Шаг 4. Типографика: заголовок или подпись по отношению размера шрифта к h1_size.
        typography_cfg = ANALYSIS_RULES.get("typography_detection", {})
        h1_ratio = float(typography_cfg.get("h1_ratio_threshold", 0.85))
        h2_ratio = float(typography_cfg.get("h2_ratio_threshold", 0.65))
        h3_ratio = float(typography_cfg.get("h3_ratio_threshold", 0.45))
        caption_max = float(typography_cfg.get("caption_max_size", 14.0))
        if h1_size > 0:
            ratio = size / h1_size
            if ratio >= h1_ratio:
                return ResolutionResult(node_id=node_id, semantic_role="heading_h1", html_tag="h1", source="rules",
                                        confidence=0.92)

            if ratio >= h2_ratio:
                return ResolutionResult(node_id=node_id, semantic_role="heading_h2", html_tag="h2", source="rules",
                                        confidence=0.90)

            if ratio >= h3_ratio:
                return ResolutionResult(node_id=node_id, semantic_role="heading_h3", html_tag="h3", source="rules",
                                        confidence=0.85)

        if size <= caption_max:
            return ResolutionResult(node_id=node_id, semantic_role="caption", html_tag="span", source="rules",
                                    confidence=0.85)

        # Шаг 5. Ключевые слова: имя узла или текст похожи на кнопку либо заголовок.
        keyword_cfg = SEMANTIC_RULES.get("button_keyword_matching", {})
        max_name_words = int(keyword_cfg.get("max_words_in_name", 4))
        max_text_words = int(keyword_cfg.get("max_words_in_text", 4))
        is_short_phrase = len(name.split()) <= max_name_words or len(txt.split()) <= max_text_words
        btn_keywords = SEMANTIC_RULES.get("button_keywords", [])
        if is_short_phrase and (keyword_match(name, btn_keywords) or txt.lower() in btn_keywords):
            return ResolutionResult(node_id=node_id, semantic_role="button_label", html_tag="span", source="rules",
                                    confidence=0.70)

        heading_keywords = SEMANTIC_RULES.get("heading_keywords", [])
        if keyword_match(name, heading_keywords):
            ratio = size / h1_size if h1_size else 0.0
            if ratio >= h2_ratio:
                return ResolutionResult(node_id=node_id, semantic_role="heading_h1", html_tag="h1", source="rules",
                                        confidence=0.65)

            return ResolutionResult(node_id=node_id, semantic_role="heading_h2", html_tag="h2", source="rules",
                                    confidence=0.60)

        # Шаг 6. Ни одно правило не сработало: узел уходит в LLM.
        return None

    @staticmethod
    def _is_rate_limit_error(err_str: str) -> bool:
        """Проверяет, что ошибка — превышение лимита запросов (HTTP 429).

        Это проблема ключа: нужно сменить ключ, а не модель.

        Args:
            err_str: Текст исключения или сообщения API.

        Returns:
            bool: `True`, если текст похож на rate limit или исчерпанную квоту.
        """
        markers = ["429", "rate limit", "quota", "rate_limit_exceeded", "too many requests"]
        low = err_str.lower()
        return any(m in low for m in markers)

    @staticmethod
    def _is_model_error(err_str: str) -> bool:
        """Проверяет, что ошибка означает недоступность или неподдержку модели (404 и т.п.).

        Это проблема модели: нужно перейти к следующей модели.

        Args:
            err_str: Текст исключения или сообщения API.

        Returns:
            bool: `True`, если текст похож на неизвестную, недоступную модель
            или неподдерживаемый `response_format`.
        """
        markers = ["model unavailable", "404", "does not exist", "invalid model", "not found", "unavailable for free",
                   "response_format", "json_object is not supported"]
        low = err_str.lower()
        return any(m in low for m in markers)

    @staticmethod
    def _is_length_limit_error(err_str: str) -> bool:
        """Проверяет, что модель оборвала ответ по лимиту длины.

        Это проблема модели: нужно перейти к следующей модели.

        Args:
            err_str: Текст исключения или сообщения API.

        Returns:
            bool: `True`, если текст указывает на лимит длины ответа.
        """
        markers = ["length limit", "max_tokens", "completion_tokens", "finish_reason: length", "length_limit"]
        low = err_str.lower()
        return any(m in low for m in markers)

    @staticmethod
    def _is_parse_or_validation_error(err_str: str) -> bool:
        """Проверяет, что ответ модели не удалось разобрать или провалидировать.

        Это проблема модели: нужно перейти к следующей модели.

        Args:
            err_str: Текст исключения или сообщения API.

        Returns:
            bool: `True`, если текст указывает на ошибку парсинга JSON или валидации.
        """
        markers = ["validation error", "invalid json", "jsondecodeerror", "could not parse", "expected value",
                   "expecting value"]
        low = err_str.lower()
        return any(m in low for m in markers)

    @classmethod
    async def resolve_ambiguities_with_llm(cls, ambiguous_nodes: dict, llm_cfg: dict, semaphore: asyncio.Semaphore) -> \
            dict[str, ResolutionResult]:
        """Разрешает неоднозначные TEXT-узлы через LLM.

        Политика отказоустойчивости:
          * `enabled=false` — LLM не вызывается, для всех узлов используется fallback.
          * `enabled=true`, `required=false` — LLM необязателен: при любой
            невозможности его использовать возвращается fallback, pipeline продолжается.
          * `enabled=true`, `required=true` — LLM обязателен: окончательный
            отказ приводит к `RuntimeError`.

        Порядок перебора: для каждой модели по очереди пробуются все ключи
        (ошибки ключа — например, 429 — ведут к следующему ключу, ошибки модели —
        к следующей модели); если исчерпано всё, применяется fallback либо
        поднимается `RuntimeError`.

        Неизвестные ошибки (не лимит, не ошибка модели) не привязаны ни к ключу,
        ни к модели: после первой пробуется следующий ключ, после второй на той же модели — следующая модель.

        Args:
            ambiguous_nodes: Узлы, которые не классифицировали правила:
                `{node_id: метаданные узла}` (`text`, `font_size`, `font_weight`, `name`).
            llm_cfg: Раздел `llm` манифеста (`enabled`, `required`, `provider`,
                `models`/`model`, `api_key_env` и параметры клиента).
            semaphore: Ограничитель числа одновременных запросов к LLM.

        Returns:
            dict[str, ResolutionResult]: Решение для каждого узла из `ambiguous_nodes`.
            Для узлов, по которым модель не ответила, подставляется fallback.

        Raises:
            RuntimeError: Если `required=true`, а ключей или моделей нет либо
                все модели и ключи исчерпаны или недоступны.
        """
        if not ambiguous_nodes:
            return {}

        llm_enabled = llm_cfg.get("enabled", True) is not False
        llm_required = bool(llm_cfg.get("required", False))
        provider_label = str(llm_cfg.get("provider", "openai_compatible"))

        if not llm_enabled:
            logger.info(
                f"🧠 [{provider_label.upper()}] LLM отключён конфигурацией. Используем deterministic fallback для {len(ambiguous_nodes)} нод.")
            return {nid: cls.make_fallback(nid) for nid in ambiguous_nodes}

        # Формирование запроса и весь перебор моделей и ключей выполняются под
        # semaphore: он ограничивает параллелизм запросов к LLM по конфигу.
        async with semaphore:
            max_chars = int(PIPELINE_SETTINGS.get("llm", {}).get("max_text_chars_sent", 80))
            payload = {}
            for nid, data in ambiguous_nodes.items():
                payload[nid] = {"text": str(data.get("text", ""))[:max_chars], "font_size": data.get("font_size"),
                                "font_weight": data.get("font_weight"), "node_name": data.get("name")}

            prompt = (
                "Classify text nodes for a landing page.\n"
                "Return ONLY a JSON object with EXACTLY this "
                "structure, no markdown fences, no extra keys:\n"
                f"{SEMANTIC_MAP_SCHEMA_HINT}\n\n"
                f"Nodes to classify:\n"
                f"{json.dumps(payload, ensure_ascii=False)}"
            )

            # Отсутствие ключей — не ошибка само по себе: при required=false
            # просто используется fallback.
            keys = LLMFactory.get_keys(llm_cfg)
            if not keys:
                message = f"LLM provider '{provider_label}': API-ключи не найдены."
                if llm_required:
                    logger.error(f"❌ {message} Но llm.required=true.")
                    raise RuntimeError(f"LLM required=true, но API key отсутствует для провайдера '{provider_label}'.")

                logger.warning(f"⚠️ {message} Используем deterministic fallback.")
                return {nid: cls.make_fallback(nid) for nid in ambiguous_nodes}

            models_to_try = llm_cfg.get("models") or [llm_cfg.get("model", "default")]
            models_to_try = [str(model).strip() for model in models_to_try if str(model).strip()]
            if not models_to_try:
                message = f"LLM provider '{provider_label}': список моделей пуст."
                if llm_required:
                    logger.error(f"❌ {message} Но llm.required=true.")
                    raise RuntimeError("LLM required=true, но список моделей пуст.")

                logger.warning(f"⚠️ {message} Используем deterministic fallback.")
                return {nid: cls.make_fallback(nid) for nid in ambiguous_nodes}

            logger.info(
                f"🧠 [{provider_label.upper()}] Доступно {len(keys)} API-ключей и {len(models_to_try)} моделей-кандидатов ({models_to_try}). required={llm_required}")

            for model_idx, model_name in enumerate(models_to_try, 1):
                logger.info(f"--- Пробуем модель [{model_idx}/{len(models_to_try)}]: '{model_name}' ---")
                llm_cfg_current = {**llm_cfg, "model": model_name}
                model_failed_completely = False
                unknown_error_count = 0
                for attempt, key in enumerate(keys, 1):
                    masked_key = mask_secret(key, prefix=9, suffix=4)
                    logger.info(
                        f"  [{attempt}/{len(keys)}] Запрос ({provider_label}, модель: {model_name}, ключ: {masked_key})...")
                    t0 = time.perf_counter()
                    try:
                        llm = LLMFactory.create(llm_cfg_current, api_key=key, pipeline_settings=PIPELINE_SETTINGS)
                        raw_response = await llm.ainvoke(prompt)

                        # Провайдеры (например, OpenRouter) могут молча подменить модель.
                        # Фактическая модель лежит в разных полях ответа в зависимости от
                        # провайдера, поэтому проверяем несколько мест. Только для лога.
                        if hasattr(raw_response, "model_dump"):
                            resp_dict = raw_response.model_dump()

                        elif hasattr(raw_response, "dict"):
                            resp_dict = raw_response.dict()

                        else:
                            resp_dict = {}

                        actual_model = (
                                resp_dict.get("model")
                                or resp_dict.get("response_metadata", {}).get("model")
                                or resp_dict.get("additional_kwargs", {}).get("model")
                                or resp_dict.get("additional_kwargs", {}).get("model_name")
                                or resp_dict.get("response_metadata", {}).get("model_name")
                                or resp_dict.get("response_metadata", {}).get("model_id")
                                or "unknown"
                        )
                        logger.info(f"  → Фактическая модель: {actual_model}")

                        content_str = str(raw_response.content or "")
                        res = clean_and_parse_json(content_str)
                        duration = time.perf_counter() - t0
                        logger.info(
                            f"  ✓ Ответ получен за {duration:.2f}с (модель {model_name}, ключ {masked_key}). Получено {len(res.decisions)} решений.")

                        results: dict[str, ResolutionResult] = {}
                        for decision in res.decisions:
                            # Модель могла выдумать или перепутать node_id — такие решения отбрасываются.
                            if decision.id not in ambiguous_nodes:
                                logger.warning(
                                    f"  ⚠️ LLM вернула неизвестный node_id '{decision.id}'. Решение игнорируется.")
                                continue

                            results[decision.id] = ResolutionResult(node_id=decision.id,
                                                                    semantic_role=decision.semantic_role,
                                                                    html_tag=decision.html_tag, source="llm",
                                                                    confidence=decision.confidence)

                        # Модель могла ответить не по всем узлам: fallback подставляется
                        # точечно, а её реальные решения по остальным узлам сохраняются.
                        for nid in ambiguous_nodes:
                            if nid not in results:
                                logger.warning(
                                    f"  ⚠️ LLM не вернула решение для node '{nid}'. Используем fallback для этой ноды.")
                                results[nid] = cls.make_fallback(nid)

                        return results

                    except Exception as attempt_err:
                        err_str = str(attempt_err)
                        duration = time.perf_counter() - t0
                        logger.warning(
                            f"  ⚠️ Ошибка на модели '{model_name}' / ключе {masked_key} за {duration:.2f}с: {err_str}")

                        # Тип ошибки определяет, что менять: ключ (лимит) или модель
                        # (недоступна, обрезала ответ, вернула невалидный JSON).
                        if cls._is_rate_limit_error(err_str):
                            logger.warning(
                                f"     -> 🛑 [429 RATE LIMIT] Лимит ключа {masked_key}. Пробуем следующий ключ...")
                            continue

                        elif cls._is_model_error(err_str):
                            logger.warning(
                                f"     -> ❌ [MODEL ERROR] Модель '{model_name}' недоступна. Переходим к следующей модели...")
                            model_failed_completely = True
                            break

                        elif cls._is_length_limit_error(err_str):
                            logger.warning(
                                f"     -> 📏 [LENGTH LIMIT] Модель '{model_name}' не смогла вернуть полный ответ. Переходим к следующей модели...")
                            model_failed_completely = True
                            break

                        elif cls._is_parse_or_validation_error(err_str):
                            logger.warning(
                                f"     -> 📄 [JSON PARSE ERROR] Модель '{model_name}' вернула невалидный ответ. Переходим к следующей модели...")
                            model_failed_completely = True
                            break

                        else:
                            unknown_error_count += 1
                            logger.warning(
                                f"     -> ❓ [НЕИЗВЕСТНАЯ ОШИБКА, #{unknown_error_count}] Пробуем следующий ключ...")
                            if unknown_error_count >= 2:
                                logger.warning(
                                    f"     -> Слишком много неизвестных ошибок на модели '{model_name}'. Переходим к следующей.")
                                model_failed_completely = True
                                break
                            continue

                # Цикл по ключам закончился без break: ни один ключ не дал ответа, но модель не признана негодной
                # (лимиты ключей или единичные неизвестные ошибки).
                if not model_failed_completely:
                    logger.error(f"❌ Модель '{model_name}': все {len(keys)} ключей исчерпали лимиты.")

            message = f"Все {len(models_to_try)} моделей и {len(keys)} API-ключей исчерпаны/недоступны."
            if llm_required:
                logger.error(f"❌ {message} Но llm.required=true.")
                raise RuntimeError("LLM required=true, но все модели и API-ключи исчерпаны или недоступны.")

            logger.warning(f"⚠️ {message} Используем deterministic fallback.")
            return {nid: cls.make_fallback(nid) for nid in ambiguous_nodes}


# ---------------------------------------------------------------------------
# Компонентный резолвер: кнопки, бейджи, карточки
# ---------------------------------------------------------------------------
class ComponentResolver:
    """Определяет `component_role` (button/badge/card) и `html_tag` узлов IR-дерева.

    Настоящие Figma-компоненты и инстансы (`INSTANCE`/`COMPONENT`) проверяются
    в первую очередь. Остальные узлы (`FRAME` и др.) классифицируются
    структурным fallback'ом по форме и содержимому (`ComponentHeuristics`).
    Содержимое уже найденной кнопки повторно не классифицируется, иначе,
    например, иконка внутри кнопки могла бы получить роль `card` или `badge`.
    """

    @classmethod
    def resolve_components(cls, root: IRNode) -> None:
        """Проставляет `component_role` и `html_tag` во всём дереве от `root`.

        Изменяет узлы на месте.
        Порядок обхода смешанный: настоящие компоненты (INSTANCE/COMPONENT)
        классифицируются до обхода детей, чтобы дети знали, что лежат внутри кнопки;
        структурный fallback выполняется после детей, чтобы has_button_descendant видел
        уже проставленные роли.

        Args:
            root: Корневой узел обходимого поддерева.
        """

        def has_button_descendant(node: IRNode) -> bool:
            """Проверяет, есть ли среди потомков `node` кнопка.

            Args:
                node: Корень проверяемого поддерева.

            Returns:
                bool: `True`, если найден потомок с `component_role == "button"`.
            """
            return any(child.component_role == "button" or has_button_descendant(child) for child in node.children)

        def walk(node: IRNode, inside_button: bool = False) -> None:
            """Классифицирует `node` и его детей.

            Args:
                node: Текущий узел.
                inside_button: `True`, если узел лежит внутри найденной кнопки;
                    тогда классификация потомков не выполняется.
            """
            current_is_button = False

            # Настоящий Figma-компонент проверяется первым; бейдж — раньше кнопки.
            if not inside_button and node.type in {"INSTANCE", "COMPONENT"} and node.component_name:
                text_children = [child for child in node.children if child.type == "TEXT"]
                if ComponentHeuristics.looks_like_badge(node, text_children):
                    node.component_role = "badge"
                    node.html_tag = "span"

                elif ComponentHeuristics.looks_like_button(node, text_children):
                    node.component_role = "button"
                    node.html_tag = "button"
                    current_is_button = True

            for child in node.children:
                walk(child, inside_button=(inside_button or current_is_button))

            if inside_button or current_is_button:
                return

            # Структурный fallback: узел не настоящий компонент, судим по форме.
            if node.type not in {"FRAME", "COMPONENT", "INSTANCE"}:
                return

            text_children = [child for child in node.children if child.type == "TEXT"]
            if ComponentHeuristics.looks_like_badge(node, text_children):
                node.component_role = "badge"
                node.html_tag = "span"
                return

            if not has_button_descendant(node) and ComponentHeuristics.looks_like_button(node, text_children):
                node.component_role = "button"
                node.html_tag = "button"
                return

            if ComponentHeuristics.looks_like_card(node):
                node.component_role = "card"
                node.html_tag = "div"
                return

        walk(root)


# ---------------------------------------------------------------------------
# Переопределение токенов по dot-пути
# ---------------------------------------------------------------------------
def set_by_dot_path(target_dict: dict, path: str, value: Any) -> None:
    """Записывает `value` в `target_dict` по dot-пути, создавая промежуточные словари.

    Понимает алиас `typography.scale.X` -> `typography.X`. Для пути
    `colors.<имя>` сохраняет структуру токена (`value`, `source`, `confidence`),
    а не затирает её плоским значением: источник помечается как `override`.
    Если токена `colors.<имя>` в `target_dict` нет, значение записывается как
    есть, без структуры токена. Изменяет `target_dict` на месте.
    Если токена colors.<имя> в target_dict нет, значение записывается как есть, без структуры токена.

    Args:
        target_dict: Словарь токенов (`colors`, `typography`, ...).
        path: Путь через точки, например `typography.button.size` или `colors.primary`.
        value: Записываемое значение.
    """
    norm_path = path.replace("typography.scale.", "typography.")
    parts = norm_path.split(".")

    # Прямая запись цвета: colors.primary = "#xxx".
    if parts[0] == "colors" and len(parts) == 2:
        color_name = parts[1]
        if color_name in target_dict.get("colors", {}):
            if isinstance(target_dict["colors"][color_name], dict) and "value" in target_dict["colors"][color_name]:
                target_dict["colors"][color_name]["value"] = value
                target_dict["colors"][color_name]["source"] = "override"
                return
            else:
                target_dict["colors"][color_name] = {"value": value, "source": "override", "confidence": 1.0}
                return

    curr = target_dict
    for k in parts[:-1]:
        if k not in curr or not isinstance(curr[k], dict):
            curr[k] = {}
        curr = curr[k]
    curr[parts[-1]] = value


# ---------------------------------------------------------------------------
# Перенос стиля дизайн-системы на секции
# ---------------------------------------------------------------------------
class StyleTransferEngine:
    """Переносит визуальный язык дизайн-системы на IR-дерево секции.

    Переносятся цвета, типографика и стиль компонентов; декоративные элементы
    и растровые ассеты перекрашиваются (retint) под акцентный цвет дизайн-системы.
    Результат — `ResolvedSectionSpec`, готовый для `WebRenderer` и `ReferenceRenderer`.
    Точка входа — `StyleTransferEngine.transfer`.
    """

    # Режимы наложения, которыми дизайнеры тонируют текстуру акцентным цветом
    # (паттерн «прямоугольник с HUE поверх image-fill»). Узел с таким режимом —
    # источник цвета для составного ассета.
    RECOLORABLE_BLEND_MODES = frozenset({"HUE", "COLOR", "SATURATION", "MULTIPLY"})

    @staticmethod
    def _find_accent_source_color(node: IRNode) -> str | None:
        """Ищет цвет, формирующий видимый акцент композиции в поддереве узла.

        Три шага, от точного к приблизительному; каждый следующий выполняется,
        только если предыдущий ничего не нашёл:
          1. Самый большой по площади узел с непустой заливкой и тонирующим
             режимом наложения (`RECOLORABLE_BLEND_MODES`): паттерн
             «текстура + тонирующий слой».
          2. Собственный `bg_color` узла (одноцветная иконка или точка без вложенности).
          3. Пиксельный сэмплинг файла ассета. Если в IR нет цвета в виде
             RGB-токена (image_fill с фильтрами, градиент и т.п.), структурный
             поиск бессилен, а файл ассета — именно то, что попадёт в браузер.

        Шаги не привязаны к id, секции или файлу: шаг 3 включается по признаку
        «нет `bg_color`, но есть собственный растровый файл».

        Args:
            node: Корень поддерева, в котором ищется акцентный цвет.

        Returns:
            str | None Цвет в формате `rgba(...)` или hex; `None`, если
            ни один из трёх шагов цвет не нашёл.
        """
        candidates: list[tuple[float, str]] = []

        def visit(n: IRNode) -> None:
            """Собирает в `candidates` пары (площадь, bg_color) узлов с тонирующим режимом наложения.
            Args:
                n: Текущий узел; обход рекурсивный.
            """
            if n.style.bg_color and n.style.bg_color != "transparent" and n.style.blend_mode in StyleTransferEngine.RECOLORABLE_BLEND_MODES:
                area = max(n.rel_geometry.width, 0.0) * max(n.rel_geometry.height, 0.0)
                candidates.append((area, n.style.bg_color))
            for ch in n.children:
                visit(ch)

        visit(node)
        if candidates:
            candidates.sort(key=lambda pair: pair[0], reverse=True)
            return candidates[0][1]

        if node.style.bg_color and node.style.bg_color != "transparent":
            return node.style.bg_color

        if node.asset_path:
            asset_file = ASSETS_DIR / Path(node.asset_path).name
            if asset_file.exists():
                acc_cfg = ANALYSIS_RULES.get("accent_color_detection", {})
                sampled = sample_accent_color_from_asset(
                    asset_file,
                    k=int(acc_cfg.get("sample_top_k", 8)),
                    resize_dim=int(acc_cfg.get("resize_dim", 48)),
                    min_alpha=float(acc_cfg.get("min_alpha", 0.5)),
                    min_saturation=float(acc_cfg.get("min_saturation", 0.15)),
                )
                if sampled is not None:
                    logger.info(
                        f"  [retint] Акцент для '{node.name}' ({node.id}) не найден через bg_color/blend_mode (image_fill/gradient-only контент) — определён пиксельным сэмплингом ассета {asset_file.name}: {sampled}"
                    )
                return sampled

        return None

    @staticmethod
    def _apply_retint_filter(node: IRNode, target_primary: str | None) -> None:
        """Вычисляет CSS-фильтр перекраски узла и записывает его в `node.style`.

        Записываются `retint_hue_deg`, `retint_saturate`, `retint_brightness`
        и `retinted=True`. Если акцентный цвет не найден или близок к серому
        (см. `compute_retint_filter`), узел не изменяется.

        Args:
            node: Узел с ассетом или raster_composite, изменяется на месте.
            target_primary: Целевой акцентный цвет дизайн-системы (`rgba(...)`
                или hex); `None`, если токен не определён.
        """
        source_color = StyleTransferEngine._find_accent_source_color(node)
        filt = compute_retint_filter(source_color, target_primary)
        if filt is not None:
            node.style.retint_hue_deg = filt["hue_deg"]
            node.style.retint_saturate = filt["saturate"]
            node.style.retint_brightness = filt["brightness"]
            node.style.retinted = True

    @staticmethod
    def _retint_effect_colors(node: IRNode, target_primary: str | None) -> None:
        """Перекрашивает цвет теней (DROP_SHADOW, INNER_SHADOW) узла под целевой акцент.
        Работает для любого типа узла (включая TEXT) независимо от его заливки.
        Цвет эффекта хранится отдельно от `bg_color` (`node.style.extras["effects"]`),
        поэтому обычный перенос цветов его не затрагивает. Новый цвет пишется
        прямо в `effect["color"]` (компоненты 0..1, как в Figma), а не через
        CSS-фильтр: `effects_to_css` остаётся без изменений и не может дважды
        применить `hue-rotate`. Изменяет `node.style.extras` на месте.
        INNER_SHADOW перекрашивается, но в CSS не выводится (см. effects_to_css).
        Args:
            node: Узел, чьи эффекты проверяются.
            target_primary: Целевой акцентный цвет дизайн-системы (`rgba(...)`
                или hex); `None`, если токен не определён.
        """
        effects = node.style.extras.get("effects") if isinstance(node.style.extras, dict) else None
        if not effects:
            return

        for effect in effects:
            if not isinstance(effect, dict) or not effect.get("visible", True):
                continue
            if effect.get("type") not in ("DROP_SHADOW", "INNER_SHADOW"):
                continue

            color = effect.get("color") or {}
            try:
                r = round(float(color.get("r", 0.0)) * 255)
                g = round(float(color.get("g", 0.0)) * 255)
                b = round(float(color.get("b", 0.0)) * 255)
                a = float(color.get("a", 1.0))
            except (TypeError, ValueError):
                continue

            source_rgba = f"rgba({r}, {g}, {b}, {a})"
            filt = compute_retint_filter(source_rgba, target_primary)
            if filt is None:
                continue  # серый или прозрачный акцент не сдвигаем

            retinted_rgba = apply_retint_to_rgba(source_rgba, filt["hue_deg"], filt["saturate"], filt["brightness"])
            channels = parse_rgba_channels(retinted_rgba)
            if channels is None:
                continue
            new_r, new_g, new_b, new_a = channels
            effect["color"] = {"r": new_r, "g": new_g, "b": new_b, "a": new_a}

    @staticmethod
    def apply_retint_to_native_decoration(node: IRNode, target_primary: str | None) -> None:
        """Перекрашивает декоративную native-фигуру под целевой акцентный цвет.

        Декоративные фигуры (кружки-подложки, точки-акценты) — не TEXT и не
        ASSET/raster_composite, поэтому другие шаги `transfer` их `bg_color`
        не переписывают. Признак декоративной фигуры: нет `component_role`,
        есть собственная заливка, нет TEXT-детей и вложенных групп (допустимы
        только листовые дети). Широкие тёмные фоновые
        панели отсекаются порогом `min_saturation` внутри `compute_retint_filter`,
        отдельной проверки размера или имени узла не нужно. Расчёт фильтра — тот же,
        что для ассетов. Изменяет `node.style.bg_color` на месте.

        Args:
            node: Проверяемый узел; перекрашивается, если подходит под признаки.
            target_primary: Целевой акцентный цвет дизайн-системы (`rgba(...)`
                или hex); `None`, если токен не определён.
        """
        if node.component_role is not None:
            return
        if not node.style.bg_color or node.style.bg_color == "transparent":
            return
        if any(c.type == "TEXT" or c.children for c in node.children):
            return

        filt = compute_retint_filter(node.style.bg_color, target_primary)
        if filt is not None:
            node.style.bg_color = apply_retint_to_rgba(node.style.bg_color, filt["hue_deg"], filt["saturate"],
                                                       filt["brightness"])
            node.style.retinted = True

    @staticmethod
    def build_audit_entry(node: IRNode, kind: str, target_primary: str | None) -> dict[str, Any]:
        """Формирует запись аудита перекраски для `style_coherence` в `qa_report.json`.

        Args:
            node: Проверяемый узел.
            kind: Вид узла: `"asset"` или `"native_decoration"`.
            target_primary: Целевой акцентный цвет дизайн-системы (`rgba(...)`
                или hex); `None`, если токен не определён.

        Returns:
            dict[str, Any]: Запись с полями `node_id`, `node_name`, `kind`,
            `source_color`, `retinted` и `reason` (`applied`, `no_source_color_found`,
            `low_saturation_skip` или `not_attempted`). Для asset добавляются
            параметры фильтра, для native_decoration — `final_color`.
        """
        source_color = StyleTransferEngine._find_accent_source_color(node)
        entry: dict[str, Any] = {"node_id": node.id, "node_name": node.name, "kind": kind, "source_color": source_color,
                                 "retinted": node.style.retinted}
        if node.style.retinted:
            entry["reason"] = "applied"
            if kind == "asset":
                entry["retint_hue_deg"] = node.style.retint_hue_deg
                entry["retint_saturate"] = node.style.retint_saturate
                entry["retint_brightness"] = node.style.retint_brightness
            else:
                # Итоговый цвет native-декорации уже записан в bg_color. Поля
                # retint_hue_deg/... здесь не копируются: для native-декораций
                # они всегда None («не применимо»), а их наличие привело бы к
                # повторному сдвигу оттенка в `effects_to_css`.
                entry["final_color"] = node.style.bg_color
        elif source_color is None:
            entry["reason"] = "no_source_color_found"
        elif compute_retint_filter(source_color, target_primary) is None:
            entry["reason"] = "low_saturation_skip"
        else:
            entry["reason"] = "not_attempted"
        return entry

    @staticmethod
    def audit_retint_coverage(root: IRNode, target_primary: str | None) -> list[dict[str, Any]]:
        """Фиксирует покрытие перекраской видимых элементов `resolved_root` после `transfer`.

        «Видимая единица» определяется через `render_strategy` (asset или
        raster_composite с `asset_id`), а не через собственную заливку: у
        составных групп своей заливки почти нет, а CSS-фильтр `hue-rotate`
        ставится именно на asset-узел. Обход останавливается на таком узле:
        рендер в его детей тоже не заходит.

        Args:
            root: Корень дерева после переноса стиля.
            target_primary: Целевой акцентный цвет дизайн-системы (`rgba(...)`
                или hex); `None`, если токен не определён.

        Returns:
            list[dict[str, Any]]: Записи аудита (см. `build_audit_entry`)
            по asset-узлам и native-декорациям.
        """
        results: list[dict[str, Any]] = []

        def visit(node: IRNode) -> None:
            """Аудирует узел (asset или native-декорацию) и спускается в детей, кроме детей asset-узла.
            Args:
                node: Текущий узел.
            """
            is_asset_like = node.render_strategy in ("asset", "raster_composite") and node.asset_id
            if is_asset_like:
                results.append(StyleTransferEngine.build_audit_entry(node, "asset", target_primary))
                return

            has_own_fill = bool(node.style.bg_color and node.style.bg_color != "transparent")
            is_native_leaf = node.component_role is None and has_own_fill and not any(
                c.type == "TEXT" or c.children for c in node.children)
            if is_native_leaf:
                results.append(StyleTransferEngine.build_audit_entry(node, "native_decoration", target_primary))

            for ch in node.children:
                visit(ch)

        visit(root)
        return results

    @staticmethod
    def transfer(section: SectionSpec, ds: DesignSystemSpec) -> ResolvedSectionSpec:
        """Переносит стиль дизайн-системы на секцию и возвращает готовую к рендеру копию.
        Работает с глубокими копиями дерева: `resolved_root` (с перенесённым
        стилем) и `original_root` (без изменений, эталон для Extraction QA).
        Учитывает `token_overrides` и политику `preservation` секции. В режиме
        `style_transfer` применяет стиль компонентов, перекрашивает эффекты,
        native-декорации и ассеты, переносит типографику и цвет текста. В любом
        режиме подставляет `text_color` тексту, у которого его нет.
        Args:
            section: Секция композиции с IR-деревом и политикой переноса.
            ds: Дизайн-система: источник токенов цвета и типографики.
        Returns:
            ResolvedSectionSpec: Секция с `resolved_root` и `original_root`;
            `reference_image` переносится без изменений.
        """
        logger.info(f"Перенос стилей для секции '{section.name}' (режим: {section.transform.mode})...")
        resolved_root = section.root_node.model_copy(deep=True)
        original_root = section.root_node.model_copy(deep=True)
        policy = section.transform.preservation
        is_transfer = section.transform.mode == "style_transfer"
        raw_tokens = ds.tokens.model_dump()
        overrides = section.transform.token_overrides
        if overrides:
            logger.info(f"  Применение {len(overrides)} переопределений токенов: {list(overrides.keys())}")
            for path_key, val in overrides.items():
                set_by_dot_path(raw_tokens, path_key, val)
        effective_tokens = DesignTokens.model_validate(raw_tokens)
        c_tok = {k: v.value for k, v in effective_tokens.colors.items()}
        t_tok = effective_tokens.typography
        btn_padding = RENDER_RULES["button_padding"]
        card_border_w = RENDER_RULES["card_border_width"]
        component_style_rules = RENDER_RULES.get("component_style_transfer", {})
        button_fill_min_lightness_gap = component_style_rules.get("button_fill_min_lightness_gap", 0.25)
        button_border_min_alpha_gap = component_style_rules.get("button_border_min_alpha_gap", 0.2)
        card_glass_alpha_threshold = component_style_rules.get("card_glass_alpha_threshold", 0.15)
        badge_min_visible_alpha = component_style_rules.get("badge_min_visible_alpha", 0.55)
        accent_token_value, accent_token_key = resolve_reliable_accent_token(
            effective_tokens.colors,
            min_confidence=float(component_style_rules.get("min_reliable_accent_confidence", 0.6)),
            preferred_order=tuple(
                component_style_rules.get("accent_token_priority", ["primary", "accent", "secondary"])),
        )
        if accent_token_key != "primary":
            logger.info(
                f"  [accent] Для retint/CTA-заливки секции '{section.name}' используется токен '{accent_token_key}' вместо 'primary' (см. confidence в дизайн-системе).")

        role_color_map: dict[str, str] = RENDER_RULES.get("role_color_map", {})
        default_color_key: str = role_color_map.get("__default__", "text_secondary")
        typography_role_map: dict[str, str] = RENDER_RULES.get("typography_role_map", {})
        default_typo_key: str = typography_role_map.get("__default__", "body")

        # id текстовых узлов, которым уже назначен осознанный цвет, контрастный
        # к реальному финальному фону: общий `role_color_map` в `walk` их не трогает.
        styled_label_ids: set = set()

        def apply_contrasting_label_color(node: IRNode, label_bg: str | None) -> None:
            """Красит текстовых потомков `node` в цвет, контрастный к `label_bg`.

            Общий код кнопки и бейджа. Id перекрашенных узлов попадают в
            `styled_label_ids`, иначе `walk` затёр бы цвет ролевым.

            Args:
                node: Компонент (кнопка или бейдж), чьи подписи красятся.
                label_bg: Итоговый цвет фона подписи; `None`, если он неизвестен.
            """
            label_color = pick_contrasting_text_color(label_bg, c_tok)
            for child in node.children:
                if child.type == "TEXT":
                    child.style.text_color = label_color
                    styled_label_ids.add(child.id)

        def find_own_h1_size(n: IRNode) -> float | None:
            """Ищет `font_size` первого `heading_h1` в поддереве.
            Нужен, чтобы масштабировать типографику по собственному соотношению
            размеров секции-получателя, а не источника стиля.

            Args:
                n: Корень поддерева.

            Returns:
                float | None: Размер шрифта или None, если h1 нет.
            """
            if n.type == "TEXT" and n.semantic_role == "heading_h1" and n.style.font_size:
                return n.style.font_size
            for ch in n.children:
                found = find_own_h1_size(ch)
                if found:
                    return found
            return None

        own_h1_size: float | None = find_own_h1_size(resolved_root) if is_transfer else None
        source_h1_token = t_tok.get("h1")

        def apply_component_style(node: IRNode) -> None:
            """Применяет стиль дизайн-системы к компоненту (button, card, badge).
            Правила берутся из `component_style_transfer` в `render_rules.json`.
            Ничего не делает, если политика секции запрещает перенос стиля компонентов.
            Args:
                node: Проверяемый узел; изменяется на месте.
            """
            if not policy.transfer_component_style:
                return

            # border_radius токеном дизайн-системы не переопределяется ни в одной из
            # веток ниже: это геометрия секции-получателя, а не цвет.

            if node.component_role == "button":
                had_fill = bool(node.style.bg_color and node.style.bg_color != "transparent")
                had_border_only = not had_fill and node.style.border_width > 0 and node.style.border_color
                if had_border_only:
                    source_alpha = alpha_from_rgba(node.style.border_color)
                    token_border = str(c_tok.get("border", DEFAULTS["colors"]["border"]))
                    token_channels = parse_rgba_channels(token_border)
                    token_alpha = token_channels[3] if token_channels else 1.0
                    if token_channels is not None and source_alpha > token_alpha + button_border_min_alpha_gap:
                        r, g, b, _a = token_channels
                        node.style.border_color = f"rgba({round(r * 255)}, {round(g * 255)}, {round(b * 255)}, {source_alpha})"
                    else:
                        node.style.border_color = token_border

                    node.style.bg_color = None
                    node.style.border_width = card_border_w

                else:
                    token_primary = accent_token_value or DEFAULTS["colors"]["primary"]
                    source_lightness = rgba_lightness(node.style.bg_color)
                    token_lightness = rgba_lightness(token_primary)

                    # Если заливка кнопки источника заметно светлее акцента дизайн-системы, она остаётся светлой (retint
                    # с сохранением насыщенности, иначе самый светлый токен); в остальных случаях берётся сам акцентный цвет.
                    if source_lightness is not None and token_lightness is not None and source_lightness - token_lightness > button_fill_min_lightness_gap:
                        if not retint_bg_color_toward_token(node, token_primary, preserve_source_saturation=True):
                            node.style.bg_color = pick_lightest_token(c_tok, c_tok.get("canvas"))
                    else:
                        node.style.bg_color = token_primary

                    node.style.border_color = None
                    node.style.border_width = 0.0

                if not policy.preserve_geometry:
                    node.layout.padding_top = max(node.layout.padding_top, btn_padding["top"])
                    node.layout.padding_bottom = max(node.layout.padding_bottom, btn_padding["bottom"])
                    node.layout.padding_left = max(node.layout.padding_left, btn_padding["left"])
                    node.layout.padding_right = max(node.layout.padding_right, btn_padding["right"])

                label_bg = node.style.bg_color if had_fill else c_tok.get("canvas")
                apply_contrasting_label_color(node, label_bg)

            elif node.component_role == "card":
                if node.style.bg_color and node.style.bg_color != "transparent":
                    source_alpha = alpha_from_rgba(node.style.bg_color)
                    token_surface = str(c_tok.get("surface", DEFAULTS["colors"]["surface"]))
                    if source_alpha < card_glass_alpha_threshold:
                        retint_bg_color_toward_token(node, token_surface, preserve_source_saturation=False)
                    else:
                        node.style.bg_color = token_surface

                    node.style.border_color = str(c_tok.get("border", DEFAULTS["colors"]["border"]))
                    node.style.border_width = card_border_w

            elif node.component_role == "badge":
                if node.style.bg_color and node.style.bg_color != "transparent":
                    token_surface = str(c_tok.get("surface", DEFAULTS["colors"]["surface"]))
                    retint_bg_color_toward_token(node, token_surface, preserve_source_saturation=False)
                    current_alpha = alpha_from_rgba(node.style.bg_color)
                    if current_alpha < badge_min_visible_alpha:
                        channels = parse_rgba_channels(node.style.bg_color)
                        if channels is not None:
                            r, g, b, _a = channels
                            node.style.bg_color = f"rgba({round(r * 255)}, {round(g * 255)}, {round(b * 255)}, {badge_min_visible_alpha})"

                    node.style.border_color = str(c_tok.get("border", DEFAULTS["colors"]["border"]))
                    if node.style.border_width <= 0:
                        node.style.border_width = card_border_w

                label_bg = node.style.bg_color or c_tok.get("surface")
                apply_contrasting_label_color(node, label_bg)

        def walk(node: IRNode):
            """Применяет перенос стиля ко всему поддереву `node`.
            В режиме `style_transfer` переносятся стиль компонентов, эффекты,
            декорации, ассеты, типографика и цвет текста. Тексту без цвета
            `text_color` подставляется в любом режиме.

            Args:
                node: Корень обходимого поддерева; изменяется на месте.
            """
            if is_transfer:
                apply_component_style(node)
                StyleTransferEngine._retint_effect_colors(node, accent_token_value)
                if node.type != "TEXT" and node.render_strategy == "native":
                    StyleTransferEngine.apply_retint_to_native_decoration(node, accent_token_value)

                if node.type == "TEXT":
                    if policy.transfer_typography:
                        text_role = node.semantic_role or "body"
                        typo_key = typography_role_map.get(text_role, default_typo_key)
                        mapped_token = t_tok.get(typo_key) or t_tok.get("body")
                        if mapped_token:
                            own_size = node.style.font_size
                            node.style.font_family = mapped_token.family
                            node.style.font_weight = mapped_token.weight
                            node.style.line_height_percent = mapped_token.line_height_percent
                            node.style.letter_spacing = mapped_token.letter_spacing
                            # Размер масштабируется по собственному соотношению к h1 секции,
                            # чтобы сохранить её иерархию заголовков, а не иерархию источника стиля.
                            if own_h1_size and own_h1_size > 0 and own_size and source_h1_token and source_h1_token.size:
                                own_ratio = own_size / own_h1_size
                                node.style.font_size = own_ratio * source_h1_token.size
                            else:
                                node.style.font_size = mapped_token.size

                    if policy.transfer_colors and node.id not in styled_label_ids:
                        text_role = node.semantic_role or "body"
                        color_key = role_color_map.get(text_role, default_color_key)
                        node.style.text_color = str(
                            c_tok.get(color_key, c_tok.get(default_color_key, DEFAULTS["colors"]["text_primary"])))

                    if not policy.preserve_content and node.characters:
                        node.characters = node.characters.strip()

                # Дети asset/raster_composite-узла рендеру не видны (внутри готовая
                # картинка), поэтому обход в них не заходит.
                if node.render_strategy in ("raster_composite", "asset") and node.asset_id:
                    if policy.retint_assets:
                        StyleTransferEngine._apply_retint_filter(node, accent_token_value)
                    return

            if node.type == "TEXT" and not node.style.text_color:
                text_role = node.semantic_role or "body"
                color_key = role_color_map.get(text_role, default_color_key)
                node.style.text_color = str(
                    c_tok.get(color_key, c_tok.get(default_color_key, DEFAULTS["colors"]["text_primary"])))

            for ch in node.children:
                walk(ch)

        walk(resolved_root)
        logger.info(f"✓ Стили для '{section.name}' успешно перенесены.")
        return ResolvedSectionSpec(
            id=section.id,
            name=section.name,
            source=section.source,
            geometry=section.geometry,
            resolved_root=resolved_root,
            original_root=original_root,
            assets=section.assets,
            reference_image=section.reference_image,
            responsive_capable=section.responsive_capable,
        )


# ---------------------------------------------------------------------------
# Разделение секции на декоративный и контентный слои
# ---------------------------------------------------------------------------
class ResponsiveLayerClassifier:
    """Классифицирует блоки секции как декоративные или контентные.

    Тип Figma-ноды сам по себе семантику не определяет. Решение строится из
    совокупности признаков: семантическая и компонентная роль, потомки,
    auto-layout, режимы размера, прозрачность, эффекты, имя узла, доля
    покрытия секции.

    Контейнеры, не признанные декоративными, отрисовываются как контент либо
    раскладываются до листьев. Границей контента признаётся широкий набор
    признаков (is_content_boundary), поэтому спорные блоки чаще попадают в
    контент. Лист без детей и без признаков контента считается декоративным (см. classify).
    """

    # Семантические роли текста, которые считаются контентом.
    CONTENT_ROLES = frozenset({"heading_h1", "heading_h2", "heading_h3", "body", "button_label", "stat"})
    # Роли компонентов, которые считаются контентом (интерактивные блоки и карточки).
    CONTENT_COMPONENT_ROLES = frozenset({"button", "card"})

    @staticmethod
    def effective_alpha(node: IRNode) -> float:
        """Возвращает эффективную непрозрачность узла (0..1).

        Приоритет: цвет текста (для TEXT), затем `bg_color`, затем `style.opacity`.

        Args:
            node: Проверяемый узел.

        Returns:
            float: Значение непрозрачности от 0 до 1.
        """
        if node.type == "TEXT" and node.style.text_color:
            return alpha_from_rgba(node.style.text_color)

        if node.style.bg_color and node.style.bg_color != "transparent":
            return alpha_from_rgba(node.style.bg_color)

        return max(0.0, min(float(node.style.opacity), 1.0))

    @classmethod
    def is_meaningful_caption(cls, node: IRNode, cfg: dict) -> bool:
        """Проверяет, что узел — читаемая подпись, а не полупрозрачный декоративный текст.

        Args:
            node: Проверяемый узел.
            cfg: Раздел `responsive_detection` из `analysis_rules.json`;
                используется `content_caption_min_alpha`.

        Returns:
            bool: `True` для непустого TEXT-узла с ролью `caption` и
            непрозрачностью не ниже `content_caption_min_alpha`.
        """
        if node.type != "TEXT":
            return False

        if node.semantic_role != "caption":
            return False

        text = (node.characters or "").strip()
        if not text:
            return False

        min_alpha = float(cfg.get("content_caption_min_alpha", 0.80))
        return cls.effective_alpha(node) >= min_alpha

    @classmethod
    def contains_semantic_content(cls, node: IRNode, cfg: dict) -> bool:
        """Проверяет, что узел или любой его потомок несёт значимый контент.

        Контентом считаются: роль из `CONTENT_ROLES`, читаемая подпись,
        интерактивный компонент или карточка (`CONTENT_COMPONENT_ROLES`).

        Args:
            node: Корень проверяемого поддерева.
            cfg: Раздел `responsive_detection` из `analysis_rules.json`.

        Returns:
            bool: `True`, если в поддереве есть значимый контент.
        """
        if node.type == "TEXT" and node.semantic_role in cls.CONTENT_ROLES:
            return True

        if cls.is_meaningful_caption(node, cfg):
            return True

        if node.component_role in cls.CONTENT_COMPONENT_ROLES:
            return True

        return any(cls.contains_semantic_content(child, cfg) for child in node.children)

    @staticmethod
    def has_visible_text(node: IRNode) -> bool:
        """Проверяет, что узел или любой его потомок — TEXT-узел с непустым содержимым.

        Args:
            node: Корень проверяемого поддерева.

        Returns:
            bool: `True`, если в поддереве есть непустой текст.
        """
        if node.type == "TEXT" and bool((node.characters or "").strip()):
            return True

        return any(ResponsiveLayerClassifier.has_visible_text(child) for child in node.children)

    @staticmethod
    def has_interactive_component(node: IRNode) -> bool:
        """Проверяет, что узел или любой его потомок — кнопка.

        Args:
            node: Корень проверяемого поддерева.

        Returns:
            bool: `True`, если в поддереве есть узел с `component_role == "button"`.
        """
        if node.component_role == "button":
            return True

        return any(ResponsiveLayerClassifier.has_interactive_component(child) for child in node.children)

    @staticmethod
    def has_blur_effect(node: IRNode) -> bool:
        """Проверяет, что у узла есть видимый эффект LAYER_BLUR или BACKGROUND_BLUR.

        Args:
            node: Проверяемый узел.

        Returns:
            bool: `True`, если такой эффект есть.
        """
        effects = node.style.extras.get("effects", []) if isinstance(node.style.extras, dict) else []
        return any(effect.get("visible", True) and effect.get("type") in {"LAYER_BLUR", "BACKGROUND_BLUR"} for effect in
                   effects)

    @staticmethod
    def geometry_coverage(node: IRNode, root: IRNode) -> float:
        """Возвращает долю площади `root`, занимаемую `node`.

        Args:
            node: Проверяемый узел.
            root: Корневой узел секции.

        Returns:
            float: Значение от 0 до 1; 0, если площадь `root` нулевая.
        """
        root_area = max(root.rel_geometry.width, 0.0) * max(root.rel_geometry.height, 0.0)
        if root_area <= 0:
            return 0.0

        node_area = max(node.rel_geometry.width, 0.0) * max(node.rel_geometry.height, 0.0)
        return min(node_area / root_area, 1.0)

    @classmethod
    def decoration_score(cls, node: IRNode, root: IRNode, cfg: dict) -> float:
        """Считает скор декоративности узла.
        Это сумма весов сработавших признаков, ограниченная диапазоном 0..1,
        а не вероятность модели. Реальный контент и интерактивные компоненты
        получают 0 сразу. Признаки: имя узла из `decorative_name_keywords`,
        низкая непрозрачность, blur-эффект, большое покрытие секции без
        текста и flex, визуальный лист без детей.
        Args:
            node: Оцениваемый узел.
            root: Корневой узел секции (для расчёта покрытия).
            cfg: Раздел `responsive_detection` из `analysis_rules.json`
                (веса, пороги, ключевые слова).
        Returns:
            float: Скор от 0 до 1.
        """
        if cls.contains_semantic_content(node, cfg):
            return 0.0

        if cls.has_interactive_component(node):
            return 0.0

        score = 0.0
        weights = cfg.get("weights", {})
        name_weight = float(weights.get("name_keyword", 0.35))
        opacity_weight = float(weights.get("low_opacity", 0.25))
        effects_weight = float(weights.get("effects", 0.15))
        coverage_weight = float(weights.get("large_coverage", 0.15))
        leaf_weight = float(weights.get("visual_leaf", 0.10))

        name_keywords = cfg.get("decorative_name_keywords", [])
        if name_keywords and keyword_match((node.name or "").lower(), name_keywords):
            score += name_weight

        alpha = cls.effective_alpha(node)
        max_decorative_alpha = float(cfg.get("decorative_max_opacity", 0.5))
        if alpha < max_decorative_alpha:
            score += opacity_weight

        if cls.has_blur_effect(node):
            score += effects_weight

        coverage = cls.geometry_coverage(node, root)
        min_background_coverage = float(cfg.get("background_min_coverage_ratio", 0.45))
        if coverage >= min_background_coverage and not cls.has_visible_text(node) and not node.layout.is_flex:
            score += coverage_weight

        visual_leaf_types = set(cfg.get("visual_leaf_types", ["VECTOR", "ELLIPSE", "LINE", "STAR", "REGULAR_POLYGON"]))
        if node.type in visual_leaf_types and not node.children:
            score += leaf_weight

        return max(0.0, min(score, 1.0))

    @classmethod
    def is_decorative(cls, node: IRNode, root: IRNode, cfg: dict) -> bool:
        """Проверяет, что скор декоративности узла достиг порога `decorative_score_threshold`.

        Args:
            node: Оцениваемый узел.
            root: Корневой узел секции.
            cfg: Раздел `responsive_detection` из `analysis_rules.json`.

        Returns:
            bool: `True`, если узел декоративный.
        """
        threshold = float(cfg.get("decorative_score_threshold", 0.55))
        return cls.decoration_score(node, root, cfg) >= threshold

    @classmethod
    def is_content_boundary(cls, node: IRNode, cfg: dict) -> bool:
        """Проверяет, что структуру блока нельзя разрушать раскладыванием детей на верхний уровень.

        Блок считается границей контента, если он: компонент из
        `CONTENT_COMPONENT_ROLES`, flex-контейнер, имеет режим размера
        FILL/HUG (`content_sizing_modes`), является текстом или содержит
        значимый контент.

        Args:
            node: Проверяемый узел.
            cfg: Раздел `responsive_detection` из `analysis_rules.json`.

        Returns:
            bool: `True`, если узел нужно отрисовывать целиком.
        """
        if node.component_role in cls.CONTENT_COMPONENT_ROLES:
            return True

        if node.layout.is_flex:
            return True

        content_sizing = set(cfg.get("content_sizing_modes", ["FILL", "HUG"]))
        if node.layout.sizing_horizontal in content_sizing:
            return True

        if node.layout.sizing_vertical in content_sizing:
            return True

        if node.type == "TEXT":
            return True

        if cls.contains_semantic_content(node, cfg):
            return True

        return False

    @classmethod
    def classify(cls, root: IRNode, cfg: dict) -> tuple[list[IRNode], list[IRNode]]:
        """Разбивает верхнеуровневые блоки секции на декоративные и контентные.

        Рекурсивно спускается через «прозрачные» промежуточные группы (не
        декоративные и не граница контента), пока не найдёт настоящие
        декоративные или контентные блоки. Узел без детей, не признанный ни
        декоративным, ни контентным, считается декоративным.

        Args:
            root: Корневой узел секции.
            cfg: Раздел `responsive_detection` из `analysis_rules.json`.

        Returns:
            tuple[list[IRNode], list[IRNode]]: Пара `(decorative, content)`.
            Контентные блоки отсортированы по `rel_geometry.absolute_y`
            для стабильного порядка в потоке документа.
        """
        decorative: list[IRNode] = []
        content: list[IRNode] = []

        def visit(node: IRNode) -> None:
            """Относит `node` к decorative или content либо спускается в его детей.
            Args:
                node: Текущий верхнеуровневый блок секции или его потомок.
            """
            if cls.is_decorative(node, root, cfg):
                decorative.append(node)
                return
            if cls.is_content_boundary(node, cfg):
                content.append(node)
                return
            if node.children:
                for child in node.children:
                    visit(child)
                return
            decorative.append(node)

        for child in root.children:
            visit(child)

        content.sort(key=lambda n: n.rel_geometry.absolute_y)
        return decorative, content


# ---------------------------------------------------------------------------
# CSS-хелперы, общие для WebRenderer и ReferenceRenderer
# ---------------------------------------------------------------------------
def resolve_border_radius_css(node: "IRNode") -> str | None:
    """Вычисляет значение CSS `border-radius` для узла.

    Единая точка расчёта для `WebRenderer` и `ReferenceRenderer`: общая функция
    не даёт двум рендерерам разойтись. У Figma ELLIPSE поля `cornerRadius` нет,
    поэтому `style.border_radius` у эллипса всегда 0, но сам эллипс круглый по
    типу узла. `border-radius: 50%` рисует эллипс, вписанный в bounding box, при
    любых пропорциях, поэтому подходит и для кругов, и для овалов без привязки
    к размеру ассета, id или имени узла.

    Args:
        node: Узел, для которого вычисляется скругление.

    Returns:
        str | None: `"50%"` для ELLIPSE, `"<N>px"` при `border_radius > 0`,
        иначе `None` (скругление не нужно).
    """
    if node.type == "ELLIPSE":
        return "50%"
    if node.style.border_radius > 0:
        return f"{node.style.border_radius}px"
    return None


def resolve_border_css(node: "IRNode") -> str | None:
    """Вычисляет CSS-декларацию `border` (ширина, стиль, цвет) для узла.

    Единая точка расчёта для `WebRenderer` и `ReferenceRenderer`. Число и
    единица измерения должны идти без пробела (`1px`, а не `1 px`): иначе
    браузер не распознаёт значение как `<length>` и отбрасывает всё свойство
    `border` целиком.

    Args:
        node: Узел, для которого вычисляется рамка.

    Returns:
        str | None: Строка вида `border: 1px solid <цвет>`; `None`, если у
        узла нет рамки (нулевая ширина или не задан цвет).
    """
    if node.style.border_width > 0 and node.style.border_color:
        return f"border: {node.style.border_width}px solid {node.style.border_color}"
    return None


# ---------------------------------------------------------------------------
# Рендер финального адаптивного HTML
# ---------------------------------------------------------------------------
class WebRenderer:
    """Рендерит IR-дерево `ResolvedSectionSpec` в HTML и CSS.

    Точка входа — `render_page`: собирает полную страницу из всех секций.
    `node_to_html` рекурсивно рендерит один узел (адаптивная раскладка либо
    абсолютное позиционирование по координатам Figma); `gradient_to_css` и
    `effects_to_css` переводят стилевые данные IR в CSS-выражения.
    """

    @staticmethod
    def gradient_to_css(grad) -> str:
        """Преобразует `GradientData` в CSS-выражение градиента.

        Формат зависит от `grad.css_type`:
          * `linear` -> `linear-gradient(<угол>deg, <стопы>)`;
          * `radial` -> `radial-gradient(ellipse at <x>% <y>%, <стопы>)`;
          * `conic`  -> `conic-gradient(from <угол>deg at <x>% <y>%, <стопы>)`.

        Args:
            grad: Объект `GradientData` (`css_type`, `angle_deg`, `stops`,
                `center_x_pct`, `center_y_pct`).

        Returns:
            str: CSS-выражение градиента; `"none"`, если стопов нет.
        """
        stops_css = ", ".join(f"{s.color} {round(s.position * 100, 1)}%" for s in grad.stops)
        if not stops_css:
            return "none"

        if grad.css_type == "radial":
            center_x = float(getattr(grad, "center_x_pct", 50.0))
            center_y = float(getattr(grad, "center_y_pct", 50.0))
            return f"radial-gradient(ellipse at {center_x:.2f}% {center_y:.2f}%, {stops_css})"

        if grad.css_type == "conic":
            center_x = float(getattr(grad, "center_x_pct", 50.0))
            center_y = float(getattr(grad, "center_y_pct", 50.0))
            return f"conic-gradient(from {grad.angle_deg}deg at {center_x:.2f}% {center_y:.2f}%, {stops_css})"

        return f"linear-gradient({grad.angle_deg}deg, {stops_css})"

    @staticmethod
    def effects_to_css(node: IRNode) -> list[str]:
        """Переводит эффекты узла в список CSS-деклараций.
        Обрабатываются видимые эффекты из `node.style.extras["effects"]`:
        LAYER_BLUR и DROP_SHADOW превращаются в единое свойство `filter`,
        BACKGROUND_BLUR — в `backdrop-filter`. Если задан `retint_hue_deg`,
        в `filter` добавляются `hue-rotate`, а при заметном отклонении от 1 —
        `saturate` и `brightness`. Коэффициенты радиуса берутся из
        `render_rules.json` (`blur`, `drop_shadow`).
        Эффекты других типов (в том числе INNER_SHADOW) игнорируются.
        Args:
            node: Узел, чьи эффекты переводятся.
        Returns:
            list[str]: Декларации вида `"filter: ..."`, `"backdrop-filter: ..."`.
            Пустой список, если эффектов нет.
        """
        css: list[str] = []
        effects = node.style.extras.get("effects", []) if isinstance(node.style.extras, dict) else []
        blur_cfg = RENDER_RULES.get("blur", {})
        layer_factor = float(blur_cfg.get("layer_radius_factor", 0.5))
        background_factor = float(blur_cfg.get("background_radius_factor", 0.5))
        drop_shadow_cfg = RENDER_RULES.get("drop_shadow", {})
        shadow_radius_factor = float(drop_shadow_cfg.get("radius_factor", 1.0))

        # LAYER_BLUR и DROP_SHADOW оба записываются в CSS-свойство `filter`.
        # Два отдельных `filter: ...` в style-атрибуте не суммируются: второе
        # перезаписало бы первое. Поэтому части собираются в список и
        # выводятся одной декларацией.
        filter_parts: list[str] = []
        for effect in effects:
            if not isinstance(effect, dict):
                continue

            if not effect.get("visible", True):
                continue

            effect_type = effect.get("type")

            if effect_type == "LAYER_BLUR":
                try:
                    raw_radius = float(effect.get("radius", 0.0))
                except (TypeError, ValueError):
                    continue

                if raw_radius <= 0:
                    continue

                radius = raw_radius * layer_factor
                filter_parts.append(f"blur({radius:.2f}px)")

            elif effect_type == "BACKGROUND_BLUR":
                try:
                    raw_radius = float(effect.get("radius", 0.0))
                except (TypeError, ValueError):
                    continue

                if raw_radius <= 0:
                    continue

                radius = raw_radius * background_factor
                css.append(f"backdrop-filter: blur({radius:.2f}px)")
                css.append(f"-webkit-backdrop-filter: blur({radius:.2f}px)")

            elif effect_type == "DROP_SHADOW":
                try:
                    raw_radius = float(effect.get("radius", 0.0))
                except (TypeError, ValueError):
                    raw_radius = 0.0

                offset = effect.get("offset", {}) or {}
                try:
                    offset_x = float(offset.get("x", 0.0))
                except (TypeError, ValueError):
                    offset_x = 0.0

                try:
                    offset_y = float(offset.get("y", 0.0))
                except (TypeError, ValueError):
                    offset_y = 0.0

                shadow_color = parse_rgba(effect.get("color", {}))
                blur_radius = raw_radius * shadow_radius_factor
                filter_parts.append(
                    f"drop-shadow({offset_x:.2f}px {offset_y:.2f}px {blur_radius:.2f}px {shadow_color})")

        if node.style.retint_hue_deg:
            filter_parts.append(f"hue-rotate({node.style.retint_hue_deg}deg)")
            if node.style.retint_saturate and abs(node.style.retint_saturate - 1.0) > 0.05:
                filter_parts.append(f"saturate({node.style.retint_saturate})")
            if node.style.retint_brightness and abs(node.style.retint_brightness - 1.0) > 0.05:
                filter_parts.append(f"brightness({node.style.retint_brightness})")

        if filter_parts:
            css.append(f"filter: {' '.join(filter_parts)}")

        return css

    @staticmethod
    def node_to_html(node: IRNode, positioning_mode: Literal["flow", "absolute"] = "flow", force_absolute: bool = False,
                     guard_gap: float | None = None) -> str:
        """Рекурсивно рендерит узел IR-дерева и его детей в HTML.
        Шаги, отмеченные в теле функции: позиционирование, обрезка, фон, рамка,
        режим наложения, эффекты, типографика, затем вывод ассета, текста или
        контейнера с детьми.
        Узел `raster_composite` в режиме `flow` возвращается досрочно, в шаге 2:
        шаги 3-9 (обрезка, рамка, режим наложения, эффекты) для него не
        выполняются. Из них применяются только скругление и прозрачность.
        Args:
            node: Рендерируемый узел.
            positioning_mode: `"absolute"` — `position: absolute` по координатам
                и констрейнтам Figma; `"flow"` — обычный поток документа
                (адаптивный контент).
            force_absolute: Принудительно использовать absolute-позиционирование
                для детей, даже внутри flow-родителя (декоративные слои).
            guard_gap: Отступ до соседнего элемента справа, px. Нужен только
                HUG-тексту внутри Figma Auto Layout: передаётся в атрибут
                `data-autofit-guard-gap` (см. шаг 10 и `AUTOFIT_SCRIPT`).
                Значение — `itemSpacing` родителя из Figma.
        Returns:
            str: HTML-фрагмент узла: `<img>` для ассетов, тег текста
            для TEXT, иначе контейнер с отрендеренными детьми.
        """
        css: list[str] = []
        if node.html_tag == "button":
            css.extend(
                [
                    "appearance: none",
                    "-webkit-appearance: none",
                    "background: transparent",
                    "border: none",
                    "margin: 0",
                    "padding: 0",
                    "font: inherit",
                    "color: inherit",
                    "text-align: inherit",
                    "cursor: pointer",
                ]
            )

        is_asset = node.render_strategy in ("asset", "raster_composite") and bool(node.asset_path)
        is_raster_composite = node.render_strategy == "raster_composite" and bool(node.asset_path)
        is_hug_text = node.type == "TEXT" and node.layout.sizing_horizontal == "HUG"
        intended_single_line = node.type == "TEXT" and node.layout.intended_single_line

        # Шаг 1. Абсолютное позиционирование по координатам и констрейнтам Figma.
        if positioning_mode == "absolute":
            css.append("position: absolute")
            parent_w = max(float(node.rel_geometry.parent_width), 1.0)
            parent_h = max(float(node.rel_geometry.parent_height), 1.0)
            g = node.rel_geometry

            # Горизонталь.
            if node.layout.h_constraint == "scale":
                left_pct = g.x / parent_w * 100.0
                width_pct = g.width / parent_w * 100.0
                css.append(f"left: {left_pct:.4f}%")
                # HUG-тексту процентная ширина не задаётся: она рассчитана под
                # оригинальный шрифт Figma, а в браузере используется веб-замена.
                if is_hug_text:
                    css.append("width: max-content")
                else:
                    css.append(f"width: {width_pct:.4f}%")

            elif node.layout.h_constraint == "right":
                right_px = parent_w - (g.x + g.width)
                css.append(f"right: {right_px}px")
                if is_hug_text:
                    css.append("width: max-content")
                elif g.width > 0:
                    css.append(f"width: {g.width}px")

            elif node.layout.h_constraint == "center":
                css.append("left: 50%")
                css.append("transform: translateX(-50%)")
                if is_hug_text:
                    css.append("width: max-content")
                elif g.width > 0:
                    css.append(f"width: {g.width}px")

            elif node.layout.h_constraint == "stretch":
                left_px = g.x
                right_px = parent_w - (g.x + g.width)
                css.append(f"left: {left_px}px")
                css.append(f"right: {right_px}px")

            else:
                css.append(f"left: {g.x}px")
                if is_hug_text:
                    css.append("width: max-content")
                elif g.width > 0:
                    css.append(f"width: {g.width}px")

            # Вертикаль.
            is_hug_vertical = node.layout.sizing_vertical == "HUG"

            def height_rule(px: float) -> str:
                """Формирует правило высоты для absolute-позиционирования.
                Args:
                    px: Высота узла, px.
                Returns:
                    str: `min-height: <px>px` для HUG по вертикали (контент может
                    вырасти), иначе `height: <px>px`.
                """
                prop = "min-height" if is_hug_vertical else "height"
                return f"{prop}: {px}px"

            if node.layout.v_constraint == "scale":
                top_pct = g.y / parent_h * 100.0
                height_pct = g.height / parent_h * 100.0
                css.append(f"top: {top_pct:.4f}%")
                css.append(f"height: {height_pct:.4f}%")

            elif node.layout.v_constraint == "bottom":
                bottom_px = parent_h - (g.y + g.height)
                css.append(f"bottom: {bottom_px}px")
                if g.height > 0:
                    css.append(height_rule(g.height))

            elif node.layout.v_constraint == "center":
                css.append("top: 50%")
                # Центрирование по обеим осям объединяется в один transform.
                if node.layout.h_constraint == "center":
                    css = [rule for rule in css if not rule.startswith("transform:")]
                    css.append("transform: translate(-50%, -50%)")
                else:
                    css.append("transform: translateY(-50%)")

                if g.height > 0:
                    css.append(height_rule(g.height))

            elif node.layout.v_constraint == "stretch":
                top_px = g.y
                bottom_px = parent_h - (g.y + g.height)
                css.append(f"top: {top_px}px")
                css.append(f"bottom: {bottom_px}px")

            else:
                css.append(f"top: {g.y}px")
                if g.height > 0:
                    css.append(height_rule(g.height))

        # Шаг 2. Поток документа: Auto Layout Figma -> flexbox.
        else:
            layout = node.layout
            if positioning_mode == "flow" and layout.layout_positioning == "ABSOLUTE":
                # layoutPositioning=ABSOLUTE меняет режим только для детей этого узла (шаг 11). Сам узел остаётся в
                # потоке: его CSS уже сформирован для flow.
                positioning_mode = "absolute"

            if layout.is_flex:
                css.append("display: flex")
                css.append(f"flex-direction: {layout.direction}")
                if layout.gap > 0:
                    css.append(f"gap: {layout.gap}px")

                if any([layout.padding_top, layout.padding_right, layout.padding_bottom, layout.padding_left]):
                    css.append(
                        f"padding: {layout.padding_top}px {layout.padding_right}px {layout.padding_bottom}px {layout.padding_left}px")

                css.append(f"align-items: {layout.align_items}")
                css.append(f"justify-content: {layout.justify_content}")
                if layout.wrap:
                    css.append("flex-wrap: wrap")

            else:
                css.append("position: relative")

            if layout.sizing_horizontal == "FILL":
                css.extend(["width: 100%", "flex: 1 1 0%"])

            elif is_hug_text:
                css.append("width: max-content")

            elif layout.sizing_horizontal == "FIXED" and layout.width > 0:
                css.append("width: 100%")
                css.append(f"max-width: {layout.width}px")
                if layout.sizing_vertical == "FIXED" and layout.height > 0 and not layout.is_flex:
                    css.append(f"min-height: {layout.height}px")

            # Досрочный выход: в режиме flow raster_composite рендерится здесь и
            # не доходит до шагов 3-9 (blend, эффекты, hue-rotate перекраски).
            if is_raster_composite:
                radius_css = resolve_border_radius_css(node)
                if radius_css:
                    css.append(f"border-radius: {radius_css}")
                if node.style.opacity < 1.0:
                    css.append(f"opacity: {node.style.opacity}")

                style_attr = f'style="{"; ".join(css)}"' if css else ""
                return f'<img src="{html.escape(node.asset_path)}" alt="{html.escape(node.name)}" class="ui-asset ui-composite-asset" {style_attr} />'

        # Шаг 3. Обрезка: `clipsContent` из Figma -> `overflow: hidden`.
        # Проверяется здесь, а не внутри ветки потока, чтобы работать при любом
        # позиционировании: иначе декоративные слои absolute-узла вылезали бы за
        # край фрейма-маски.
        if node.layout.clips_content:
            css.append("overflow: hidden")

        # Шаг 4. Фон: цвет или градиент.
        # Для ассетов фон не применяется: заливка уже растеризована внутри
        # самого файла. Повторный CSS-фон закрыл бы сплошным прямоугольником
        # прозрачные зоны картинки (свечение, «хвост» и т.п.). Так же ведёт себя
        # `ReferenceRenderer`.
        if not is_asset:
            if node.style.bg_gradient and node.style.bg_gradient.stops:
                css.append(f"background: {WebRenderer.gradient_to_css(node.style.bg_gradient)}")

            elif node.style.bg_color:
                css.append(f"background-color: {node.style.bg_color}")

        # Шаг 5. Рамка и скругление.
        radius_css = resolve_border_radius_css(node)
        if radius_css:
            css.append(f"border-radius: {radius_css}")

        border_css = resolve_border_css(node)
        if border_css:
            css.append(border_css)

        # Шаг 6. Режим наложения: Figma blendMode -> CSS mix-blend-mode.
        blend_map = RENDER_RULES.get("figma_blend_mode_to_css", {})
        blend_mode = node.style.blend_mode
        if blend_mode not in {None, "", "NORMAL", "PASS_THROUGH"}:
            css_blend = blend_map.get(blend_mode)
            if css_blend:
                css.append(f"mix-blend-mode: {css_blend}")
            else:
                logger.debug(
                    f"Unsupported Figma blendMode '{blend_mode}' for node '{node.name}' ({node.id}). CSS fallback: normal.")

        # Шаг 7. Эффекты: тени, блюр, перекраска (hue-rotate).
        css.extend(WebRenderer.effects_to_css(node))

        # Шаг 8. Типографика: цвет, шрифт, размер, интервалы, выравнивание.
        if node.style.text_gradient and node.style.text_gradient.stops:
            if node.style.text_color:
                css.append(f"color: {node.style.text_color}")
            css.append(f"background: {WebRenderer.gradient_to_css(node.style.text_gradient)}")
            css.append("background-clip: text")
            css.append("-webkit-background-clip: text")
            css.append("color: transparent")
        elif node.style.text_color:
            css.append(f"color: {node.style.text_color}")

        if node.style.text_align and node.type == "TEXT":
            css.append(f"text-align: {node.style.text_align}")

        if node.style.font_family:
            resolved_font_name, _ = resolve_font(node.style.font_family)
            css.append(f"font-family: '{resolved_font_name}', sans-serif")

        if node.style.font_size:
            css.append(f"font-size: {node.style.font_size}px")

        if node.style.font_weight:
            effective_weight = apply_font_weight_offset(node.style.font_weight, node.style.font_family)
            css.append(f"font-weight: {effective_weight}")
        if node.style.line_height_percent:
            css.append(f"line-height: {node.style.line_height_percent / 100}")

        if node.style.letter_spacing:
            css.append(f"letter-spacing: {node.style.letter_spacing}px")

        if is_hug_text or intended_single_line:
            css.append("white-space: nowrap")

        for prop, value in node.style.custom_css.items():
            css.append(f"{prop}: {value}")

        style_attr = f'style="{"; ".join(css)}"' if css else ""

        # Шаг 9. Ассет: готовый экспортированный файл (PNG/SVG) как <img>.
        if is_asset:
            return f'<img src="{html.escape(node.asset_path)}" alt="{html.escape(node.name)}" class="ui-asset" {style_attr} />'

        # Шаг 10. Текстовый узел: собственный тег (h1/h2/p/span) с текстом.
        if node.type == "TEXT":
            tag = node.html_tag or "p"
            content = html.escape(node.characters or "")
            role = node.semantic_role or "body"
            autofit_attr = ' data-autofit="1"' if intended_single_line else ""
            # `guard_gap` задаёт вызывающий код (шаг 11) только для HUG-текста внутри
            # настоящего Figma Auto Layout с соседом справа: после замены шрифта
            # такой текст мог бы наехать на соседний элемент.
            guard_attr = f' data-autofit-guard-gap="{guard_gap}"' if guard_gap is not None else ""
            return f'<{tag} class="ui-text role-{role}"{autofit_attr}{guard_attr} {style_attr}>{content}</{tag}>'

        # Шаг 11. Дети: рекурсивный рендер дочерних узлов.
        child_positioning_mode: Literal["flow", "absolute"] = "flow"
        if force_absolute:
            child_positioning_mode = "absolute"

        elif positioning_mode == "absolute" and node.layout.layout_strategy == "relative":
            child_positioning_mode = "absolute"

        children = node.children
        inner_html_parts: list[str] = []
        for i, child in enumerate(children):
            # Кандидат на защиту от наезда на соседа: HUG-текст внутри настоящего
            # Figma Auto Layout (`is_flex`, а не наша реконструкция) с соседом
            # справа. Узлы с `layoutPositioning=ABSOLUTE` исключены: у оверлеев
            # и декора пересечение с соседом задумано дизайнером.
            child_is_hug_text = child.type == "TEXT" and child.layout.sizing_horizontal == "HUG"
            child_has_next_sibling = (i + 1) < len(children)
            child_guard_gap: float | None = node.layout.gap if (
                    node.layout.is_flex and child_is_hug_text and child_has_next_sibling and child.layout.layout_positioning != "ABSOLUTE") else None
            inner_html_parts.append(
                WebRenderer.node_to_html(child, positioning_mode=child_positioning_mode, force_absolute=force_absolute,
                                         guard_gap=child_guard_gap))
        inner_html = "".join(inner_html_parts)
        tag = node.html_tag or "div"
        return f'<{tag} class="ui-container node-{node.type.lower()}" {style_attr}>{inner_html}</{tag}>'

    @classmethod
    def render_page(cls, landing: LandingSpec) -> str:
        """Собирает полный HTML-документ лендинга.

        Для каждой секции узлы верхнего уровня делятся на декоративный слой
        (всегда `position: absolute`) и контентный (адаптивный поток либо
        масштабируемый абсолютный холст, в зависимости от `responsive_capable`).
        Подключает шрифты дизайн-системы и реально использованные в узлах,
        задаёт CSS-переменные секций и встраивает `AUTOFIT_SCRIPT`.

        Args:
            landing: Собранная спецификация лендинга: дизайн-система, секции
                после переноса стиля и настройки рендера.

        Returns:
            str: Готовый HTML-документ.
        """
        logger.info("Компиляция лендинга в единый HTML (двухслойный responsive-рендер)...")
        ds = landing.design_system
        imports = []

        def collect_font_imports(node: IRNode) -> None:
            """Добавляет в `imports` уникальные `@import` для всех `font_family` в поддереве.
            Args:
                node: Корень обходимого поддерева.
            """

            family = node.style.font_family
            if family:
                _, import_stmt = resolve_font(family)
                if import_stmt and import_stmt not in imports:
                    imports.append(import_stmt)

            for child in node.children:
                collect_font_imports(child)

        # Сначала шрифты дизайн-системы, затем реально присутствующие в узлах.
        for role_token in ds.tokens.typography.values():
            _, import_stmt = resolve_font(role_token.family)
            if import_stmt and import_stmt not in imports:
                imports.append(import_stmt)

        for section in landing.sections:
            collect_font_imports(section.resolved_root)

        css_imports = "\n".join(imports)
        resp_cfg = ANALYSIS_RULES.get("responsive_detection", {})
        responsive_cfg = RENDER_RULES.get("responsive", {})
        content_gap_token = responsive_cfg.get("content_gap_token", "md")
        content_gap = ds.tokens.spacing.get(content_gap_token, 16.0)
        content_padding_token = responsive_cfg.get("content_padding_token", "md")
        fallback_content_padding = ds.tokens.spacing.get(content_padding_token, 16.0)
        language = landing.render.language if landing.render.language else "en"
        sections_html = []
        for sec in landing.sections:
            w = int(sec.geometry.width or 1440)
            h = int(sec.geometry.height or 900)
            root_layout = sec.resolved_root.layout
            padding_top = max(root_layout.padding_top, 0.0)
            padding_right = max(root_layout.padding_right, 0.0)
            padding_bottom = max(root_layout.padding_bottom, 0.0)
            padding_left = max(root_layout.padding_left, 0.0)

            # Реальный padding секции из Figma приоритетнее запасного значения из конфига.
            if not any([padding_top, padding_right, padding_bottom, padding_left]):
                padding_top = fallback_content_padding
                padding_right = fallback_content_padding
                padding_bottom = fallback_content_padding
                padding_left = fallback_content_padding

            decorative_children, content_children = ResponsiveLayerClassifier.classify(sec.resolved_root, resp_cfg)
            logger.info(
                f"  [{sec.id}] Слои: decorative={len(decorative_children)}, content={len(content_children)} блоков верхнего уровня (было responsive_capable={sec.responsive_capable})")

            # Декоративный слой всегда позиционный, а не document-flow, поэтому
            # force_absolute=True безопасен и для responsive-секций.
            decor_html = "".join(
                WebRenderer.node_to_html(child, positioning_mode="absolute", force_absolute=True) for child in
                decorative_children)
            if sec.responsive_capable:
                content_html = "".join(
                    WebRenderer.node_to_html(child, positioning_mode="flow") for child in content_children)
            else:
                fixed_inner = "".join(
                    WebRenderer.node_to_html(child, positioning_mode="absolute", force_absolute=True)
                    for child in content_children
                )
                content_html = f'<div class="fixed-content-scaler">{fixed_inner}</div>'

            # min-height секции считается тем же коэффициентом масштаба
            # (min(1, 100vw / design-w)), что и у decor-scaler и fixed-content-scaler.
            # Иначе на узких экранах контейнер не сжимался бы вместе с уже
            # уменьшенным содержимым, и между ними возникал бы пустой промежуток.
            sections_html.append(
                f'<section class="landing-section" id="{sec.id}" '
                f'style="--design-w:{w}px; --design-h:{h}px; '
                f'min-height: calc(var(--design-h) * min(1, calc(100vw / var(--design-w))));">'
                f'<div class="section-bg-decor"><div class="decor-scaler">{decor_html}</div></div>'
                f'<div class="section-content" '
                f'style="'
                f"--content-gap:{content_gap}px;"
                f"--padding-top:{padding_top}px;"
                f"--padding-right:{padding_right}px;"
                f"--padding-bottom:{padding_bottom}px;"
                f"--padding-left:{padding_left}px;"
                f'">'
                f"{content_html}"
                f"</div>"
                f"</section>"
            )

        c_canvas = ds.tokens.colors["canvas"].value
        c_text = ds.tokens.colors["text_primary"].value
        body_font_source = ds.tokens.typography.get("body", TypographyToken(family="Inter", size=16, weight=400)).family
        body_font, _ = resolve_font(body_font_source)
        font_stack = ", ".join(RENDER_RULES["font_fallbacks"])
        return f"""<!DOCTYPE html>
        <html lang="{html.escape(language)}">
        <head>
          <meta charset="UTF-8">
          <meta name="viewport" content="width=device-width, initial-scale=1.0">
          <title>{landing.project_name}</title>
          <style>
    {css_imports}
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      background-color: {c_canvas};
      color: {c_text};
      font-family: '{body_font}', {font_stack};
      display: flex;
      flex-direction: column;
      align-items: center;
      width: 100%;
      overflow-x: hidden;
    }}
    .landing-section {{
    position: relative;
    width: 100%;
    min-width: 0;
    display: flex;
    justify-content: center;
    overflow: hidden;
    }}
    .section-bg-decor {{
      position: absolute;
      inset: 0;
      overflow: hidden;
      z-index: 0;
      pointer-events: none;
    }}
    .section-bg-decor .decor-scaler {{
      position: relative;
      width: var(--design-w);
      height: var(--design-h);
      transform-origin: top left;
      transform: scale(min(1, calc(100vw / var(--design-w))));
    }}
    .section-content {{
    position: relative;
    z-index: 1;

    width: min(100%, var(--design-w));
    max-width: var(--design-w);
    min-width: 0;

    display: flex;
    flex-direction: column;
    align-items: center;

    gap: var(--content-gap, 16px);

    padding-top: var(--padding-top, 16px);
    padding-right: var(--padding-right, 16px);
    padding-bottom: var(--padding-bottom, 16px);
    padding-left: var(--padding-left, 16px);

    box-sizing: border-box;
    }}
    .fixed-content-scaler {{
    position: absolute;
    top: 0;
    left: 0;
    width: var(--design-w);
    height: var(--design-h);
    transform-origin: top left;
    transform: scale(min(1, calc(100vw / var(--design-w))));
    pointer-events: auto;
    }}
    button {{ cursor: pointer; border: none; outline: none; }}
  </style>
</head>
<body>
  {"".join(sections_html)}
<script>
(function () {{
    const run = {AUTOFIT_SCRIPT};
    if (document.fonts && document.fonts.ready) {{
        document.fonts.ready.then(run);
    }} else {{
        window.addEventListener('load', run);
    }}
}})();
</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Рендер эталона для Extraction QA
# ---------------------------------------------------------------------------
class ReferenceRenderer:
    """Рендерит оригинальное (дотрансферное) дерево секции для Extraction QA.

    Не использует `WebRenderer.node_to_html`: там нужна адаптивность
    (flex, проценты), а здесь — точное совпадение с пиксельными координатами,
    которые Figma вычислила для оригинала. Разные задачи решаются разными
    рендерерами.
    """

    @staticmethod
    def resolve_reference_bg_color(section: ResolvedSectionSpec, design_system: DesignSystemSpec | None) -> tuple[
        str, str]:
        """Определяет цвет фона эталонного рендера секции.

        Источники проверяются по порядку, первый подходящий побеждает:
          1. Собственный `bg_color` корня секции.
          2. Взвешенный скоринг по дереву секции (без ASSET, декоративных
             режимов наложения и градиентных приближений; вес — по покрытию
             видимой области).
          3. Backing-цвет под прозрачной областью референсного PNG, если
             прозрачных пикселей достаточно (`min_transparent_fraction_for_backing`).
          4. Доминирующий цвет референсного PNG, но только если непрозрачная
             область достаточно велика (`min_opaque_page_coverage`) и
             однородна (`max_opaque_color_std`) либо в ней есть явный
             доминирующий цвет: то есть это плоская заливка, а не текстурная сцена.
          5. Токен `canvas` дизайн-системы, если он посчитан из того же Figma-файла.
          6. Значение по умолчанию из `defaults.json`.

        Args:
            section: Секция, для которой определяется фон.
            design_system: Дизайн-система (для токена `canvas`); может быть `None`.

        Returns:
            tuple[str, str]: Пара `(цвет, метка источника)`. Метка
            (`root_bg`, `weighted_scan`, `transparent_backing_match`,
            `png_sampling`, `design_system_canvas`, `default`) попадает в
            `qa_report.json` для трассировки.
        """
        bg_color = section.original_root.style.bg_color
        if bg_color and bg_color != "transparent":
            logger.info(f"  [{section.id}] Фон референса взят из корня секции: {bg_color}")
            return bg_color, "root_bg"

        cfg = ANALYSIS_RULES.get("reference_bg_detection", {})
        root_w = section.geometry.width or section.original_root.rel_geometry.width
        root_h = section.geometry.height or section.original_root.rel_geometry.height
        candidates = collect_background_candidates(section.original_root, root_w, root_h, cfg)
        if candidates:
            score, color, name = candidates[0]
            logger.info(
                f"  [{section.id}] Фон референса определён взвешенным скорингом: {color} (узел '{name}', score={score:.3f}, кандидатов={len(candidates)})")
            return color, "weighted_scan"

        if section.reference_image:
            png_path = PROJECT_ROOT / section.reference_image
            if png_path.exists():
                ce_cfg = PIPELINE_SETTINGS.get("color_extraction", {})
                min_sample_alpha = ce_cfg.get("min_sample_alpha", 0.5)
                min_opaque_coverage = cfg.get("min_opaque_page_coverage", 0.5)
                max_color_std = cfg.get("max_opaque_color_std", 20.0)
                min_dominant_coverage = cfg.get("min_dominant_color_coverage", 0.4)

                # Прежде чем доверять png_sampling, проверяем однородность цвета
                # и наличие доминирующего оттенка.
                stats = analyze_opaque_region(png_path, min_alpha=min_sample_alpha,
                                              resize_dim=cfg.get("png_fallback_resize", 64))
                is_flat_enough = stats.color_std <= max_color_std
                has_dominant_color = stats.dominant_color_coverage >= min_dominant_coverage

                # Backing-цвет под прозрачностью проверяется первым и независимо от
                # доли непрозрачных пикселей. Типичный паттерн «карточка на холсте»
                # даёт одновременно большую непрозрачную область (сама карточка) и
                # большую прозрачную (фон холста вокруг): доля непрозрачных
                # пикселей не говорит, какой цвет — настоящий фон секции.
                # Прозрачность в экспорте Figma — явный сигнал «здесь просвечивает
                # фон страницы», поэтому важно лишь, достаточно ли прозрачной области.
                transparent_ratio = 1.0 - stats.coverage_ratio
                min_transparent_for_backing = cfg.get("min_transparent_fraction_for_backing", 0.05)
                if transparent_ratio >= min_transparent_for_backing:
                    backing_rgb = sample_transparent_backing_rgb(png_path,
                                                                 max_alpha=cfg.get("transparent_backing_max_alpha",
                                                                                   0.05),
                                                                 resize_dim=cfg.get("png_fallback_resize", 64))
                    if backing_rgb:
                        logger.info(
                            f"  [{section.id}] Прозрачная область референса "
                            f"({transparent_ratio:.0%} кадра, >= порога "
                            f"{min_transparent_for_backing:.0%}) достаточно большая — "
                            f"фон взят из реального backing-цвета под прозрачностью "
                            f"PNG: {backing_rgb}"
                        )
                        return backing_rgb, "transparent_backing_match"

                if stats.coverage_ratio < min_opaque_coverage:
                    logger.info(
                        f"  [{section.id}] Непрозрачных пикселей референса всего "
                        f"{stats.coverage_ratio:.0%} (< порога {min_opaque_coverage:.0%}), "
                        "а надёжного backing-цвета под прозрачностью тоже нет — "
                        "у секции нет сплошного фона на весь бокс, пропускаю "
                        "png_sampling."
                    )
                elif not is_flat_enough and not has_dominant_color:
                    logger.info(
                        f"  [{section.id}] Непрозрачная область референса покрывает "
                        f"{stats.coverage_ratio:.0%}, но цвет в ней неоднороден "
                        f"(std={stats.color_std:.1f} > порога {max_color_std}) и "
                        f"явного доминирующего цвета тоже нет "
                        f"(dominant_coverage={stats.dominant_color_coverage:.0%} < "
                        f"порога {min_dominant_coverage:.0%}) — "
                        "это текстурная сцена/карточка, а не плоская заливка. "
                        "Пропускаю png_sampling, чтобы не покрасить всю секцию "
                        "случайным средним цветом этой сцены."
                    )
                else:
                    composite_bg = None
                    if design_system and design_system.source.resource_id == section.source.resource_id and "canvas" in design_system.tokens.colors:
                        composite_bg = parse_color_to_rgb_tuple(design_system.tokens.colors["canvas"].value)

                    sampled = sample_dominant_colors_from_png(png_path, k=1,
                                                              resize_dim=cfg.get("png_fallback_resize", 40),
                                                              min_alpha=min_sample_alpha, composite_bg=composite_bg)
                    if sampled:
                        logger.info(
                            f"  [{section.id}] Ни один узел не набрал min_coverage_ratio — "
                            f"фон взят alpha-aware PNG-сэмплингом референса "
                            f"(min_alpha={min_sample_alpha}, composite_bg={composite_bg}, "
                            f"coverage={stats.coverage_ratio:.0%}, std={stats.color_std:.1f}): {sampled[0]}"
                        )
                        return sampled[0], "png_sampling"

        if design_system and design_system.source.resource_id == section.source.resource_id and "canvas" in design_system.tokens.colors:
            logger.info(f"  [{section.id}] Фон референса взят из design_system.canvas (тот же Figma-файл)")
            return str(design_system.tokens.colors["canvas"].value), "design_system_canvas"

        logger.warning(f"  [{section.id}] Не удалось определить фон референса — используется дефолт из defaults.json")
        return DEFAULTS["colors"]["canvas"], "default"

    @staticmethod
    def render_original_tree(section: ResolvedSectionSpec, design_system: DesignSystemSpec | None = None) -> tuple[
        str, str]:
        """Рендерит `original_root` секции в самодостаточный HTML для Extraction QA.

        Узлы позиционируются абсолютно в точных координатах Figma. Результат
        снимается в Playwright и сравнивается с `reference_image` (MAE, SSIM).

        Args:
            section: Секция, чьё дотрансферное дерево рендерится.
            design_system: Дизайн-система (для определения фона); может быть `None`.

        Returns:
            tuple[str, str]: Пара `(html, метка источника фона)`; метка —
            из `resolve_reference_bg_color`.
        """
        root_x = section.original_root.rel_geometry.x
        root_y = section.original_root.rel_geometry.y
        blend_map = RENDER_RULES.get("figma_blend_mode_to_css", {})
        font_fallback_stack = ", ".join(RENDER_RULES["font_fallbacks"])
        font_import_statements: set = set()

        def build_style(n: IRNode, abs_x: float, abs_y: float, include_width: bool = True) -> list[str]:
            """Строит базовые CSS-декларации нетекстового узла: позиция, размер, фон, рамка, эффекты.

            Args:
                n: Узел.
                abs_x: Абсолютная координата X относительно корня секции, px.
                abs_y: Абсолютная координата Y относительно корня секции, px.
                include_width: `False` для HUG-текста: вместо фиксированной ширины
                    ставится `width: max-content`.

            Returns:
                list[str]: Список CSS-деклараций.
            """
            css: list[str] = ["position: absolute", f"left: {abs_x}px", f"top: {abs_y}px"]
            if include_width:
                if n.rel_geometry.width > 0:
                    css.append(f"width: {n.rel_geometry.width}px")
            else:
                css.append("width: max-content")

            if n.rel_geometry.height > 0:
                height_prop = "min-height" if n.layout.sizing_vertical == "HUG" else "height"
                css.append(f"{height_prop}: {n.rel_geometry.height}px")

            if n.style.bg_gradient and n.style.bg_gradient.stops:
                css.append(f"background: {WebRenderer.gradient_to_css(n.style.bg_gradient)}")
            elif n.style.bg_color and n.style.bg_color != "transparent":
                css.append(f"background-color: {n.style.bg_color}")

            radius_css = resolve_border_radius_css(n)
            if radius_css:
                css.append(f"border-radius: {radius_css}")

            border_css = resolve_border_css(n)
            if border_css:
                css.append(border_css)

            opacity = float(n.style.opacity)
            if opacity < 1.0:
                css.append(f"opacity: {opacity}")

            blend_mode = n.style.blend_mode
            if blend_mode not in {None, "", "NORMAL", "PASS_THROUGH"}:
                css_blend = blend_map.get(blend_mode)
                if css_blend:
                    css.append(f"mix-blend-mode: {css_blend}")

            css.extend(WebRenderer.effects_to_css(n))
            return css

        def walk(n: IRNode, p_x: float, p_y: float) -> str:
            """Рендерит поддерево узла `n` и возвращает HTML только этого поддерева.

            Возвращаемое значение (а не запись в общий плоский список) нужно,
            чтобы обрезка `clipsContent` могла обернуть детей узла в
            контейнер с `overflow: hidden`.

            Args:
                n: Текущий узел.
                p_x: Накопленное смещение по X от предков, px.
                p_y: Накопленное смещение по Y от предков, px.

            Returns:
                str: HTML узла и его потомков.
            """
            abs_x = p_x + n.rel_geometry.x
            abs_y = p_y + n.rel_geometry.y
            self_html = ""
            if n.render_strategy in ("asset", "raster_composite") and n.asset_path:
                css = ["position: absolute", f"left: {abs_x}px", f"top: {abs_y}px"]
                if n.rel_geometry.width > 0:
                    css.append(f"width: {n.rel_geometry.width}px")
                if n.rel_geometry.height > 0:
                    css.append(f"height: {n.rel_geometry.height}px")

                opacity = float(n.style.opacity)
                if opacity < 1.0:
                    css.append(f"opacity: {opacity}")

                blend_mode = n.style.blend_mode
                if blend_mode not in {None, "", "NORMAL", "PASS_THROUGH"}:
                    css_blend = blend_map.get(blend_mode)
                    if css_blend:
                        css.append(f"mix-blend-mode: {css_blend}")

                border_css = resolve_border_css(n)
                if border_css:
                    css.append(border_css)
                radius_css = resolve_border_radius_css(n)
                if radius_css:
                    css.append(f"border-radius: {radius_css}")

                # Ассет уже содержит полностью отрисованный Figma визуал своего
                # поддерева (вложенные path/vector/blend-слои), поэтому рекурсия в
                # детей не нужна: иначе поверх готовой картинки рисовались бы
                # грубые bbox-приближения тех же векторных путей. Тот же принцип
                # действует в `WebRenderer.node_to_html` (шаг 9).
                style_attr = "; ".join(css)
                return f'<img src="{n.asset_path}" style="{style_attr};">'

            elif n.type == "TEXT":
                is_hug_text = n.layout.sizing_horizontal == "HUG"
                intended_single_line = n.layout.intended_single_line
                css = build_style(n, abs_x, abs_y, include_width=not is_hug_text)
                lh = n.style.line_height_percent / 100 if n.style.line_height_percent else 1.2
                resolved_font_name = n.style.font_family
                if resolved_font_name:
                    resolved_font_name, font_import_stmt = resolve_font(resolved_font_name)
                    if font_import_stmt:
                        font_import_statements.add(font_import_stmt)

                css.extend(
                    [
                        f"font-size: {n.style.font_size}px",
                        f"font-weight: {apply_font_weight_offset(n.style.font_weight, n.style.font_family)}",
                        f"font-family: '{resolved_font_name}', {font_fallback_stack}" if resolved_font_name else "font-family: sans-serif",
                        f"line-height: {lh}",
                    ]
                )
                if n.style.letter_spacing:
                    css.append(f"letter-spacing: {n.style.letter_spacing}px")

                if is_hug_text or intended_single_line:
                    css.append("white-space: nowrap")

                if n.style.text_align:
                    css.append(f"text-align: {n.style.text_align}")

                if n.style.text_gradient and n.style.text_gradient.stops:
                    css.append(f"background: {WebRenderer.gradient_to_css(n.style.text_gradient)}")
                    css.append("background-clip: text")
                    css.append("-webkit-background-clip: text")
                    css.append("color: transparent")

                elif n.style.text_color:
                    css.append(f"color: {n.style.text_color}")

                autofit_attr = ' data-autofit="1"' if intended_single_line else ""
                style_attr = "; ".join(css)
                self_html = f'<div class="dbg-text"{autofit_attr} style="{style_attr};">{html.escape(n.characters or "")}</div>'

            elif (
                    (n.style.bg_color and n.style.bg_color != "transparent")
                    or (n.style.bg_gradient and n.style.bg_gradient.stops)
                    or (n.style.border_width > 0 and n.style.border_color)
                    or WebRenderer.effects_to_css(n)
            ):
                css = build_style(n, abs_x, abs_y)
                style_attr = "; ".join(css)
                self_html = f'<div style="{style_attr};"></div>'

            needs_clip_wrapper = n.layout.clips_content and n.rel_geometry.width > 0 and n.rel_geometry.height > 0
            if needs_clip_wrapper:
                # Внутри обёртки-маски координаты детей отсчитываются от её левого верхнего угла.
                children_html = "".join(walk(ch, 0.0, 0.0) for ch in n.children)
                wrapper_css = f"position: absolute; left: {abs_x}px; top: {abs_y}px; width: {n.rel_geometry.width}px; height: {n.rel_geometry.height}px; overflow: hidden;"
                return f'{self_html}<div style="{wrapper_css}">{children_html}</div>'

            children_html = "".join(walk(ch, abs_x, abs_y) for ch in n.children)
            return self_html + children_html

        html_body = walk(section.original_root, p_x=-root_x, p_y=-root_y)
        w = int(section.geometry.width or 1440)
        h = int(section.geometry.height or 900)
        bg_color, bg_source_label = ReferenceRenderer.resolve_reference_bg_color(section, design_system)

        # @import для всех шрифтов, реально использованных в дереве (по разрешённым именам).
        font_imports = sorted(font_import_statements)
        css_imports_block = f"<style>{''.join(font_imports)}</style>" if font_imports else ""
        logger.info(
            f"  [{section.id}] Рендеринг оригинального дерева: элементов вложенности учтено, фон={bg_color}, источник={bg_source_label}, шрифтов загружено={len(font_imports)}")
        autofit_script_block = (
            "<script>"
            "(function () {"
            f"const run = {AUTOFIT_SCRIPT};"
            "if (document.fonts && document.fonts.ready) {"
            "document.fonts.ready.then(run);"
            "} else {"
            "window.addEventListener('load', run);"
            "}"
            "})();"
            "</script>"
        )
        html_output = (
            f"<!DOCTYPE html><html><head>{css_imports_block}</head>"
            f'<body style="margin:0; background:{bg_color}; '
            f'width:{w}px; height:{h}px; position:relative; overflow:hidden;">'
            f"{html_body}{autofit_script_block}</body></html>"
        )
        return html_output, bg_source_label


# ---------------------------------------------------------------------------
# QA: проверка качества собранного лендинга
# ---------------------------------------------------------------------------
class QAEngine:
    """Двухуровневая проверка качества собранного лендинга.

    Уровень 1, Extraction QA: для каждой секции сверяет, что извлечение из
    Figma и рендер оригинального дерева корректны сами по себе, до переноса
    стиля (IR fidelity, MAE и SSIM рендера против `section.reference_image`).

    Уровень 2, Final Landing QA: проверяет живую страницу в Playwright
    (структурные инварианты, overflow, пустое пространство, скриншоты по
    вьюпортам, advisory AI Visual QA) и принимает решение о релизе.

    Точка входа — `QAEngine.run`; результат сохраняется в `qa_report.json`.
    """

    @staticmethod
    def _get_pixel_data(img: Image.Image):
        """Возвращает пиксели изображения в виде плоской последовательности.

        Использует `Image.get_flattened_data()` (актуальный API Pillow), а на
        старых версиях откатывается на устаревший `getdata()`. Так исчезает
        `DeprecationWarning` без потери совместимости.

        Args:
            img: Изображение Pillow.

        Returns:
            Последовательность пикселей (кортежи каналов или числа).
        """
        if hasattr(img, "get_flattened_data"):
            return img.get_flattened_data()
        return img.getdata()

    @staticmethod
    def compute_mae(img_a_path: Path, img_b_path: Path, diff_out: Path) -> float:
        """Считает среднюю абсолютную ошибку (MAE) двух изображений и сохраняет diff-картинку.
        Значение нормализовано по 255 и числу каналов (без штрафа диапазон 0..1). Если
        размеры не совпадают, сравнение идёт по пересечению, а к результату
        добавляется `size_penalty` за разницу ширины с учётом size_penalty итог может превысить 1.
        Args:
            img_a_path: Путь к первому изображению.
            img_b_path: Путь ко второму изображению.
            diff_out: Путь, по которому сохраняется картинка разницы.
        Returns:
            float: MAE, округлённая до 4 знаков.
        Raises:
            FileNotFoundError: Если один из входных файлов отсутствует.
        """
        if not img_a_path.exists():
            raise FileNotFoundError(f"QA Failed: отсутствует файл {img_a_path}")
        if not img_b_path.exists():
            raise FileNotFoundError(f"QA Failed: отсутствует файл {img_b_path}")

        im_a = Image.open(img_a_path).convert("RGB")
        im_b = Image.open(img_b_path).convert("RGB")
        if im_a.size != im_b.size:
            w = min(im_a.width, im_b.width)
            h = min(im_a.height, im_b.height)
            crop_a = im_a.crop((0, 0, w, h))
            crop_b = im_b.crop((0, 0, w, h))
            diff = ImageChops.difference(crop_a, crop_b)
            diff.save(diff_out)
            size_penalty = abs(im_a.width - im_b.width) / max(im_a.width, im_b.width)
            stat = QAEngine._get_pixel_data(diff)
            total_err = 0.0
            for p in stat:
                if isinstance(p, (tuple, list)):
                    total_err += sum(p)
                elif isinstance(p, (int, float)):
                    total_err += p
            mae = (total_err / (w * h * 3 * 255)) + size_penalty
            return round(mae, 4)

        diff = ImageChops.difference(im_a, im_b)
        diff.save(diff_out)
        stat = QAEngine._get_pixel_data(diff)
        total_err = 0.0
        for p in stat:
            if isinstance(p, (tuple, list)):
                total_err += sum(p)
            elif isinstance(p, (int, float)):
                total_err += p

        return round(total_err / (im_a.width * im_a.height * 3 * 255), 4)

    @staticmethod
    def compute_ssim(img_a_path: Path, img_b_path: Path) -> float:
        """Считает структурное различие `1 - SSIM` двух изображений в оттенках серого.

        Сравнение идёт по пересечению размеров. Используется гауссово окно
        (как в оригинальной работе Wang et al., 2004): равномерное окно 7x7 по
        умолчанию слишком чувствительно на тонких линиях, где суб-пиксельная
        разница рендеров Chromium и Figma полностью разрушает локальную
        структуру, а гауссово окно сглаживает это и всё равно ловит реальные
        расхождения (смещённые блоки, другую форму букв).

        Args:
            img_a_path: Путь к первому изображению.
            img_b_path: Путь ко второму изображению.

        Returns:
            float: `1 - SSIM`, округлённое до 4 знаков. 0 — изображения
            идентичны; на практике значение не превышает 1, теоретически может
            достигать 2, так как SSIM бывает отрицательным.
        """
        from skimage.metrics import structural_similarity as ssim
        import numpy as np

        im_a = np.array(Image.open(img_a_path).convert("L"))
        im_b = np.array(Image.open(img_b_path).convert("L"))
        h = min(im_a.shape[0], im_b.shape[0])
        w = min(im_a.shape[1], im_b.shape[1])
        score, _ = ssim(im_a[:h, :w], im_b[:h, :w], full=True, gaussian_weights=True, sigma=1.5,
                        use_sample_covariance=False)
        return round(1 - score, 4)

    @staticmethod
    def build_text_mask(size: tuple[int, int], boxes: list[tuple[float, float, float, float]]):
        """Строит булеву маску, где `True` — пиксели внутри любого из прямоугольников.

        Args:
            size: Размер маски `(ширина, высота)`, px.
            boxes: Прямоугольники `(x0, y0, x1, y1)`; выходящие за границы
                обрезаются по размеру маски.

        Returns:
            numpy.ndarray: Булев массив формы `(высота, ширина)`.
        """
        import numpy as np

        w, h = size
        mask = np.zeros((h, w), dtype=bool)
        for x0, y0, x1, y1 in boxes:
            xi0, yi0 = max(0, int(x0)), max(0, int(y0))
            xi1, yi1 = min(w, int(np.ceil(x1))), min(h, int(np.ceil(y1)))
            if xi1 > xi0 and yi1 > yi0:
                mask[yi0:yi1, xi0:xi1] = True
        return mask

    @staticmethod
    def compute_masked_metrics(
            img_a_path: Path, img_b_path: Path, text_boxes: list[tuple[float, float, float, float]],
            raster_composite_boxes: list[tuple[float, float, float, float]] = (), subblock_grid_size: int = 6
    ) -> dict[str, float | None]:
        """Считает MAE и SSIM раздельно по областям изображения.

        Области: внутри bbox текстовых узлов, внутри raster_composite и вне
        обеих (non-text; дополнительно делится на тёмную и светлую части по
        `LOW_LUMINANCE_THRESHOLD`). Раздельный расчёт нужен потому, что форма букв
        веб-шрифта-заменителя гарантированно отличается от оригинала, а
        растровая текстура raster_composite не совпадает попиксельно между
        Figma и Chromium. Это ограничения технологии, а не дефекты рендера,
        поэтому их нельзя проверять тем же порогом, что фон и композицию.

        Полная карта SSIM считается один раз, а по каждой области только усредняется.

        `ssim_diff_non_text_worst_subblock` — худший `ssim_diff` среди
        под-блоков регулярной сетки `subblock_grid_size` x `subblock_grid_size`,
        пересечённых с non-text-маской. Среднее по всей маске может «размазать»
        заметный, но небольшой по площади дефект; худший под-блок его не
        скрывает. Сетка чисто пространственная и не привязана к id или узлу.

        Args:
            img_a_path: Путь к отрисованному скриншоту.
            img_b_path: Путь к референсному изображению.
            text_boxes: bbox текстовых узлов (`collect_text_bboxes`).
            raster_composite_boxes: bbox raster_composite-узлов
                (`collect_raster_composite_bboxes`).
            subblock_grid_size: Число под-блоков сетки по каждой оси.

        Returns:
            dict[str, float | None]: Метрики с ключами `mae_*` и `ssim_diff_*`
            для областей `text`, `non_text`, `non_text_dark`, `non_text_bright`,
            `raster_composite`, а также `ssim_diff_non_text_worst_subblock`.
            Значение `None`, если область пуста; для SSIM — также если в ней
            меньше 49 пикселей (окно SSIM не помещается).
        """
        import numpy as np
        from skimage.metrics import structural_similarity as ssim

        im_a = Image.open(img_a_path).convert("RGB")
        im_b = Image.open(img_b_path).convert("RGB")
        w = min(im_a.width, im_b.width)
        h = min(im_a.height, im_b.height)
        im_a = im_a.crop((0, 0, w, h))
        im_b = im_b.crop((0, 0, w, h))
        arr_a_l = np.array(im_a.convert("L"))
        arr_b_l = np.array(im_b.convert("L"))
        diff_arr = np.abs(np.array(im_a, dtype=float) - np.array(im_b, dtype=float))
        text_mask = QAEngine.build_text_mask((w, h), text_boxes)
        raster_mask = QAEngine.build_text_mask((w, h), list(raster_composite_boxes))
        non_text_mask = ~text_mask & ~raster_mask
        dark_mask = arr_b_l.astype(float) < LOW_LUMINANCE_THRESHOLD
        non_text_dark_mask = non_text_mask & dark_mask
        non_text_bright_mask = non_text_mask & ~dark_mask

        # Дорогой вызов ssim() выполняется один раз до вложенных функций:
        # они лишь усредняют готовую карту по своей маске.
        _, full_ssim_map = ssim(arr_a_l, arr_b_l, full=True, gaussian_weights=True, sigma=1.5,
                                use_sample_covariance=False)

        def region_mae(mask) -> float | None:
            """Считает MAE (0..1) по маске.
            Args:
                mask: Булева маска области.
            Returns:
                float | None: MAE или `None`, если маска пуста.
            """
            if not mask.any():
                return None
            return round(float(diff_arr[mask].mean() / 255.0), 4)

        def region_ssim_value(mask) -> float | None:
            """Считает `ssim_diff` по маске по общей формуле для областей и под-блоков.
            Args:
                mask: Булева маска области.
            Returns:
                float | None: `1 - mean(SSIM)` или `None`, если в маске меньше 49 пикселей.
            """
            if mask.sum() < 49:
                return None
            return round(float(1 - full_ssim_map[mask].mean()), 4)

        def region_ssim_worst_subblock(mask, grid: int) -> float | None:
            """Ищет наибольший `ssim_diff` среди под-блоков сетки, пересечённых с маской.
            Блоки, где маска покрывает меньше 49 пикселей, пропускаются, а не
            считаются пройденными.
            Args:
                mask: Булева маска области.
                grid: Число под-блоков по каждой оси.
            Returns:
                float | None: Худший `ssim_diff` или `None`, если маска пуста.
            """

            if not mask.any():
                return None
            step_h = max(1, h // grid)
            step_w = max(1, w // grid)
            worst: float | None = None
            for gy in range(grid):
                y0 = gy * step_h
                y1 = h if gy == grid - 1 else (gy + 1) * step_h
                for gx in range(grid):
                    x0 = gx * step_w
                    x1 = w if gx == grid - 1 else (gx + 1) * step_w
                    block_mask = np.zeros_like(mask)
                    block_mask[y0:y1, x0:x1] = mask[y0:y1, x0:x1]
                    block_diff = region_ssim_value(block_mask)
                    if block_diff is None:
                        continue
                    if worst is None or block_diff > worst:
                        worst = block_diff
            return worst

        return {
            "mae_text": region_mae(text_mask),
            "mae_non_text": region_mae(non_text_mask),
            "mae_non_text_dark": region_mae(non_text_dark_mask),
            "mae_non_text_bright": region_mae(non_text_bright_mask),
            "ssim_diff_text": region_ssim_value(text_mask),
            "ssim_diff_non_text": region_ssim_value(non_text_mask),
            "ssim_diff_non_text_dark": region_ssim_value(non_text_dark_mask),
            "ssim_diff_non_text_bright": region_ssim_value(non_text_bright_mask),
            "ssim_diff_non_text_worst_subblock": region_ssim_worst_subblock(non_text_mask, subblock_grid_size),
            "mae_raster_composite": region_mae(raster_mask),
            "ssim_diff_raster_composite": region_ssim_value(raster_mask),
        }

    @staticmethod
    def check_structural_invariants(sections: list[ResolvedSectionSpec]) -> list[str]:
        """Быстро (без Playwright) проверяет структурные инварианты после переноса стиля.

        Перенос стиля не должен превращать заголовки в интерактивные
        компоненты и создавать вложенные `button` внутри `button`.

        Args:
            sections: Секции после переноса стиля.

        Returns:
            list[str]: Описания нарушений; пустой список, если всё в порядке.
        """
        issues: list[str] = []
        HEADING_ROLES = {"heading_h1", "heading_h2", "heading_h3"}

        def walk(node: IRNode, inside_button: bool, section_id: str):
            """Проверяет `node` на два нарушения: заголовок внутри кнопки и вложенный button.
            Args:
                node: Текущий узел.
                inside_button: `True`, если среди предков уже есть кнопка.
                section_id: Идентификатор секции (для сообщений).
            """
            is_button_here = node.component_role == "button"
            if is_button_here and node.type == "TEXT" and node.semantic_role in HEADING_ROLES:
                issues.append(f"[{section_id}] Заголовок '{node.characters}' попал внутрь button-компонента")
            if is_button_here and inside_button:
                issues.append(f"[{section_id}] Вложенный button внутри button: '{node.name}'")
            for ch in node.children:
                walk(ch, inside_button or is_button_here, section_id)

        for sec in sections:
            walk(sec.resolved_root, False, sec.id)
        return issues

    @staticmethod
    def check_asset_coverage(sections: list[ResolvedSectionSpec]) -> dict[str, Any]:
        """Проверяет, что у всех asset/raster_composite-узлов задан `asset_path`.

        Узел без пути к файлу означает «дыру» в рендере.

        Args:
            sections: Секции после переноса стиля.

        Returns:
            dict[str, Any]: Словарь с ключами `total`, `resolved`, `coverage`
            (доля 0..1; 1.0, если ассетов нет) и `missing` (не более 20 имён
            узлов без пути — для диагностики).
        """
        total, resolved, missing_names = 0, 0, []

        def walk(n: IRNode):
            """Считает asset-подобные узлы и собирает имена тех, у кого нет `asset_path`.
            Args:
                n: Текущий узел.
            """
            nonlocal total, resolved
            if n.render_strategy in ("asset", "raster_composite"):
                total += 1
                if n.asset_path:
                    resolved += 1
                else:
                    missing_names.append(n.name)
            for ch in n.children:
                walk(ch)

        for sec in sections:
            walk(sec.resolved_root)

        coverage = round(resolved / total, 3) if total else 1.0
        return {"total": total, "resolved": resolved, "coverage": coverage, "missing": missing_names[:20]}

    @classmethod
    async def run(cls, run_dir: Path, landing: LandingSpec, sections: list[ResolvedSectionSpec],
                  style_coherence_audit: dict[str, list[dict[str, Any]]] | None = None):
        """Запускает полный QA-прогон и сохраняет отчёт `qa_report.json` в `run_dir`.

        Этапы (помечены в теле метода как «Шаг N»):
          0. Предварительные проверки: есть ли секции и `reference_image`.
          1. Покрытие ассетов.
          2. Запуск Playwright (один экземпляр Chromium на все проверки).
          3. Extraction QA по каждой секции: IR fidelity, рендер оригинального
             дерева, MAE и SSIM против `reference_image`.
          4. Final Landing QA: страница `index.html` в каждом вьюпорте
             (overflow, пустое пространство, ошибки консоли, скриншот,
             advisory AI Visual QA).
          5. Сводка визуальных проверок.
          6. Release Gate: вычисление `release_ready`.
          7. Сохранение отчёта.

        Args:
            run_dir: Каталог текущего запуска; в нём лежат `index.html`, и
                сюда же сохраняются артефакты QA.
            landing: Собранная спецификация лендинга (дизайн-система, конфиг QA).
            sections: Секции после переноса стиля.
            style_coherence_audit: Результат `StyleTransferEngine.audit_retint_coverage`
                по секциям; включается в отчёт без изменений.

        Returns:
            dict[str, Any]: Полный QA-отчёт. Ключевые поля: `passed`,
            `structural_passed`, `runtime_passed`, `visual_checked`,
            `visual_passed`, `release_ready`, `extraction_qa`, `final_landing_qa`.
        """
        logger.info("\n--- ЗАПУСК ДВУХУРОВНЕВОГО QA (Playwright) ---")
        qa_config = landing.qa
        vr_cfg = PIPELINE_SETTINGS.get("visual_regression", {})
        ai_visual_qa_semaphore = asyncio.Semaphore(1)
        visual_regression_enforced = bool(vr_cfg.get("enforce", False))
        report = {
            "timestamp": datetime.datetime.now().isoformat(),
            "passed": True,
            "structural_passed": True,
            "runtime_passed": True,
            "visual_checked": False,
            "visual_passed": False,
            "release_ready": False,
            "threshold": vr_cfg.get("diff_threshold", qa_config.diff_threshold),
            "ssim_threshold": vr_cfg.get("ssim_threshold", qa_config.ssim_threshold),
            "style_coherence": style_coherence_audit,
            "visual_regression_enforced": visual_regression_enforced,
            "extraction_qa": {},
            "final_landing_qa": {},
        }

        # Шаг 0. Предварительные проверки: без секций и референсов QA не имеет смысла.
        if not sections:
            report["passed"] = False
            report["error"] = "Нет секций для QA."
            logger.error("❌ QA остановлен: список секций пуст.")
            qa_report_path = run_dir / "qa_report.json"
            qa_report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
            return report

        missing_reference_images = [sec.id for sec in sections if not sec.reference_image]
        if missing_reference_images:
            logger.error(f"❌ Для секций отсутствует reference_image: {missing_reference_images}")
            report["reference_images_missing"] = missing_reference_images
            # Без reference_image визуальный QA нельзя считать выполненным, поэтому это отказ.
            report["passed"] = False

        else:
            report["reference_images_missing"] = []

        # Шаг 1. Покрытие ассетов: узел без файла даст дыру в рендере.
        asset_coverage = cls.check_asset_coverage(sections)
        report["asset_coverage"] = asset_coverage
        if asset_coverage["total"] > 0 and asset_coverage["coverage"] < 1.0:
            logger.error(
                f"❌ Покрытие ассетов {asset_coverage['coverage'] * 100:.0f}% ({asset_coverage['resolved']}/{asset_coverage['total']}). Пропавшие: {asset_coverage['missing']}")
            report["passed"] = False

        # Шаг 2. Playwright: один Chromium на все проверки (Extraction QA и Final Landing QA),
        # чтобы не запускать браузер заново для каждой секции.
        try:
            async with async_playwright() as p:
                logger.info("🌐 [Playwright] Запуск браузера Chromium...")
                browser = await p.chromium.launch()
                logger.info("✓ Chromium успешно запущен.")

                # Шаг 3. Extraction QA: IR и рендер ОРИГИНАЛЬНОГО (дотрансферного) дерева
                # сверяются с reference_image. Проверяется, что извлечение из Figma и
                # базовый рендер корректны сами по себе.
                logger.info(f"Сверка эталонов Extraction QA для {len(sections)} секций...")
                for idx, sec in enumerate(sections, 1):
                    logger.info(f"  [{idx}/{len(sections)}] Проверка IR секции '{sec.name}'...")
                    fidelity_report = IRFidelityChecker.check(sec)
                    sec_passed = bool(fidelity_report["passed"])
                    issues = fidelity_report.get("issues", [])
                    warnings = fidelity_report.get("warnings", [])
                    if not sec_passed:
                        report["passed"] = False
                        report["structural_passed"] = False

                    logger.info(f"    IR Fidelity: {'PASSED ✅' if sec_passed else 'FAILED ❌'}")
                    for issue in issues:
                        logger.error(f"      - {issue}")

                    for warning in warnings:
                        logger.warning(f"      [warn] {warning}")

                    # Диагностический HTML сохраняется рядом с отчётом: по нему можно
                    # открыть в браузере ровно то, что было сфотографировано.
                    orig_html, bg_source_label = ReferenceRenderer.render_original_tree(sec,
                                                                                        design_system=landing.design_system)
                    orig_html_path = run_dir / f"orig_{sec.id}.html"
                    orig_html_path.write_text(orig_html, encoding="utf-8")

                    # Значения по умолчанию; переопределяются, если скриншот и метрики посчитаны.
                    mae: float | None = None
                    ssim_diff: float | None = None
                    masked: dict[str, float | None] = None
                    gate_results: dict[str, dict[str, Any]] | None = None
                    visual_status = "not_measured"
                    visual_reason = None
                    rendered_png = run_dir / f"extraction_{sec.id}.png"
                    diff_png = run_dir / f"diff_extraction_{sec.id}.png"

                    if not sec.reference_image:
                        visual_status = "not_measured"
                        visual_reason = "reference_image is not defined"
                        logger.error(f" [visual] [{sec.id}] reference_image отсутствует.")

                    else:
                        ref_png = PROJECT_ROOT / sec.reference_image
                        if not ref_png.exists():
                            visual_status = "reference_missing"
                            visual_reason = f"Reference image does not exist: {ref_png}"
                            logger.error(f" [visual] [{sec.id}] Файл референса отсутствует: {ref_png}")

                        else:
                            sec_w = max(int(sec.geometry.width or 1440), 1)
                            sec_h = max(int(sec.geometry.height or 900), 1)
                            vis_page = None
                            try:
                                # Размер вьюпорта равен размеру секции, чтобы скриншот
                                # совпал по размеру с reference_image.
                                vis_page = await browser.new_page(viewport={"width": sec_w, "height": sec_h})
                                original_url = orig_html_path.resolve().as_uri()
                                await vis_page.goto(original_url, wait_until="load")

                                # Ждём веб-шрифты: иначе первый кадр уйдёт с системным шрифтом.
                                await vis_page.evaluate(FONT_WAIT_SCRIPT)
                                await vis_page.evaluate(AUTOFIT_SCRIPT)

                                debug_fonts = await vis_page.evaluate("""
                                                                      () => {
                                                                          const results = [];
                                                                          for (const el of document.querySelectorAll('.dbg-text')) {
                                                                              const text = (el.textContent || '').trim();
                                                                              if (!text) continue;
                                                                              const computed = getComputedStyle(el).fontFamily;
                                                                              results.push({
                                                                                  text: text.slice(0, 40),
                                                                                  fontFamily: computed
                                                                              });
                                                                              if (results.length >= 5) break;
                                                                          }
                                                                          return results;
                                                                      }
                                                                      """)
                                logger.info(f"    [font-debug] [{sec.id}] Реально применённые шрифты: {debug_fonts}")

                                await vis_page.screenshot(path=rendered_png, full_page=False)

                                # Playwright в редких случаях завершается без ошибки, но не
                                # записывает файл (например, при обрыве I/O).
                                if not rendered_png.exists():
                                    raise RuntimeError("Playwright screenshot не был создан.")

                                # Размеры скриншота и референса обязаны совпадать, иначе
                                # метрики посчитаются по обрезанным или растянутым областям.
                                rendered_size = Image.open(rendered_png).size
                                reference_size = Image.open(ref_png).size
                                if rendered_size != reference_size:
                                    visual_status = "invalid_reference_size"
                                    visual_reason = f"Размер screenshot {rendered_size} не совпадает с размером reference {reference_size}."
                                    logger.error(f"    [visual] [{sec.id}] {visual_reason}")
                                else:
                                    # MAE и SSIM по всему изображению — только справочные:
                                    # в gate участвуют раздельные метрики ниже.
                                    mae = cls.compute_mae(rendered_png, ref_png, diff_png)
                                    ssim_diff = cls.compute_ssim(rendered_png, ref_png)
                                    text_boxes = collect_text_bboxes(sec)
                                    raster_boxes = collect_raster_composite_bboxes(sec)
                                    masked = cls.compute_masked_metrics(rendered_png, ref_png, text_boxes, raster_boxes)
                                    mae_threshold = float(vr_cfg.get("diff_threshold", qa_config.diff_threshold))
                                    ssim_threshold = float(vr_cfg.get("ssim_threshold", qa_config.ssim_threshold))
                                    mae_threshold_non_text = float(vr_cfg.get("mae_threshold_non_text", mae_threshold))
                                    ssim_threshold_non_text = float(
                                        vr_cfg.get("ssim_threshold_non_text", ssim_threshold))
                                    ssim_threshold_text = float(vr_cfg.get("ssim_threshold_text", 0.55))
                                    ssim_threshold_raster_composite = float(
                                        vr_cfg.get("ssim_threshold_raster_composite", 0.55))
                                    ssim_threshold_non_text_worst_subblock = float(
                                        vr_cfg.get("ssim_threshold_non_text_worst_subblock", 0.92))
                                    gate_checks = {
                                        "mae_non_text": (masked["mae_non_text"], mae_threshold_non_text),
                                        "ssim_diff_non_text": (masked["ssim_diff_non_text"], ssim_threshold_non_text),
                                        "ssim_diff_non_text_worst_subblock": (
                                            masked["ssim_diff_non_text_worst_subblock"],
                                            ssim_threshold_non_text_worst_subblock),
                                        "ssim_diff_text": (masked["ssim_diff_text"], ssim_threshold_text),
                                        "ssim_diff_raster_composite": (masked["ssim_diff_raster_composite"],
                                                                       ssim_threshold_raster_composite),
                                        "mae_raster_composite": (masked["mae_raster_composite"],
                                                                 mae_threshold_non_text),
                                    }
                                    gate_results = {name: {"value": value, "threshold": threshold,
                                                           "passed": (value is None or value <= threshold)} for
                                                    name, (value, threshold) in gate_checks.items()}
                                    vr_ok = all(g["passed"] for g in gate_results.values())
                                    if vr_ok:
                                        visual_status = "ok"
                                        visual_reason = "Visual regression passed."
                                    else:
                                        visual_status = "drift_enforced_fail" if visual_regression_enforced else "drift_diagnostic_only"
                                        failed_metrics = [n for n, g in gate_results.items() if not g["passed"]]
                                        visual_reason = f"Порог(и) превышены по: {', '.join(failed_metrics)}."

                                    gate_summary = "; ".join(
                                        f"{n}={g['value']}(порог {g['threshold']})" for n, g in gate_results.items())
                                    logger.info(
                                        f"    [visual] [справочно, НЕ участвует в gate] MAE={mae:.6f} SSIM_diff={ssim_diff:.6f} (полное изображение) | [gate] {gate_summary} -> status={visual_status}"
                                    )
                                    if not vr_ok and visual_regression_enforced:
                                        report["passed"] = False

                            except Exception as vis_err:
                                visual_status = "measurement_failed"
                                visual_reason = str(vis_err)
                                logger.exception(f"    [visual] [{sec.id}] Ошибка визуального QA.")

                                # При обязательной проверке visual regression сбой измерения
                                # считается провалом.
                                if visual_regression_enforced:
                                    report["passed"] = False

                            finally:
                                if vis_page is not None:
                                    try:
                                        await vis_page.close()
                                    except Exception:
                                        logger.warning(f" [visual] [{sec.id}] Не удалось закрыть Playwright page.")

                    report["extraction_qa"][sec.id] = {
                        "passed": sec_passed,
                        "issues": issues,
                        "warnings": warnings,
                        "visual_mae_informational_only": mae,
                        "visual_ssim_diff_informational_only": ssim_diff,
                        "visual_masked_metrics": masked,
                        "visual_gate_results": gate_results,
                        "visual_status": visual_status,
                        "visual_reason": visual_reason,
                        "reference_image": sec.reference_image,
                        "diagnostic_html": (f"orig_{sec.id}.html"),
                        "rendered_image": (rendered_png.name if rendered_png.exists() else None),
                        "diff_image": (diff_png.name if diff_png.exists() else None),
                        "reference_bg_source": (bg_source_label),
                    }

                # Шаг 4. Final Landing QA: собранная страница index.html открывается в каждом
                # вьюпорте (desktop, tablet, mobile) и проверяется на адаптивность, ошибки
                # консоли и пустое пространство; скриншот идёт в advisory AI Visual QA.
                vps = qa_config.viewports
                logger.info(f"Тестирование адаптивности Final Landing во всех вьюпортах ({len(vps)} шт.)...")
                for idx, vp in enumerate(vps, 1):
                    logger.info(f"  [{idx}/{len(vps)}] Вьюпорт '{vp.name}' ({vp.width}x{vp.height}px)...")
                    vp_size: ViewportSize = {"width": vp.width, "height": vp.height}
                    page = None
                    console_errors: list[str] = []
                    failed_requests: list[str] = []
                    vp_passed = True
                    ai_visual_gate_passed = True
                    ai_visual_gate_reason = "not evaluated"
                    try:
                        page = await browser.new_page(viewport=vp_size)
                        page.on("console", lambda message: (
                            console_errors.append(message.text) if message.type == "error" else None))
                        page.on("pageerror", lambda error: (console_errors.append(str(error))))
                        page.on("requestfailed",
                                lambda request: failed_requests.append(f"{request.url} - {request.failure}"))
                        index_path = run_dir / "index.html"
                        index_url = index_path.resolve().as_uri()
                        await page.goto(index_url, wait_until="load")
                        await page.evaluate(FONT_WAIT_SCRIPT)
                        await page.evaluate(AUTOFIT_SCRIPT)

                        # Элементы, вылезающие за границы вьюпорта по горизонтали, — признак того,
                        # что адаптивная секция не сжалась на узком экране.
                        overflow_report = await page.evaluate("""
                                                              () => {
                                                                  const viewportWidth = window.innerWidth;

                                                                  const offenders = [];

                                                                  for (const el of document.querySelectorAll('*')) {
                                                                      const rect = el.getBoundingClientRect();

                                                                      const outsideLeft = rect.left < -1;
                                                                      const outsideRight = rect.right > viewportWidth + 1;

                                                                      if (outsideLeft || outsideRight) {
                                                                          offenders.push({
                                                                              tag: el.tagName,
                                                                              id: el.id || null,
                                                                              className:
                                                                                  typeof el.className ===
                                                                                  'string'
                                                                                      ? el.className
                                                                                      : null,
                                                                              left: rect.left,
                                                                              right: rect.right,
                                                                              width: rect.width
                                                                          });
                                                                      }
                                                                  }

                                                                  return {
                                                                      overflow: offenders.length > 0,
                                                                      offenders: offenders.slice(0, 20)
                                                                  };
                                                              }
                                                              """)

                        overflow = bool(overflow_report["overflow"])

                        # Пустое пространство. Проверка универсальна: опирается только на
                        # структурные классы `.landing-section`, `.section-content`,
                        # `.fixed-content-scaler`, которые есть у любой секции. Ловит дефект,
                        # который overflow не видит: содержимое уменьшено через
                        # `transform: scale()`, а контейнер секции — нет, и внутри остаётся
                        # пустая область (см. `min-height` в `render_page`).
                        empty_space_report = await page.evaluate("""
                                                                 () => {
                                                                     const offenders = [];
                                                                     for (
                                                                         const section
                                                                         of document.querySelectorAll('.landing-section')
                                                                         ) {
                                                                         const rect = section.getBoundingClientRect();
                                                                         const content = section.querySelector(
                                                                             '.section-content, .fixed-content-scaler'
                                                                         );
                                                                         if (!content || rect.height <= 0) continue;

                                                                         const contentRect = content.getBoundingClientRect();
                                                                         const emptyRatio = 1 - (contentRect.height / rect.height);

                                                                         if (emptyRatio > 0.15) {
                                                                             offenders.push({
                                                                                 id: section.id || null,
                                                                                 sectionHeight: rect.height,
                                                                                 contentHeight: contentRect.height,
                                                                                 emptyRatio: Math.round(emptyRatio * 100) / 100
                                                                             });
                                                                         }
                                                                     }
                                                                     return {
                                                                         has_empty_space: offenders.length > 0,
                                                                         offenders: offenders
                                                                     };
                                                                 }
                                                                 """)

                        has_empty_space = bool(empty_space_report["has_empty_space"])

                        # Скриншот сохраняется как артефакт QA; для целевого вьюпорта
                        # (`visual_qa_ai.target_viewport`) он же идёт на вход AI Visual QA.
                        shot_path = run_dir / f"final_view_{vp.name}.png"
                        await page.screenshot(path=shot_path, full_page=True)

                        # Advisory-проверка vision-моделью: ищет элементы стиля секции-получателя,
                        # «просочившиеся» туда, где должен быть стиль дизайн-системы. По умолчанию
                        # не блокирует release_ready. При visual_qa_ai.enforce=true провал gate ставит
                        # vp_passed=False и через runtime_passed блокирует релиз.
                        ai_visual_review: dict[str, Any] | None = None
                        ai_qa_cfg = PIPELINE_SETTINGS.get("visual_qa_ai", {})
                        if bool(ai_qa_cfg.get("enabled", False)) and vp.name == ai_qa_cfg.get("target_viewport",
                                                                                              "desktop"):
                            style_node_id = landing.design_system.source.node_id
                            style_ref_fname = f"style_ref_{hashlib.md5(style_node_id.encode()).hexdigest()[:8]}.png"
                            style_ref_path = ASSETS_DIR / style_ref_fname
                            composition_ref_paths = [PROJECT_ROOT / s.reference_image for s in sections if
                                                     s.reference_image and (PROJECT_ROOT / s.reference_image).exists()]
                            if style_ref_path.exists() and composition_ref_paths:
                                try:
                                    ai_visual_review = await AIVisualQAReviewer.review(
                                        final_screenshot=shot_path,
                                        reference_style_source=style_ref_path,
                                        reference_composition_sources=composition_ref_paths,
                                        composition_authority=", ".join(s.id for s in sections),
                                        visual_authority=str(landing.design_system.source.resource_id),
                                        llm_cfg=ai_qa_cfg,
                                        semaphore=ai_visual_qa_semaphore,
                                    )
                                except Exception as ai_err:
                                    logger.warning(f"  [AI Visual QA] Ошибка ревью на вьюпорте '{vp.name}': {ai_err}")
                                    ai_visual_review = {"status": "review_failed", "reason": str(ai_err)}
                            else:
                                logger.info(
                                    f"  [AI Visual QA] Пропущено на вьюпорте '{vp.name}' — нет референсных изображений на диске.")

                        ai_visual_gate_passed, ai_visual_gate_reason = evaluate_ai_visual_gate(ai_visual_review,
                                                                                               ai_qa_cfg)
                        if not ai_visual_gate_passed:
                            logger.warning(
                                f"  [AI Visual QA] {'⛔ ЗАБЛОКИРОВАН' if bool(ai_qa_cfg.get('enforce', False)) else '⚠️ не пройден (informational, enforce=False)'} гейт на вьюпорте '{vp.name}': {ai_visual_gate_reason}"
                            )
                            if bool(ai_qa_cfg.get("enforce", False)):
                                vp_passed = False

                        # Вьюпорт не пройден, если сработал любой из включённых в конфиге QA признаков отказа.
                        if qa_config.fail_on_console_error and (console_errors or failed_requests):
                            vp_passed = False

                        if qa_config.fail_on_overflow and overflow:
                            vp_passed = False

                        if qa_config.fail_on_empty_space and has_empty_space:
                            vp_passed = False

                        if not vp_passed:
                            report["passed"] = False
                            report["runtime_passed"] = False

                        logger.info(
                            f"    Результат: overflow={overflow}, console_errors={len(console_errors)}, failed_requests={len(failed_requests)} -> {'PASSED ✅' if vp_passed else 'FAILED ❌'}")
                        report["final_landing_qa"][vp.name] = {
                            "width": vp.width,
                            "height": vp.height,
                            "overflow": overflow,
                            "overflow_offenders": (overflow_report["offenders"]),
                            "has_empty_space": has_empty_space,
                            "empty_space_offenders": (empty_space_report["offenders"]),
                            "console_errors": (console_errors),
                            "failed_requests": (failed_requests),
                            "screenshot": (shot_path.name),
                            "passed": vp_passed,
                            "ai_visual_review": ai_visual_review,
                            "ai_visual_gate_passed": ai_visual_gate_passed,
                            "ai_visual_gate_reason": ai_visual_gate_reason,
                        }

                    except Exception as runtime_err:
                        vp_passed = False
                        report["passed"] = False
                        report["runtime_passed"] = False
                        logger.exception(f"    ❌ Runtime QA '{vp.name}' завершился исключением.")
                        report["final_landing_qa"][vp.name] = {
                            "width": vp.width,
                            "height": vp.height,
                            "overflow": None,
                            "overflow_offenders": [],
                            "console_errors": (console_errors),
                            "failed_requests": (failed_requests),
                            "screenshot": None,
                            "passed": False,
                            "ai_visual_review": None,
                            "ai_visual_gate_passed": ai_visual_gate_passed,
                            "ai_visual_gate_reason": ai_visual_gate_reason,
                            "error": str(runtime_err),
                        }

                    finally:
                        if page is not None:
                            try:
                                await page.close()
                            except Exception:
                                logger.warning(f"Не удалось закрыть viewport page '{vp.name}'.")

                # Ошибка закрытия браузера только логируется и не роняет весь QA-прогон.
                try:
                    await browser.close()
                except Exception:
                    logger.warning("Не удалось корректно закрыть Chromium.")

        except Exception as inner_err:
            err_str = str(inner_err)
            logger.exception(f"❌ Критическая ошибка при работе с Playwright: {err_str}")
            logger.critical("Если Playwright не находит браузер, выполните: playwright install chromium")
            report["passed"] = False
            report["runtime_passed"] = False
            report["error"] = err_str

        # Шаг 5. Сводка визуальных проверок: visual_checked — сравнение выполнилось для всех
        # секций без технических сбоев; visual_passed — там, где выполнилось, результат приемлем.
        visual_statuses = {sec_id: result.get("visual_status") for sec_id, result in report["extraction_qa"].items()}
        visual_checked = bool(visual_statuses) and all(
            status not in {"not_measured", "reference_missing", "measurement_failed", "invalid_reference_size"} for
            status in visual_statuses.values())

        def _status_is_acceptable(status: str | None, enforced: bool) -> bool:
            """Проверяет, приемлем ли `visual_status` секции для релиза.

            `ok` приемлем всегда. `drift_diagnostic_only` (порог не пройден, но
            не обязателен, `enforce=False`) приемлем: это осознанное решение
            команды. SSIM на тёмном декоративном контенте ненадёжен, поэтому
            решение о релизе принимает человек по diff-картинкам и
            `final_view_*.png`, а MAE и SSIM продолжают считаться и
            сохраняться в `qa_report.json`. Остальные статусы
            (`reference_missing`, `measurement_failed`, `invalid_reference_size`)
            блокируют всегда: это технические сбои, а не «порог не дотянут».

            Args:
                status: Значение `visual_status` секции из `extraction_qa`.
                enforced: Значение `visual_regression_enforced` из конфига.

            Returns:
                bool: `True`, если статус допустим для `release_ready`.
            """
            if status == "ok":
                return True
            if status == "drift_diagnostic_only" and not enforced:
                return True
            return False

        visual_passed = bool(visual_statuses) and all(
            _status_is_acceptable(status, visual_regression_enforced) for status in visual_statuses.values())
        unverified_sections = [sec_id for sec_id, status in visual_statuses.items() if status != "ok"]
        report["visual_checked"] = visual_checked
        report["visual_passed"] = visual_passed
        report["visual_fidelity_verified"] = visual_passed
        report["visual_fidelity_unverified_sections"] = unverified_sections

        # Шаг 6. Release Gate: structural и runtime пересчитываются как AND по всем секциям и
        # вьюпортам (накопленных флагов недостаточно), а релиз требует чистых трёх уровней сразу.
        structural_passed = (
                report["structural_passed"]
                and not report.get("reference_images_missing")
                and (report["asset_coverage"]["total"] == 0 or report["asset_coverage"]["coverage"] >= 1.0)
                and all(result.get("passed", False) for result in report["extraction_qa"].values())
        )
        runtime_passed = report["runtime_passed"] and all(
            result.get("passed", False) for result in report["final_landing_qa"].values())
        report["structural_passed"] = structural_passed
        report["runtime_passed"] = runtime_passed
        report["release_ready"] = structural_passed and runtime_passed and visual_checked and visual_passed
        if not visual_checked:
            logger.error(f"❌ Визуальная проверка не выполнена полностью. Секции: {unverified_sections}")

        elif not visual_passed:
            logger.warning(f"⚠️ Визуальное соответствие не подтверждено. Секции: {unverified_sections}")

        if not report["release_ready"]:
            report["passed"] = False

        # release_ready считается только из structural, runtime и числового visual-слоёв: AI Visual QA
        # влияет на release_ready только косвенно: при enforce=true провал gate роняет runtime_passed,
        # при enforce=false слой чисто advisory. Флаги ниже показывают в отчёте, выполнился ли
        # AI Visual QA (например, он мог не отработать из-за rate limit у всех моделей), чтобы
        # «PASSED» не создавал ложного впечатления, что semantic/style-coherence слой тоже проверен.
        ai_visual_target_vp = PIPELINE_SETTINGS.get("visual_qa_ai", {}).get("target_viewport", "desktop")
        ai_review_target = (report["final_landing_qa"].get(ai_visual_target_vp) or {}).get("ai_visual_review")
        ai_visual_reviewed = bool(ai_review_target) and ai_review_target.get("status") not in (None, "not_reviewed",
                                                                                               "review_failed")
        report["ai_visual_reviewed"] = ai_visual_reviewed

        # `ai_visual_reviewed=True` означает лишь «ревьюер технически отработал», а не «результат
        # чистый». Отдельный флаг делает видимым сам факт находок (status == "fail").
        ai_visual_has_findings = ai_visual_reviewed and ai_review_target.get("status") == "fail"
        report["ai_visual_has_unresolved_findings"] = ai_visual_has_findings

        # Шаг 7. Сохранение отчёта и итоговая сводка в лог.
        qa_report_path = run_dir / "qa_report.json"
        qa_report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        logger.info(f"📋 Полный отчет QA сохранен -> {qa_report_path.resolve()}")
        logger.info(
            f"QA: structural={structural_passed}, runtime={runtime_passed}, visual_checked={visual_checked}, visual_passed={visual_passed}, release_ready={report['release_ready']}, ai_visual_reviewed={ai_visual_reviewed}"
        )
        verdict_text = "PASSED ✅" if report["passed"] else "FAILED ❌"
        if report["passed"] and not ai_visual_reviewed:
            verdict_text += (
                " (⚠️ БЕЗ подтверждения AI Visual QA — semantic/style-coherence "
                "слой не отработал в этом прогоне, см. ai_visual_review.status "
                f"на вьюпорте '{ai_visual_target_vp}' в qa_report.json)"
            )
        elif report["passed"] and ai_visual_has_findings:
            finding_count = len(ai_review_target.get("foreign_style_elements", []) or [])
            verdict_text += (
                f" (⚠️ AI Visual QA нашёл {finding_count} потенциальных "
                f"нарушений Design Contract на вьюпорте '{ai_visual_target_vp}' "
                f"(confidence={ai_review_target.get('confidence')}) — gate пока "
                "advisory/enforce=false, поэтому release_ready это НЕ блокирует, "
                "но НЕЛЬЗЯ предъявлять заказчику как безусловный PASSED без "
                "ручного просмотра qa_report.json → ai_visual_review)"
            )

        logger.info(f"🏆 Итоговый вердикт QA: {verdict_text}")
        return report


# ---------------------------------------------------------------------------
# Основной pipeline
# ---------------------------------------------------------------------------
async def main():
    """Точка входа CLI: собирает лендинг и проверяет его QA.

    Запуск: `python -m scripts.generate_landing [--config <манифест>]`.
    Загружает манифест и spec, затем выполняет три этапа:
      1. Семантический и компонентный анализ (`SemanticResolver`, `ComponentResolver`).
      2. Перенос стиля (`StyleTransferEngine.transfer`) и защитные проверки:
         `reference_image` не потерян, вложенных кнопок нет (при необходимости
         они авто-исправляются).
      3. Рендер (`WebRenderer.render_page`), сохранение в `output/run_<timestamp>/`
         и QA (`QAEngine.run`).
    После QA удаляются старые каталоги `run_*` сверх лимита `keep_output_runs`.

    Raises:
        FileNotFoundError: Если spec, собранный `figma_extractor.py`, не найден.
        RuntimeError: Если LLM обязателен, но недоступен; если авто-починка не
            устранила нарушения структуры; если `StyleTransferEngine` изменил
            `reference_image`; если сборка не прошла Quality Gate
            (`release_ready=False`).
    """
    t_start = time.perf_counter()
    logger.info("=" * 60)
    logger.info("🚀 СТАРТ ГЕНЕРАЦИИ ЛЕНДИНГА")
    logger.info("=" * 60)
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="configs/landing_manifest.json", help="Манифест лендинга")
    args = parser.parse_args()
    manifest_path = PROJECT_ROOT / args.config
    logger.info(f"Загрузка манифеста: {manifest_path.name}...")
    manifest = ConfigLoader.load(manifest_path)

    # Список бесплатных моделей провайдера — только справочный вывод в лог: модели для
    # классификации берутся из манифеста, а не отсюда.
    llm_cfg = manifest.get("llm", {})
    if llm_cfg:
        keys = LLMFactory.get_keys(llm_cfg)
        if keys:
            base_url = llm_cfg.get("base_url", "")
            free_models = get_free_models(base_url, keys[0])
            if free_models:
                logger.info(f"📋 Доступно бесплатных моделей: {len(free_models)}")
                for model_id in free_models[:20]:
                    logger.info(f"  → {model_id}")

            else:
                logger.info("📋 Не удалось получить список бесплатных моделей (или их нет)")

    llm_concurrency = int(manifest.get("llm", {}).get("concurrency", 1))
    llm_concurrency = max(llm_concurrency, 1)
    llm_semaphore = asyncio.Semaphore(llm_concurrency)

    # Spec (IR-дерево и style_spec) заранее собирает `figma_extractor.py`.
    spec_path = SPECS_DIR / f"{manifest_path.stem}_spec.json"
    if not spec_path.exists():
        logger.critical(f"Файл спецификации {spec_path} не найден!")
        logger.critical(f"Сначала запустите: python scripts/figma_extractor.py --config {args.config}")
        raise FileNotFoundError(f"Файл spec не найден: {spec_path}")

    # Предупреждение об устаревшем spec: если код экстрактора или моделей изменён после
    # сборки spec, в нём остаются старые данные IR.
    extractor_files = [PROJECT_ROOT / "scripts" / "figma_extractor.py", PROJECT_ROOT / "scripts" / "models.py"]
    extractor_mtimes = [f.stat().st_mtime for f in extractor_files if f.exists()]
    if extractor_mtimes and spec_path.stat().st_mtime < max(extractor_mtimes):
        newest_file = max((f for f in extractor_files if f.exists()), key=lambda f: f.stat().st_mtime)
        logger.warning(
            f"⚠️ {spec_path.name} старее, чем {newest_file.name}. Похоже, вы поменяли код экстрактора (логику normalize_node, новые поля IR и т.п.), но не пересобрали spec — запустите заново `python -m scripts.figma_extractor` перед этим прогоном, иначе будут использованы устаревшие данные IR."
        )

    logger.info(f"Загрузка спецификации: {spec_path.name}...")
    spec_data = ConfigLoader.load(spec_path)
    ds_spec = DesignSystemSpec.model_validate(spec_data["style_spec"])
    validate_color_tokens(ds_spec, RENDER_RULES)
    raw_sections = [SectionSpec.model_validate(section) for section in spec_data["sections"]]

    # reference_image проверяется до семантики, чтобы проблема не потерялась внутри pipeline.
    missing_spec_references = [section.id for section in raw_sections if not section.reference_image]
    # Здесь только логируем: итоговый отказ фиксирует QAEngine.run (reference_images_missing → release_ready=False).
    if missing_spec_references:
        logger.error(f"❌ В исходной spec отсутствуют reference_image у секций: {missing_spec_references}")

    else:
        logger.info(f"✓ Reference images присутствуют для всех {len(raw_sections)} секций.")

    # Размер h1 дизайн-системы — опорная точка типографических правил
    # (заголовки и подписи определяются как доля от него).
    h1_size = ds_spec.tokens.typography.get("h1", TypographyToken(family="Inter", size=48, weight=700)).size

    # Этап 1. Семантический и компонентный анализ: секции обрабатываются параллельно.
    logger.info("\n--- ЭТАП 1/3: СЕМАНТИЧЕСКИЙ И КОМПОНЕНТНЫЙ АНАЛИЗ ---")
    results = await asyncio.gather(
        *[process_one_section(section, h1_size, manifest.get("llm", {}), llm_semaphore) for section in raw_sections])
    telemetry = {"total_nodes": 0, "rules_resolved": 0, "llm_resolved": 0, "fallback": 0}
    processed_sections = []
    for section, local_tel in results:
        processed_sections.append(section)
        for key in telemetry:
            telemetry[key] += local_tel.get(key, 0)

    raw_sections = processed_sections
    logger.info(f"📊 Итог телеметрии семантики: {telemetry}")

    # Этап 2. Перенос стиля дизайн-системы на каждую секцию.
    logger.info("\n--- ЭТАП 2/3: STYLE TRANSFER ---")
    resolved_sections = [StyleTransferEngine.transfer(section, ds_spec) for section in raw_sections]

    # Аудит перекраски попадает в qa_report.json как `style_coherence`, чтобы расхождения
    # цвета были видны в отчёте, а не только на скриншоте.
    style_coherence_audit: dict[str, list[dict[str, Any]]] = {}
    for src, resolved in zip(raw_sections, resolved_sections):
        target_primary = ds_spec.tokens.colors.get("primary")
        style_coherence_audit[resolved.id] = StyleTransferEngine.audit_retint_coverage(resolved.resolved_root,
                                                                                       str(target_primary.value) if target_primary else None)

    # Защита: перенос стиля не должен терять или менять reference_image секции.
    for source_section, resolved_section in zip(raw_sections, resolved_sections):
        if source_section.reference_image and not resolved_section.reference_image:
            raise RuntimeError(
                f"КРИТИЧЕСКАЯ ОШИБКА: StyleTransferEngine потерял reference_image секции '{source_section.id}'. Исходный reference_image: {source_section.reference_image}")

        if source_section.reference_image and (resolved_section.reference_image != source_section.reference_image):
            raise RuntimeError(
                f"КРИТИЧЕСКАЯ ОШИБКА: reference_image секции '{source_section.id}' изменился после StyleTransfer.\nДо: {source_section.reference_image}\nПосле: {resolved_section.reference_image}"
            )

    structural_issues = QAEngine.check_structural_invariants(resolved_sections)
    if structural_issues:
        for issue in structural_issues:
            logger.warning(f"⚠️ [STRUCTURAL QA] {issue} — авто-починка...")

        repaired = auto_repair_nested_buttons(resolved_sections)
        for repair in repaired:
            logger.info(f"  ✓ {repair}")

        remaining = QAEngine.check_structural_invariants(resolved_sections)
        if remaining:
            raise RuntimeError(f"Авто-починка не устранила все нарушения: {remaining}")

    # Итоговая LandingSpec — единый объект для рендера и QAEngine.run().
    qa_cfg = QAConfig.model_validate(manifest.get("qa", {}))
    landing = LandingSpec(project_name=manifest.get("project_name", "PoC Landing"), design_system=ds_spec,
                          sections=resolved_sections, render=RenderConfig(**manifest.get("render", {})), qa=qa_cfg)

    # Этап 3. Рендер единого HTML и сохранение в новый каталог запуска.
    logger.info("\n--- ЭТАП 3/3: РЕНДЕРИНГ И СОХРАНЕНИЕ ---")
    html_page = WebRenderer.render_page(landing)
    run_dir = OUTPUT_BASE / f"run_{datetime.datetime.now():%Y%m%d_%H%M%S}"
    run_dir.mkdir(parents=True, exist_ok=True)
    index_path = run_dir / "index.html"
    index_path.write_text(html_page, encoding="utf-8")
    logger.info(f"✓ Финальный HTML успешно сгенерирован: {index_path.resolve()}")

    qa_report = await QAEngine.run(run_dir, landing, resolved_sections, style_coherence_audit)

    # Оставляем только `keep_output_runs` самых свежих каталогов run_*: иначе output/
    # бесконечно растёт с каждым запуском.
    keep_n = int(PIPELINE_SETTINGS.get("cache_retention", {}).get("keep_output_runs", 10))
    all_runs = sorted(OUTPUT_BASE.glob("run_*"), key=lambda directory: (directory.stat().st_mtime), reverse=True)
    for stale_run in all_runs[keep_n:]:
        shutil.rmtree(stale_run, ignore_errors=True)

    # Итог: при release_ready=True сборка успешна; иначе RuntimeError, чтобы CI и
    # вызывающий код узнали о провале Quality Gate.
    total_time = time.perf_counter() - t_start
    if qa_report.get("release_ready", False):
        logger.info("=" * 60)
        logger.info(f"✅ СБОРКА И QA ЗАВЕРШЕНЫ УСПЕШНО ЗА {total_time:.2f} сек.")
        logger.info(f"📁 Папка с артефактами: {run_dir.resolve()}")
        logger.info("=" * 60)
        return

    logger.error("=" * 60)
    logger.error(f"❌ СБОРКА НЕ ПРОШЛА QUALITY GATE ЗА {total_time:.2f} сек.")
    logger.error(f"📁 Артефакты QA: {run_dir.resolve()}")
    logger.error(f"Structural: {qa_report.get('structural_passed')}")
    logger.error(f"Runtime: {qa_report.get('runtime_passed')}")
    logger.error(f"Visual checked: {qa_report.get('visual_checked')}")
    logger.error(f"Visual passed: {qa_report.get('visual_passed')}")
    logger.error(f"Release ready: {qa_report.get('release_ready')}")
    logger.info("=" * 60)
    raise RuntimeError(f"Landing не прошёл Quality Gate. QA report: {run_dir / 'qa_report.json'}")


if __name__ == "__main__":
    asyncio.run(main())
