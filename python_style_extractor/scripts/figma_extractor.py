# python_style_extractor/scripts/figma_extractor.py

"""Извлечение данных из Figma и сборка spec для pipeline «Figma → Landing».

Модуль забирает из Figma API два источника (дизайн-систему и композиции секций),
нормализует их в IR-дерево (`models.py`) и сохраняет результат в spec-файл,
который затем читает `generate_landing.py`.

Стадии:

    get_nodes -> normalize_node -> classify_and_mark
              -> StyleBuilder / SectionBuilder -> spec.json

    1. Дизайн-система: узел `style_source` нормализуется, `StyleBuilder` строит
       из него токены цвета, типографики и радиусов.
    2. Секции: для каждой секции манифеста `SectionBuilder` нормализует дерево,
       размечает ассеты, скачивает SVG/PNG и референсное изображение секции.

Входные данные:
    * манифест лендинга (аргумент `--config`, по умолчанию `configs/landing_manifest.json`);
    * правила из `configs/defaults.json`, `analysis_rules.json`, `pipeline_settings.json`;
    * файл `.env` в корне проекта: `FIGMA_TOKENS` — список токенов Figma API.

Результат:
    * `<имя манифеста>_spec.json` в `SPECS_DIR`;
    * ассеты и референсы в `ASSETS_DIR`, индекс ассетов в `ASSET_MANIFEST_PATH`;
    * кэш ответов Figma API в `RAW_DIR` (`--force` обновляет только его).

Запуск:
    python -m scripts.figma_extractor --config configs/landing_manifest.json [--force]
"""

import argparse
import hashlib
import json
import logging
import math
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Literal

import requests
from environs import Env

from scripts.color_resolution import (
    sample_dominant_colors_from_png,
    passes_bg_reliability_filter,
    bg_reliability_weight, collect_background_candidates,
    alpha_from_rgba, color_distance_hls,
)
from scripts.component_heuristics import ComponentHeuristics
from scripts.models import (
    Geometry,
    IRLayout,
    IRStyle,
    TypographyToken,
    ResolvedToken,
    DesignTokens,
    ComponentVariant,
    DesignSystemSpec,
    IRNode,
    SourceRef,
    TransformSpec,
    SectionSpec,
    Asset,
    GradientData,
    GradientStop, ImageFillSpec,
)
from scripts.paths import (
    PROJECT_ROOT, CONFIGS_DIR, RAW_DIR, SPECS_DIR, ASSETS_DIR, ASSET_MANIFEST_PATH, TOKEN_COOLDOWN_PATH
)
from scripts.utils import normalize_figma_id, ConfigLoader, mask_secret

# Формат логирования с временными метками для контроля прогресса
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S"
)
logger = logging.getLogger("figma_extractor")


def load_asset_manifest() -> dict[str, str]:
    """Загружает индекс скачанных ассетов.

    Ключ записи — `<file_key>::<node_id>`, значение — путь к файлу относительно
    корня проекта. Записи со старыми ключами без `::` отбрасываются: по ним
    нельзя отличить ассеты из разных Figma-файлов. Функция их не мигрирует:
    запись в новом формате появится при следующем запуске, когда
    `SectionBuilder._process_asset` не найдёт ассет в индексе (для старых
    записей это любой запуск). Ассет при этом скачивается заново.

    Returns:
        dict[str, str]: Индекс `{ключ: путь}`. Пустой словарь, если файла нет,
        он повреждён или его содержимое не словарь.
    """
    if not ASSET_MANIFEST_PATH.exists():
        return {}

    try:
        raw = json.loads(
            ASSET_MANIFEST_PATH.read_text(encoding="utf-8")
        )
    except (json.JSONDecodeError, OSError):
        return {}

    if not isinstance(raw, dict):
        return {}

    return {
        str(key): str(value)
        for key, value in raw.items()
        if isinstance(key, str)
           and "::" in key
           and isinstance(value, str)
    }


def save_asset_manifest(manifest: dict[str, str]) -> None:
    """Сохраняет индекс ассетов в `ASSET_MANIFEST_PATH`.

    Родительский каталог создаётся при необходимости, ключи сортируются.

    Args:
        manifest: Индекс `{"<file_key>::<node_id>": "<путь>"}`.
    """
    ASSET_MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True, )
    ASSET_MANIFEST_PATH.write_text(json.dumps(manifest, indent=2, ensure_ascii=False, sort_keys=True, ),
                                   encoding="utf-8", )


def _manifest_key(file_key: str, node_id: str) -> str:
    """Формирует ключ ассета в индексе: `<file_key>::<node_id>`.

    `node_id` уникален только внутри одного Figma-файла, поэтому `file_key`
    входит в ключ обязательно.

    Args:
        file_key: Ключ Figma-файла.
        node_id: Идентификатор узла внутри файла.

    Returns:
        str: Ключ вида `<file_key>::<node_id>`.
    """
    return f"{file_key}::{node_id}"


env = Env()
env.read_env(PROJECT_ROOT / ".env")

try:
    DEFAULTS = ConfigLoader.load(CONFIGS_DIR / "defaults.json")
    ANALYSIS_RULES = ConfigLoader.load(CONFIGS_DIR / "analysis_rules.json")
    PIPELINE_SETTINGS = ConfigLoader.load(CONFIGS_DIR / "pipeline_settings.json")
except Exception as e:
    logger.critical(f"Не удалось инициализировать базовые конфигурации: {e}")
    raise

# --- Константы для normalize_node: вынесены на уровень модуля, чтобы не пересоздавать
# словари на каждый рекурсивный вызов и чтобы typechecker видел точный Literal-тип
# возвращаемого значения (а не widen'ил его до str).
ALIGN_MAP: dict[str, str] = {
    "MIN": "flex-start", "CENTER": "center", "MAX": "flex-end", "SPACE_BETWEEN": "space-between"
}
H_CONSTRAINT_MAP: dict[str, Literal["left", "right", "center", "scale", "stretch"]] = {
    "LEFT": "left", "RIGHT": "right", "CENTER": "center", "SCALE": "scale", "STRETCH": "stretch"
}
V_CONSTRAINT_MAP: dict[str, Literal["top", "bottom", "center", "scale", "stretch"]] = {
    "TOP": "top", "BOTTOM": "bottom", "CENTER": "center", "SCALE": "scale", "STRETCH": "stretch"
}
TEXT_ALIGN_MAP: dict[str, str] = {
    "LEFT": "left",
    "CENTER": "center",
    "RIGHT": "right",
    "JUSTIFIED": "justify",
}


class FigmaClient:
    """Клиент Figma REST API с ротацией токенов, кэшем ответов и повторами.

    Токен, получивший ответ 429, уходит в кулдаун; значение сохраняется в
    `TOKEN_COOLDOWN_PATH` и переживает перезапуск процесса. Ответы `get_nodes`
    кэшируются в `RAW_DIR`.
    """

    def __init__(self, tokens: list[str], config: dict[str, Any], pipeline_settings: dict[str, Any]) -> None:
        """Читает настройки и сохранённые кулдауны токенов.

        Args:
            tokens: Токены Figma API (`FIGMA_TOKENS` из `.env`).
            config: Содержимое `defaults.json`; используется раздел `figma`
                (`api_base_url`, `timeout_seconds`, `max_retries`, `backoff_factor`).
            pipeline_settings: Содержимое `pipeline_settings.json`; используются
                `figma_client` (`max_cooldown_wait_seconds`,
                `default_retry_after_seconds`) и `cache_retention`
                (`keep_figma_cache_versions`).

        Raises:
            ValueError: Если список токенов пуст.
        """
        if not tokens:
            logger.critical("В файле .env отсутствует список FIGMA_TOKENS!")
            raise ValueError("FIGMA_TOKENS не найдены в .env файле!")

        self.raw_tokens = list(tokens)
        self.tokens_count = len(self.raw_tokens)
        self.pipeline_settings_full = pipeline_settings
        self.pipeline_settings = pipeline_settings.get("figma_client", {})

        # Персистентный кулдаун токенов — переживает перезапуск процесса
        self._cooldown_path = TOKEN_COOLDOWN_PATH
        saved: dict[str, float] = {}
        if self._cooldown_path.exists():
            try:
                saved = json.loads(self._cooldown_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                saved = {}
        self.token_cooldowns: dict[str, float] = {t: saved.get(t, 0.0) for t in self.raw_tokens}

        figma_cfg = config.get("figma", {})
        self.base_url: str = figma_cfg.get("api_base_url", "https://api.figma.com/v1")
        self.timeout: int = figma_cfg.get("timeout_seconds", 30)
        self.max_retries: int = figma_cfg.get("max_retries", 3)
        self.backoff: int = figma_cfg.get("backoff_factor", 2)

    def _get_available_token(self) -> tuple[str, float]:
        """Возвращает свободный токен либо время ожидания.

        Returns:
            tuple[str, float]: `(токен, 0.0)`, если есть токен без кулдауна.
            Если свободных нет, `(первый токен списка, минимальное время
            ожидания в секундах)`. Это не тот токен, который освободится
            первым: после ожидания `request` выбирает токен заново.
        """
        now = time.time()
        for t in self.raw_tokens:
            if self.token_cooldowns[t] <= now:
                return t, 0.0
        min_wait = min(self.token_cooldowns.values()) - now
        return self.raw_tokens[0], max(0.0, min_wait)

    def request(self, endpoint: str) -> dict[str, Any]:
        """Выполняет GET-запрос к Figma API с ротацией токенов и повторами.

        Число попыток равно `max_retries * число токенов`. Ответ 429 расходует
        попытку без паузы: токен уходит в кулдаун (`Retry-After` либо
        `default_retry_after_seconds`, значение сохраняется на диск) и берётся
        следующий. Если все токены в кулдауне дольше
        `max_cooldown_wait_seconds`, вызывается `PermissionError`, иначе клиент
        ждёт. Таймаут, сетевая ошибка и ответы с другими кодами ошибок ведут к
        повтору с паузой `backoff_factor ** (attempt % max_retries)` секунд.

        Args:
            endpoint: Путь относительно `api_base_url` либо полный URL (`http...`).

        Returns:
            dict: Тело ответа (JSON).

        Raises:
            PermissionError: Все токены в кулдауне дольше допустимого ожидания.
            RuntimeError: Попытки исчерпаны.
        """
        url = f"{self.base_url}/{endpoint}" if not endpoint.startswith("http") else endpoint

        for attempt in range(self.max_retries * self.tokens_count):
            token, wait_needed = self._get_available_token()

            if wait_needed > 0:
                max_wait = self.pipeline_settings.get("max_cooldown_wait_seconds", 60.0)
                if wait_needed > max_wait:
                    logger.warning(
                        f"⚠️ Все токены временно исчерпали квоту (кулдаун: {wait_needed / 3600:.1f}ч). "
                        "Переключаемся на локальные данные..."
                    )
                    raise PermissionError(f"Figma API Rate Limit: блокировка на {wait_needed:.0f}с")

                logger.warning(f"⏳ Все токены заняты. Пауза {wait_needed:.1f}с...")
                time.sleep(wait_needed)
                token, _ = self._get_available_token()

            masked = mask_secret(token)

            try:
                logger.debug(f"API запрос -> {url} (токен: {masked})")
                resp = requests.get(url, headers={"X-Figma-Token": token}, timeout=self.timeout)

                if resp.status_code == 429:
                    default_retry = self.pipeline_settings.get("default_retry_after_seconds", 10)
                    retry_after = int(resp.headers.get("Retry-After", default_retry))
                    self.token_cooldowns[token] = time.time() + retry_after
                    self._cooldown_path.write_text(json.dumps(self.token_cooldowns), encoding="utf-8")
                    logger.warning(
                        f"⚠️ Токен {masked} получил 429 (блок на {retry_after}с / {retry_after / 3600:.1f}ч). "
                        "Мгновенно переключаемся на следующий ключ без сна!"
                    )
                    continue

                resp.raise_for_status()
                return resp.json()

            except requests.exceptions.Timeout:
                wait_time = self.backoff ** (attempt % self.max_retries)
                logger.warning(f"⏳ Таймаут ({self.timeout}с). Повтор через {wait_time}с...")
                time.sleep(wait_time)
            except requests.exceptions.RequestException as req_err:
                wait_time = self.backoff ** (attempt % self.max_retries)
                logger.warning(f"⚠️ Сетевая ошибка ({req_err}). Повтор через {wait_time}с...")
                time.sleep(wait_time)

        raise RuntimeError(f"Не удалось выполнить запрос: {url}")

    @staticmethod
    def _find_latest_cache(file_key: str) -> Path | None:
        """Ищет самый свежий (по времени изменения) кэш-файл Figma-файла.

        Args:
            file_key: Ключ Figma-файла.

        Returns:
            Path | None: Путь к `RAW_DIR/<file_key>_*.json` либо `None`,
            если кэша нет. Набор узлов в имени файла не учитывается.
        """
        cached_files = list(RAW_DIR.glob(f"{file_key}_*.json"))
        if not cached_files:
            return None
        return max(cached_files, key=lambda f: f.stat().st_mtime)

    def get_nodes(self, file_key: str, node_ids: list[str], force: bool = False) -> dict[str, Any]:
        """Возвращает данные узлов: из локального кэша, из сети или из устаревшего кэша.

        Порядок:
          1. Если `force=False` и самый свежий кэш файла содержит все запрошенные
             узлы, возвращается он.
          2. Иначе запрос в Figma API; ответ сохраняется в
             `RAW_DIR/<file_key>_<версия>_<хэш узлов>.json`. Хранятся только
             `keep_figma_cache_versions` последних файлов этого `file_key`.
          3. При ошибке сети возвращается самый свежий кэш файла, даже если в
             нём нет запрошенных узлов (это не проверяется).

        Кэш выбирается по `file_key` и времени изменения, а не по набору узлов.
        `force` обновляет только этот кэш и не влияет на скачанные ассеты и
        референсы. При недоступной сети откат на кэш выполняется и при
        `force=True`, поэтому `force` не гарантирует свежие данные.

        Args:
            file_key: Ключ Figma-файла.
            node_ids: Идентификаторы узлов; для сверки с кэшем `-` заменяется на `:`.
            force: Игнорировать локальный кэш и запросить данные из сети.

        Returns:
            dict: Ответ эндпоинта `files/<file_key>/nodes`.

        Raises:
            Exception: Исключение сетевого запроса, если кэша для отката нет.
        """
        target_keys = {nid.replace("-", ":") for nid in node_ids}
        latest_cache: Path | None = self._find_latest_cache(file_key)

        # 🟢 1. Проверяем локальный кэш (если не затребовано принудительное обновление)
        if latest_cache is not None and not force:
            try:
                data = json.loads(latest_cache.read_text(encoding="utf-8"))
                cached_nodes = set(data.get("nodes", {}).keys())

                if target_keys.issubset(cached_nodes):
                    logger.info(f"⚡ [ОФЛАЙН КЭШ] Все узлы {node_ids} загружены с диска: {latest_cache.name}")
                    return data

                missing_keys = target_keys - cached_nodes
                logger.info(
                    f"⚠️ В локальном кэше {latest_cache.name} отсутствуют узлы: {missing_keys}. "
                    "Запрашиваем свежие данные через сеть..."
                )
            except (json.JSONDecodeError, OSError) as read_err:
                logger.warning(f"Ошибка чтения локального кэша {latest_cache.name}: {read_err}. Пробуем сеть...")
        elif force:
            logger.info(f"🔄 [FORCE REFRESH] Принудительное обновление кэша для файла {file_key}")

        # 🌐 2. Запрос данных из Figma API через сеть
        logger.info(f"🌐 [СЕТЬ] Запрос данных из Figma API (File: {file_key}, Nodes: {node_ids})...")
        try:
            try:
                meta = self.request(f"files/{file_key}?depth=1")
                version = meta.get("version") or meta.get("lastModified", "latest").replace(":", "-")
            except (requests.exceptions.RequestException, PermissionError, RuntimeError):
                version = "offline_fallback"

            ids_hash = hashlib.md5("".join(sorted(node_ids)).encode()).hexdigest()[:8]
            cache_path = RAW_DIR / f"{file_key}_{version}_{ids_hash}.json"

            data = self.request(f"files/{file_key}/nodes?ids={','.join(node_ids)}")

            RAW_DIR.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
            logger.info(f"✓ Данные из Figma API успешно сохранены в кэш: {cache_path.name}")

            keep_n = self.pipeline_settings_full.get("cache_retention", {}).get("keep_figma_cache_versions", 3)
            all_cached = sorted(RAW_DIR.glob(f"{file_key}_*.json"), key=lambda f: f.stat().st_mtime, reverse=True)
            for stale in all_cached[keep_n:]:
                stale.unlink(missing_ok=True)
            return data

        except Exception as net_err:
            # 🛡️ 3. Аварийный откат: если сеть недоступна, но есть старый кэш — используем его
            if latest_cache is not None:
                logger.error(
                    f"❌ Ошибка сетевого запроса ({net_err}). "
                    f"Используем ранее сохраненный кэш {latest_cache.name} как fallback..."
                )
                return json.loads(latest_cache.read_text(encoding="utf-8"))
            raise net_err

    def export_images(self, file_key: str, node_ids: list[str], scale: int, fmt: str) -> dict[str, Any]:
        """Запрашивает у Figma API ссылки на экспорт узлов в изображения.

        Args:
            file_key: Ключ Figma-файла.
            node_ids: Идентификаторы узлов.
            scale: Масштаб экспорта.
            fmt: Формат (`png` или `svg`).

        Returns:
            dict: Ответ API с ключом `images` (`{node_id: url}`, значение может
            быть `None`). `{}` при пустом `node_ids`; `{"images": {}}` при любой
            ошибке, исключение не пробрасывается.
        """
        if not node_ids:
            return {}
        try:
            logger.info(f"🌐 [ЭКСПОРТ] Запрос ссылок на {len(node_ids)} изображений (fmt={fmt}, scale={scale})...")
            return self.request(f"images/{file_key}?ids={','.join(node_ids)}&format={fmt}&scale={scale}")
        except Exception as e:
            logger.warning(
                f"⚠️ Экспорт картинок через API недоступен ({e}). Будут использованы уже скачанные локальные ассеты.")
            return {"images": {}}


def parse_rgba(color: dict, opacity: float = 1.0) -> str:
    """Переводит цвет Figma в CSS-строку `rgba(...)`.

    Функцию импортирует `generate_landing.py`, поэтому это публичный API модуля.

    Args:
        color: Цвет Figma: словарь `r`, `g`, `b`, `a` с компонентами 0..1.
        opacity: Дополнительная непрозрачность (например, заливки),
            умножается на `a`.

    Returns:
        str: `rgba(R, G, B, A)`, где R, G, B — целые 0..255, A округлена до
        2 знаков; `"transparent"`, если `color` пустой.
    """
    if not color:
        return "transparent"
    r, g, b = (round(color.get(c, 0) * 255) for c in "rgb")
    a = round(color.get("a", 1.0) * opacity, 2)
    return f"rgba({r}, {g}, {b}, {a})"


def resolve_font_weight(text_style: dict) -> int:
    """Определяет числовой вес шрифта текстового узла.

    Числовой `fontWeight` из Figma API ненадёжен у кастомных шрифтов: он может
    быть 400 при `fontStyle="Semi Bold"`. Поэтому сначала ищется совпадение
    подстрокой в `fontPostScriptName` и `fontStyle` (строка приводится к нижнему
    регистру, ключи проверяются от самых длинных, чтобы `semibold` совпал
    раньше `bold`). Если совпадений нет, берётся `fontWeight`, затем
    `fallback_weight`.

    Соответствие «слово -> вес» задаётся в
    `analysis_rules.json -> font_weight_resolution.keyword_weight_map`. Ключи
    должны быть в нижнем регистре и включать слитные («semibold») и раздельные
    («semi bold») написания, если такие встречаются.

    Args:
        text_style: Объект `style` текстового узла Figma.

    Returns:
        int: Вес шрифта.
    """
    cfg = ANALYSIS_RULES.get("font_weight_resolution", {})
    keyword_map: dict = cfg.get("keyword_weight_map", {})
    fallback = int(cfg.get("fallback_weight", 400))

    candidates = " ".join(filter(None, [
        text_style.get("fontPostScriptName", ""),
        text_style.get("fontStyle", ""),
    ])).lower()

    # Сортируем ключи по длине по убыванию динамически (не полагаемся на
    # порядок ключей в JSON) — иначе короткое "bold" может сматчиться
    # раньше более специфичного "semibold" внутри той же строки.
    for keyword in sorted(keyword_map, key=len, reverse=True):
        if keyword in candidates:
            return int(keyword_map[keyword])

    try:
        return int(text_style.get("fontWeight", fallback))
    except (TypeError, ValueError):
        return fallback


def figma_gradient_to_css_angle(handles: list[dict[str, float]], ) -> float:
    """Переводит направление градиента Figma в угол CSS.

    Используется вектор от первой ручки ко второй. Figma: x вправо, y вниз;
    CSS `linear-gradient`: 0deg вверх, 90deg вправо, 180deg вниз, 270deg влево.
    Третья ручка (ширина градиента) в CSS не выражается и не используется. Для
    conic-градиентов (angular, diamond) значение служит приближением.

    Args:
        handles: Ручки градиента `[{"x": ..., "y": ...}, ...]` в нормализованных
            координатах Figma.

    Returns:
        float: Угол в градусах (0..360), округлён до 0.1; `180.0`, если ручек
        меньше двух или вектор вырожденный.
    """
    if len(handles) < 2:
        return 180.0

    h0 = handles[0]
    h1 = handles[1]
    dx = (h1.get("x", 0.5) - h0.get("x", 0.5))
    dy = (h1.get("y", 0.5) - h0.get("y", 0.5))

    # Защита от практически нулевого вектора.
    if math.isclose(dx, 0.0, abs_tol=1e-9, ) and math.isclose(dy, 0.0, abs_tol=1e-9, ):
        return 180.0

    angle = math.degrees(math.atan2(dx, -dy))

    return round(angle % 360.0, 1, )


def parse_gradient_fill(fill: dict, fill_opacity: float, ) -> GradientData | None:
    """Преобразует градиентную заливку Figma в `GradientData`.

    Соответствие типов:
      * `GRADIENT_LINEAR`  -> `linear`;
      * `GRADIENT_RADIAL`  -> `radial`;
      * `GRADIENT_ANGULAR` -> `conic`;
      * `GRADIENT_DIAMOND` -> `conic` (приближение: точного аналога в CSS нет);
      * другой тип -> `linear` (предупреждение в лог).

    Центр (`center_x_pct`, `center_y_pct`) берётся из первой ручки, при её
    отсутствии 50% / 50%. Непрозрачность заливки умножается на альфу остановок.

    Args:
        fill: Заливка Figma (`type`, `gradientStops`, `gradientHandlePositions`).
        fill_opacity: Непрозрачность заливки, 0..1.

    Returns:
        GradientData | None: Градиент; `None`, если остановок нет или
        `gradientStops` не список.
    """

    stops_raw = fill.get("gradientStops", [], )

    if not isinstance(stops_raw, list, ):
        return None

    if not stops_raw:
        return None

    stops: list[GradientStop] = []

    for stop in stops_raw:
        if not isinstance(stop, dict, ):
            continue

        color_data = stop.get("color", {}, )

        try:
            position = float(stop.get("position", 0.0, ))
        except (TypeError, ValueError,):
            position = 0.0

        color = parse_rgba(color_data, opacity=fill_opacity, )
        stops.append(GradientStop(color=color, position=position, ))

    if not stops:
        return None

    handles_raw = fill.get("gradientHandlePositions", [], )
    handles: list[dict[str, float]] = []

    if isinstance(handles_raw, list, ):
        for handle in handles_raw:
            if not isinstance(handle, dict, ):
                continue

            try:
                x = float(handle.get("x", 0.0, ))
            except (TypeError, ValueError,):
                x = 0.0
            try:
                y = float(handle.get("y", 0.0, ))
            except (TypeError, ValueError,):
                y = 0.0

            handles.append({"x": x, "y": y, })

    figma_type = str(fill.get("type", "", ) or "")

    # Первая ручка является центром radial/angular/diamond
    # в нормализованном object space Figma.
    center_x_pct = 50.0
    center_y_pct = 50.0

    if handles:
        center_x_pct = (handles[0].get("x", 0.5) * 100.0)
        center_y_pct = (handles[0].get("y", 0.5) * 100.0)

    # --------------------------------------------------
    # LINEAR
    # --------------------------------------------------
    if figma_type == "GRADIENT_LINEAR":
        return GradientData(
            figma_type=figma_type,
            css_type="linear",
            angle_deg=figma_gradient_to_css_angle(
                handles
            ),
            stops=stops,
            handle_positions=handles,
            center_x_pct=center_x_pct,
            center_y_pct=center_y_pct,
        )

    # --------------------------------------------------
    # RADIAL
    # --------------------------------------------------
    if figma_type == "GRADIENT_RADIAL":
        return GradientData(
            figma_type=figma_type,
            css_type="radial",
            angle_deg=0.0,
            stops=stops,
            handle_positions=handles,
            center_x_pct=center_x_pct,
            center_y_pct=center_y_pct,
        )

    # --------------------------------------------------
    # ANGULAR
    # --------------------------------------------------
    if figma_type == "GRADIENT_ANGULAR":
        angle_deg = figma_gradient_to_css_angle(handles)

        logger.info(f"Angular Figma gradient mapped to CSS conic-gradient: "
                    f"center=({center_x_pct:.1f}%, {center_y_pct:.1f}%), angle={angle_deg:.1f}°")

        return GradientData(
            figma_type=figma_type,
            css_type="conic",
            angle_deg=angle_deg,
            stops=stops,
            handle_positions=handles,
            center_x_pct=center_x_pct,
            center_y_pct=center_y_pct,
        )

    # --------------------------------------------------
    # DIAMOND
    # --------------------------------------------------
    if figma_type == "GRADIENT_DIAMOND":
        angle_deg = figma_gradient_to_css_angle(handles)

        logger.warning("Diamond Figma gradient has no exact plain-CSS equivalent. "
                       "Using conic-gradient approximation: "
                       f"center=({center_x_pct:.1f}%, {center_y_pct:.1f}%), angle={angle_deg:.1f}°")

        return GradientData(
            figma_type=figma_type,
            css_type="conic",
            angle_deg=angle_deg,
            stops=stops,
            handle_positions=handles,
            center_x_pct=center_x_pct,
            center_y_pct=center_y_pct,
        )

    # --------------------------------------------------
    # UNKNOWN
    # --------------------------------------------------
    logger.warning(f"Unsupported Figma gradient type '{figma_type}'. Falling back to linear-gradient.")

    return GradientData(figma_type=figma_type, css_type="linear", angle_deg=figma_gradient_to_css_angle(handles),
                        stops=stops, handle_positions=handles, center_x_pct=center_x_pct, center_y_pct=center_y_pct, )


# --- 1. FIGMA NORMALIZER ---
class FigmaNormalizer:
    """Преобразует узлы Figma API в IR-дерево (`IRNode`)."""

    @staticmethod
    def normalize_node(n: dict[str, Any], p_x: float = 0.0, p_y: float = 0.0, p_w: float = 0.0,
                       p_h: float = 0.0, ) -> IRNode:
        """Рекурсивно нормализует узел Figma и его видимых детей в `IRNode`.

        Переносятся геометрия, Auto Layout, констрейнты, заливки, рамка,
        эффекты и параметры текста.

        Геометрия: `x` и `y` считаются относительно родителя по
        `absoluteBoundingBox`. Корнем считается узел с `p_w == 0 and p_h == 0`,
        у него `x = y = 0`; поэтому не-корневой узел с родителем нулевого
        размера тоже получит `x = y = 0`.

        Фон (не TEXT): SOLID перезаписывает ранее найденный градиент. Градиент
        даёт цвет средней остановки (`bg_is_gradient_approx=True`) и
        `bg_gradient`. IMAGE учитывается (`bg_is_image`, `image_fill`), только
        если цвет фона ещё не найден. Рамка берётся из первого видимого stroke.

        Текст: вес определяет `resolve_font_weight`. `textAutoResize`:
        `WIDTH_AND_HEIGHT` -> HUG по обеим осям, `HEIGHT` -> HUG по вертикали,
        `TRUNCATE` -> HUG по горизонтали (значение `TRUNCATE` в Figma API
        устарело, усечение текста описывает поле `textTruncation`; здесь оно не
        читается). `intended_single_line` истинен, если текст не HUG по ширине
        и его высота не больше 1.4 высоты одной строки.

        Невидимые дети (`visible: false`) пропускаются; видимость самого узла
        не проверяется.

        Ограничение: высота строки берётся из `lineHeightPercent`. В Figma API
        это процент от «нормальной» высоты строки шрифта (поле устарело), а в IR
        значение используется как процент от размера шрифта.

        Args:
            n: Узел Figma API.
            p_x: Абсолютная координата X родителя, px.
            p_y: Абсолютная координата Y родителя, px.
            p_w: Ширина родителя, px.
            p_h: Высота родителя, px.

        Returns:
            IRNode: Нормализованный узел вместе с детьми.
        """
        bbox = (n.get("absoluteBoundingBox") or {})
        abs_x = float(bbox.get("x", 0.0, ))
        abs_y = float(bbox.get("y", 0.0, ))
        w = float(bbox.get("width", 0.0, ))
        h = float(bbox.get("height", 0.0, ))
        is_root = (p_w == 0.0 and p_h == 0.0)

        if is_root:
            x = 0.0
            y = 0.0

        else:
            x = abs_x - p_x
            y = abs_y - p_y

        rel_geom = Geometry(x=x, y=y, width=w, height=h, parent_width=p_w, parent_height=p_h, absolute_x=abs_x,
                            absolute_y=abs_y, )

        # ==========================================================
        # AUTO LAYOUT
        # ==========================================================

        layout_mode = n.get("layoutMode")
        is_flex = (layout_mode in {"HORIZONTAL", "VERTICAL", })
        direction: Literal["row", "column",] = ("row" if layout_mode == "HORIZONTAL" else "column")
        justify = ALIGN_MAP.get(n.get("primaryAxisAlignItems", "", ), "flex-start", )
        align_items = ALIGN_MAP.get(n.get("counterAxisAlignItems", "", ), "flex-start", )
        raw_constraints = (n.get("constraints", {}, ) or {})
        h_constraint = (H_CONSTRAINT_MAP.get(raw_constraints.get("horizontal", "", ), "left", ))
        v_constraint = (V_CONSTRAINT_MAP.get(raw_constraints.get("vertical", "", ), "top", ))
        layout_positioning = (n.get("layoutPositioning", "AUTO", ))

        if layout_positioning not in {"AUTO", "ABSOLUTE", }:
            layout_positioning = "AUTO"
        strategy: Literal["flex", "relative", "flow",]

        if is_flex:
            strategy = "flex"
        elif n.get("children"):
            strategy = "relative"
        else:
            strategy = "flow"

        layout = IRLayout(
            is_flex=is_flex,
            direction=direction,
            gap=float(n.get("itemSpacing", 0.0, )),
            padding_top=float(n.get("paddingTop", 0.0, )),
            padding_right=float(n.get("paddingRight", 0.0, )),
            padding_bottom=float(n.get("paddingBottom", 0.0, )),
            padding_left=float(n.get("paddingLeft", 0.0, )),
            align_items=align_items,
            justify_content=justify,
            sizing_horizontal=n.get("layoutSizingHorizontal", "FIXED", ),
            sizing_vertical=n.get("layoutSizingVertical", "FIXED", ),
            wrap=(n.get("layoutWrap") == "WRAP"),
            layout_strategy=strategy,
            h_constraint=h_constraint,
            v_constraint=v_constraint,
            layout_positioning=(layout_positioning),
            primary_axis_sizing_mode=n.get("primaryAxisSizingMode", "AUTO", ),
            counter_axis_sizing_mode=n.get("counterAxisSizingMode", "AUTO", ),
            layout_align=n.get("layoutAlign", "INHERIT", ),
            layout_grow=float(n.get("layoutGrow", 0.0, ) or 0.0),
            width=w,
            height=h,
            clips_content=bool(n.get("clipsContent", False)),
            extras={"layoutMode": layout_mode, "layoutPositioning": (layout_positioning),
                    "layoutGrow": n.get("layoutGrow", 0, ),
                    "layoutAlign": n.get("layoutAlign"),
                    }, )

        # ==========================================================
        # STYLE
        # ==========================================================

        op = n.get("opacity", 1.0, )
        node_blend_mode = n.get("blendMode", "PASS_THROUGH", )
        bg = None
        bg_is_gradient = False
        bg_is_image = False
        image_fill = None
        node_type = n.get("type", "FRAME", )
        bg_gradient = None

        if node_type != "TEXT":
            for fill in n.get("fills", [], ):
                if not fill.get("visible", True, ):
                    continue

                fill_opacity = float(fill.get("opacity", 1.0, ))

                if (fill.get("type") == "SOLID"):
                    bg = parse_rgba(fill.get("color"), fill_opacity, )
                    bg_is_gradient = False
                    bg_gradient = None
                    break

                if (fill.get("type", "", ).startswith("GRADIENT") and bg is None):
                    stops = fill.get("gradientStops", [], )

                    if stops:
                        mid_stop = stops[len(stops) // 2]
                        bg = parse_rgba(mid_stop.get("color", {}, ), fill_opacity, )
                        bg_is_gradient = True
                        bg_gradient = (parse_gradient_fill(fill, fill_opacity, ))

                if (fill.get("type") == "IMAGE" and bg is None):
                    bg_is_image = True

                    raw_filters = fill.get("filters", {})
                    if not isinstance(raw_filters, dict):
                        raw_filters = {}

                    normalized_filters = {}
                    for key, value in raw_filters.items():
                        try:
                            normalized_filters[key] = float(value)
                        except (TypeError, ValueError):
                            continue

                    raw_transform = fill.get("imageTransform")
                    image_transform = (raw_transform if isinstance(raw_transform, list) else None)

                    image_fill = ImageFillSpec(
                        image_ref=fill.get("imageRef"),
                        scale_mode=fill.get("scaleMode"),
                        image_transform=image_transform,
                        filters=normalized_filters,
                        opacity=fill_opacity,
                    )

        # ==========================================================
        # TEXT STYLE
        # ==========================================================

        text_color = None
        text_gradient = None

        font_family = None
        font_size = None
        font_weight = None
        line_height = None
        letter_spacing = None

        if n.get("type") == "TEXT":
            text_style = n.get("style", {}, )
            font_family = (text_style.get("fontFamily"))

            try:
                font_size = float(text_style.get("fontSize", 16, ))
            except (TypeError, ValueError,):
                font_size = 16.0

            # Вес определяется по fontPostScriptName/fontStyle, числовой fontWeight
            # используется как запасной вариант (см. resolve_font_weight).
            font_weight = resolve_font_weight(text_style)

            try:
                line_height = float(text_style.get("lineHeightPercent", 120.0, ))
            except (TypeError, ValueError,):
                line_height = 120.0

            try:
                letter_spacing = float(text_style.get("letterSpacing", 0.0, ))
            except (TypeError, ValueError,):
                letter_spacing = 0.0

            try:
                text_align = TEXT_ALIGN_MAP.get(
                    text_style.get("textAlignHorizontal", "LEFT"),
                    "left",
                )
            except (TypeError, AttributeError):
                text_align = "left"

            text_auto_resize = text_style.get("textAutoResize", "NONE")
            if text_auto_resize == "WIDTH_AND_HEIGHT":
                layout.sizing_horizontal = "HUG"
                layout.sizing_vertical = "HUG"
            elif text_auto_resize == "HEIGHT":
                layout.sizing_vertical = "HUG"
            elif text_auto_resize == "TRUNCATE":
                layout.sizing_horizontal = "HUG"

            try:
                one_line_height = font_size * (line_height / 100.0)
            except (TypeError, ZeroDivisionError):
                one_line_height = None

            layout.intended_single_line = bool(
                layout.sizing_horizontal != "HUG"
                and one_line_height
                and 0 < rel_geom.height <= one_line_height * 1.4
            )

            for fill in n.get("fills", [], ):
                if not fill.get("visible", True, ):
                    continue
                fill_opacity = float(fill.get("opacity", 1.0, ))
                effective_opacity = fill_opacity

                if (fill.get("type") == "SOLID" and "color" in fill):
                    text_color = parse_rgba(fill["color"], effective_opacity, )
                    text_gradient = None
                    break

                if fill.get("type", "", ).startswith("GRADIENT"):
                    gradient = (parse_gradient_fill(fill, effective_opacity, ))

                    if gradient is not None:
                        text_gradient = (gradient)
                        stops = fill.get("gradientStops", [], )

                        if stops:
                            mid_stop = stops[len(stops) // 2]
                            text_color = (parse_rgba(mid_stop.get("color", {}, ), effective_opacity, ))

                    break

        # ==========================================================
        # BORDER
        # ==========================================================

        border_color = None
        border_width = 0.0

        for stroke in n.get("strokes", [], ):

            if (stroke.get("visible", True, ) and "color" in stroke):
                border_color = parse_rgba(stroke["color"], float(stroke.get("opacity", 1.0)), )
                border_width = float(n.get("strokeWeight", 1.0, ))
                break

        radius = float(n.get("cornerRadius", 0.0, ))

        style = IRStyle(
            bg_color=bg,
            bg_is_image=bg_is_image,
            image_fill=image_fill,
            text_color=text_color,
            text_gradient=text_gradient,
            font_family=font_family,
            font_size=font_size,
            font_weight=font_weight,
            line_height_percent=line_height,
            letter_spacing=letter_spacing,
            text_align=text_align if n.get("type") == "TEXT" else None,
            border_radius=radius,
            border_color=border_color,
            bg_gradient=bg_gradient,
            border_width=border_width,
            opacity=op,
            blend_mode=node_blend_mode,
            bg_is_gradient_approx=bg_is_gradient,
            extras={"effects": n.get("effects", []), "render_bounds": n.get("absoluteRenderBounds")}
        )

        # ==========================================================
        # IR NODE
        # ==========================================================

        ir_node = IRNode(id=n["id"], name=n.get("name", "", ),
                         type=n.get("type", "FRAME", ),
                         rel_geometry=rel_geom,
                         layout=layout,
                         style=style,
                         characters=(n.get("characters") if n.get("type") == "TEXT" else None),
                         component_name=(n.get("name") if n.get("type") in {"COMPONENT", "INSTANCE", } else None),
                         extras={"raw_type": n.get("type"), },
                         )

        # ==========================================================
        # CHILDREN
        # ==========================================================

        for child in n.get("children", [], ):
            if child.get("visible", True, ):
                ir_node.children.append(FigmaNormalizer.normalize_node(child, abs_x, abs_y, p_w=w, p_h=h, ))
        return ir_node


# --- 2. ASSET CLASSIFIER ---
class AssetClassifier:
    """Размечает узлы IR: что экспортировать файлом и как рендерить в HTML.

    Решает две независимые задачи:
      1. Нужен ли отдельный экспорт (SVG/PNG): `asset_id`, `asset_format`.
      2. Как узел попадёт в HTML: `render_strategy` (`native`, `asset`,
         `raster_composite`).

    Figma-тип узла (`GROUP`, `RECTANGLE` и т.д.) не меняется, меняется только
    `render_strategy`. Поэтому QA и семантический анализ видят реальную
    структуру дерева.
    """

    @staticmethod
    def classify_and_mark(root_ir: IRNode, max_icon_dim: float, ) -> tuple[list[str], list[str]]:
        """Размечает узлы для экспорта и рендера, возвращает id для скачивания.

        Порядок проверок для каждого узла (побеждает первая сработавшая):
          1. Контейнер, у прямого ребёнка которого есть IMAGE-fill с фильтрами
             или `imageTransform`, -> `raster_composite`, экспорт PNG целиком.
          2. Векторный лист (`VECTOR`, `BOOLEAN_OPERATION`, `STAR`, `LINE`,
             `ELLIPSE`) -> `asset`, SVG.
          3. Иконка (FRAME/GROUP/INSTANCE без структурных детей, стороны не
             больше `max_icon_dim`) или RECTANGLE с IMAGE-fill -> `asset`, PNG.
          4. Иначе обход детей.
        Потомки размеченного узла не обходятся.

        Изменяет узлы на месте: `render_strategy`, `asset_id`, `asset_format`.

        Args:
            root_ir: Корень обходимого дерева.
            max_icon_dim: Максимальная сторона (px) узла, который считается иконкой.

        Returns:
            tuple[list[str], list[str]]: `(svg_node_ids, png_node_ids)`.
        """
        svg_node_ids: list[str] = []
        png_node_ids: list[str] = []

        CONTAINER_TYPES = {"GROUP", "FRAME", "COMPONENT", "INSTANCE"}
        VECTOR_TYPES = {"VECTOR", "BOOLEAN_OPERATION", "STAR", "LINE", "ELLIPSE"}

        def has_structural_children(node: IRNode) -> bool:
            """Проверяет, есть ли у узла дети FRAME, COMPONENT, INSTANCE, GROUP или TEXT.

            Args:
                node: Проверяемый узел.

            Returns:
                bool: `True`, если такие дети есть.
            """
            return any(c.type in {"FRAME", "COMPONENT", "INSTANCE", "GROUP", "TEXT"} for c in node.children)

        def has_visible_effects(node: IRNode) -> bool:
            """Проверяет, есть ли у узла видимый эффект.

            Args:
                node: Проверяемый узел.

            Returns:
                bool: `True`, если в `style.extras["effects"]` есть видимый эффект с типом.
            """
            effects = (
                node.style.extras.get("effects", [])
                if isinstance(node.style.extras, dict)
                else []
            )
            return any(
                isinstance(e, dict) and e.get("visible", True) and e.get("type")
                for e in effects
            )

        def has_nontrivial_blend(node: IRNode) -> bool:
            """Проверяет, что режим наложения узла не `NORMAL` и не `PASS_THROUGH`.

            Args:
                node: Проверяемый узел.

            Returns:
                bool: `True` для нестандартного режима наложения.
            """
            return node.style.blend_mode not in {None, "", "NORMAL", "PASS_THROUGH"}

        def has_image_filters(node: IRNode) -> bool:
            """Проверяет, что у IMAGE-fill узла есть фильтры Figma или `imageTransform`.

            Args:
                node: Проверяемый узел.

            Returns:
                bool: `True`, если такие параметры заданы.
            """
            image_fill = node.style.image_fill
            if image_fill is None:
                return False
            return bool(image_fill.filters) or bool(image_fill.image_transform)

        def should_rasterize_group(node: IRNode) -> bool:
            """Проверяет, что группу нужно растеризовать целиком.

            Растеризация нужна, только если пиксельный контент нельзя выразить
            через CSS: у прямого ребёнка есть IMAGE-fill с фильтрами Figma или
            с `imageTransform`. Тени, блюры (`effects_to_css`) и режим наложения
            самого узла выражаются в CSS и растеризацию родителя не вызывают.
            Растеризуется контейнер с такой картинкой: PNG поглощает вложенные
            blend-слои, которые Figma рисует вместе с ней.

            Args:
                node: Проверяемый узел.

            Returns:
                bool: `True` для контейнера с хотя бы одним таким ребёнком.
            """

            if node.type not in CONTAINER_TYPES or not node.children:
                return False

            return any(has_image_filters(c) for c in node.children)

        def walk(n: IRNode) -> None:
            """Размечает `n`; если ни одно правило не сработало, обходит его детей.

            Args:
                n: Текущий узел.
            """
            is_vector_leaf = n.type in VECTOR_TYPES
            is_pure_icon = (
                    n.type in {"FRAME", "GROUP", "INSTANCE"}
                    and not has_structural_children(n)
                    and 0 < n.rel_geometry.width <= max_icon_dim
                    and 0 < n.rel_geometry.height <= max_icon_dim
            )
            is_image_fill_rect = n.type == "RECTANGLE" and n.style.bg_is_image

            # 1) Сложный compositing — растеризуем ГРУППУ ЦЕЛИКОМ,
            #    но Figma-тип узла НЕ трогаем.
            if should_rasterize_group(n):
                n.render_strategy = "raster_composite"
                n.asset_id = n.id
                n.asset_format = "png"
                png_node_ids.append(n.id)
                logger.info(
                    f"  [COMPOSITE->PNG] {n.id} '{n.name}' "
                    f"blend={n.style.blend_mode}"
                )
                # Не спускаемся к детям: вся композиция экспортируется
                # одним PNG из Figma (Figma сама делает compositing).
                return

            # 2) Простой векторный лист -> SVG.
            if is_vector_leaf:
                n.render_strategy = "asset"
                n.asset_id = n.id
                n.asset_format = "svg"
                svg_node_ids.append(n.id)
                return

            # 3) Маленькая иконка или прямоугольник с растровым fill'ом -> PNG.
            #    Тип узла не меняется, чтобы RECTANGLE оставался отличим от иконки.
            if is_pure_icon or is_image_fill_rect:
                n.render_strategy = "asset"
                n.asset_id = n.id
                n.asset_format = "png"
                png_node_ids.append(n.id)
                return

            for ch in n.children:
                walk(ch)

        walk(root_ir)
        logger.info(f"Классификация ассетов: обнаружено {len(svg_node_ids)} SVG и {len(png_node_ids)} PNG")
        return svg_node_ids, png_node_ids


# --- 3. STYLE BUILDER ---
class StyleBuilder:
    """Строит дизайн-систему (`DesignSystemSpec`) из IR-дерева источника стиля."""

    def __init__(self, defaults: dict, rules: dict, pipeline_settings: dict) -> None:
        """Сохраняет конфигурацию.

        Args:
            defaults: Содержимое `defaults.json` (цвета, типографика, радиусы, отступы).
            rules: Содержимое `analysis_rules.json` (пороги детекции).
            pipeline_settings: Содержимое `pipeline_settings.json`.
        """
        self.defaults = defaults
        self.rules = rules
        self.pipeline_settings = pipeline_settings

    def build(self, root_ir: IRNode, source_ref: SourceRef,
              reference_image_path: Path | None = None, ) -> DesignSystemSpec:
        """Собирает токены цвета, типографики и радиусов из дерева источника стиля.

        Шаги:
          1. Обход дерева: кандидаты в акценты (небольшие узлы с заливкой, вес —
             площадь, умноженная на надёжность фона), цвета и размеры текста,
             радиусы, кнопки.
          2. canvas и surface: два лучших кандидата `collect_background_candidates`.
             Если фон в дереве не найден, сэмплинг PNG-референса. Surface,
             неотличимый от canvas, заменяется дефолтом (confidence 0.3).
          3. primary: самый частый цвет заливки кнопок (confidence 1.0), иначе
             лучший accent-кандидат (0.8, при низкой альфе 0.4), иначе дефолт (0.5).
          4. text_primary и text_secondary: два самых частых цвета текста.
          5. Типографика: размеры делятся на h1/h2/h3/body/caption по отношению к
             наибольшему; шрифт — самый частый. Исключение: `caption`
             определяется по абсолютному размеру (не больше `caption_max_size`).
             Токены `button` и `stat` выводятся из `body` и `h1`.
          6. Радиусы: для кнопок — самый частый среди кнопок, для карточек —
             самый частый в дереве.

        Побочный эффект: узлам, распознанным как кнопки, ставится
        `component_role="button"`.

        Args:
            root_ir: Корень IR-дерева источника стиля.
            source_ref: Ссылка на источник (файл и узел Figma).
            reference_image_path: PNG-референс источника; запасной вариант для
                canvas, если фон в дереве не найден.

        Returns:
            DesignSystemSpec: Токены и варианты кнопок.
        """
        logger.info(f"Анализ дизайн-системы из узла {source_ref.node_id}...")
        text_colors = Counter()
        accent_candidates = Counter()
        radii = Counter()
        fonts_by_family = defaultdict(list)
        buttons = []

        surf_rules = self.rules["surface_detection"]
        btn_rules = self.rules["button_detection"]

        min_accent_dim = self.rules.get("asset_detection", {}).get("min_accent_dimension", 12.0)
        filtered_out_micro_decorations = 0
        bg_detect_cfg = self.rules.get("reference_bg_detection", {})

        def collect(node: IRNode) -> None:
            """Собирает статистику по поддереву: цвета, шрифты, радиусы, кнопки.

            Изменяет `component_role` у узлов, распознанных как кнопки.

            Args:
                node: Корень обходимого поддерева.
            """
            nonlocal filtered_out_micro_decorations
            is_bg_reliable = passes_bg_reliability_filter(node, bg_detect_cfg)
            if (node.style.bg_color and node.style.bg_color != "transparent"
                    and is_bg_reliable
                    and not (node.layout.width >= surf_rules["min_width"]
                             and node.layout.height >= surf_rules["min_height"])):
                weight = bg_reliability_weight(node, bg_detect_cfg)
                area = max(node.layout.width, 0.0) * max(node.layout.height, 0.0) * weight
                if node.layout.width >= min_accent_dim and node.layout.height >= min_accent_dim:
                    accent_candidates[node.style.bg_color] += area
                else:
                    filtered_out_micro_decorations += 1

            if node.type == "TEXT":
                if node.style.text_color:
                    text_colors[node.style.text_color] += 1
                if node.style.font_family and node.style.font_size:
                    fonts_by_family[node.style.font_family].append(node.style)

            if node.style.border_radius > 0:
                radii[node.style.border_radius] += 1

            is_btn_container = False

            if node.type in {"FRAME", "COMPONENT", "INSTANCE", }:
                text_children = [child for child in node.children if child.type == "TEXT"]

                is_btn_container = (
                        ComponentHeuristics.looks_like_button(
                            node,
                            text_children,
                        )
                        and not ComponentHeuristics.looks_like_badge(
                    node,
                    text_children,
                )
                )
            if is_btn_container:
                node.component_role = "button"
                txt_child = next(c for c in node.children if c.type == "TEXT")
                buttons.append(ComponentVariant(
                    name=node.name,
                    role="button",
                    padding_top=node.layout.padding_top,
                    padding_right=node.layout.padding_right,
                    padding_bottom=node.layout.padding_bottom,
                    padding_left=node.layout.padding_left,
                    radius=node.style.border_radius,
                    bg_color=node.style.bg_color,
                    text_color=txt_child.style.text_color,
                    border_color=node.style.border_color
                ))

            for ch in node.children:
                collect(ch)

        collect(root_ir)
        logger.info(
            f"  Отфильтровано микро-декораций (< {min_accent_dim}px) из подбора primary: "
            f"{filtered_out_micro_decorations}"
        )

        c_def = self.defaults["colors"]
        t_def = self.defaults["typography"]
        r_def = self.defaults["radii"]

        # --- Детекция canvas/surface: единый алгоритм с ReferenceRenderer
        # (coverage-based скоринг с учётом viewport, blendMode, gradient-approx, альфы).
        bg_candidates = collect_background_candidates(
            root_ir, root_ir.rel_geometry.width, root_ir.rel_geometry.height, bg_detect_cfg
        )
        # Кандидаты отсортированы по убыванию score: [0] — canvas, [1] — surface.
        canvas_from_tree = bg_candidates[0][1] if bg_candidates else None
        surface_from_tree = bg_candidates[1][1] if len(bg_candidates) > 1 else None

        ce_cfg = self.pipeline_settings.get("color_extraction", {})
        sample_top_k = ce_cfg.get("sample_top_k", 5)
        sample_resize = ce_cfg.get("sample_resize", 100)
        # Порог альфы при сэмплинге PNG: 0.6 отсекает почти прозрачные декоративные
        # слои (alpha≈0.01) и оставляет полупрозрачные фоны (alpha 0.6-0.9).
        # Переопределяется в pipeline_settings.json -> color_extraction.min_sample_alpha.
        min_sample_alpha = ce_cfg.get("min_sample_alpha", 0.6)

        sampled_colors: list[str] = []
        if canvas_from_tree is None and reference_image_path and reference_image_path.exists():
            sampled_colors = sample_dominant_colors_from_png(
                reference_image_path, k=sample_top_k, resize_dim=sample_resize,
                min_alpha=min_sample_alpha,
            )
            if sampled_colors:
                logger.warning(
                    f"⚠️ Ни один узел дерева не прошёл детекцию фона (coverage/фильтры). "
                    f"Используем alpha-aware сэмплинг из PNG-референса (min_alpha={min_sample_alpha}): "
                    f"canvas={sampled_colors[0]}"
                )

        token_confidence_rules = self.rules.get("token_confidence", {})
        MIN_SURFACE_CANVAS_DISTANCE = token_confidence_rules.get("min_surface_canvas_distance", 0.06)
        LOW_CONFIDENCE_ACCENT_ALPHA = token_confidence_rules.get("low_confidence_accent_alpha", 0.5)

        if sampled_colors:
            canvas_val = sampled_colors[0]
            canvas_source = "figma"
            surface_val = sampled_colors[1] if len(sampled_colors) > 1 else c_def["surface"]
            surface_from_real_candidate = len(sampled_colors) > 1
        elif canvas_from_tree is not None:
            canvas_val = canvas_from_tree
            canvas_source = "figma"
            surface_val = surface_from_tree if surface_from_tree is not None else c_def["surface"]
            surface_from_real_candidate = surface_from_tree is not None
        else:
            canvas_val = c_def["canvas"]
            canvas_source = "config"
            surface_val = c_def["surface"]
            surface_from_real_candidate = False

        # Кандидат surface, неотличимый от canvas (расстояние в HLS меньше
        # min_surface_canvas_distance), считается шумом: в секции нет отдельной
        # панели, и второй по частоте цвет — соседний оттенок фона (антиалиасинг,
        # лёгкий градиент). Такой кандидат заменяется дефолтом из конфига
        # (source="config", confidence=0.3), иначе заливка card/surface совпала бы
        # с canvas и граница карточки пропала бы.
        surface_distance = color_distance_hls(canvas_val, surface_val)
        surface_is_reliable = surface_from_real_candidate and surface_distance >= MIN_SURFACE_CANVAS_DISTANCE
        if surface_from_real_candidate and not surface_is_reliable:
            logger.warning(f"⚠️ 'surface'-кандидат ({surface_val}) визуально неотличим от 'canvas' ({canvas_val}) "
                           f"(distance={surface_distance:.3f} < {MIN_SURFACE_CANVAS_DISTANCE:.2f}) — "
                           "вероятно, в секции нет отдельной панели для сэмплинга. "
                           f"Откатываемся на config-дефолт surface={c_def['surface']} вместо того, "
                           "чтобы выдавать дубликат canvas за figma-достоверный токен.")
            surface_val = c_def["surface"]

        button_bg_counter = Counter(
            button.bg_color
            for button in buttons
            if button.bg_color
        )

        if button_bg_counter:
            primary_val = button_bg_counter.most_common(1)[0][0]
            primary_src: Literal["figma", "config"] = "figma"
            primary_confidence = 1.0

        elif accent_candidates:
            primary_val = accent_candidates.most_common(1)[0][0]
            primary_src = "figma"
            primary_alpha = alpha_from_rgba(primary_val)
            # Кнопок в секции нет, поэтому primary берётся из accent-кандидата.
            # Низкая альфа (меньше low_confidence_accent_alpha) указывает на бейдж
            # или тег, а не на CTA. Цвет не подменяется (в нём может быть полезный
            # оттенок), но confidence занижается: 0.4 при низкой альфе, иначе 0.8.
            # По confidence StyleTransferEngine (resolve_reliable_accent_token)
            # решает, можно ли использовать токен как заливку кнопок и карточек.
            primary_confidence = 0.4 if primary_alpha < LOW_CONFIDENCE_ACCENT_ALPHA else 0.8
            if primary_confidence < 0.8:
                logger.warning(f"⚠️ 'primary' токен ({primary_val}) взят не из настоящей кнопки "
                               "(в reference-секции кнопок не найдено), а из decorative "
                               f"accent-кандидата с низкой альфой (alpha={primary_alpha:.2f}) — "
                               f"похоже на бейдж/тег, а не на цвет CTA. confidence={primary_confidence:.1f}.")

        else:
            primary_val = c_def["primary"]
            primary_src = "config"
            primary_confidence = 0.5

        resolved_colors = {
            "canvas": ResolvedToken(value=canvas_val, source=canvas_source),
            "surface": ResolvedToken(
                value=surface_val,
                source="figma" if surface_is_reliable else "config",
                confidence=1.0 if surface_is_reliable else 0.3,
            ),
            "primary": ResolvedToken(value=primary_val, source=primary_src, confidence=primary_confidence),
            "text_primary": ResolvedToken(
                value=text_colors.most_common(1)[0][0] if text_colors else c_def["text_primary"],
                source="figma" if text_colors else "config"),
            "text_secondary": ResolvedToken(
                value=text_colors.most_common(2)[1][0] if len(text_colors) > 1 else c_def["text_secondary"],
                source="figma" if len(text_colors) > 1 else "config"),
        }

        for key, default_value in c_def.items():
            if key not in resolved_colors:
                resolved_colors[key] = ResolvedToken(value=default_value, source="config")

        all_styles = [s for sub in fonts_by_family.values() for s in sub]
        primary_family = Counter(s.font_family for s in all_styles).most_common(1)[0][0] if all_styles else t_def[
            "primary_font"]
        unique_sizes = sorted(list({round(s.font_size) for s in all_styles if s.font_size}), reverse=True)

        def_scale = t_def.get("scale", {})

        def _token(role: str, size: float, weight: int) -> TypographyToken:
            """Создаёт `TypographyToken` с основным шрифтом и параметрами из конфига.

            Высота строки и межбуквенный интервал берутся из `defaults.json → typography.scale[role]`.

            Args:
                role: Роль типографики (`h1`, `body`, ...).
                size: Размер шрифта, px.
                weight: Вес шрифта.

            Returns:
                TypographyToken: Токен роли.
            """
            base = def_scale.get(role, {})
            return TypographyToken(
                family=primary_family,
                size=float(size),
                weight=weight,
                line_height_percent=float(base.get("line_height_percent", 120.0)),
                letter_spacing=float(base.get("letter_spacing", 0.0))
            )

        typography_tokens: dict[str, TypographyToken] = {}

        if unique_sizes:
            h1_size = float(unique_sizes[0])
            typo_rules = self.rules.get("typography_detection", {})
            h1_ratio = typo_rules.get("h1_ratio_threshold", 0.85)
            h2_ratio = typo_rules.get("h2_ratio_threshold", 0.65)
            h3_ratio = typo_rules.get("h3_ratio_threshold", 0.45)
            caption_max = typo_rules.get("caption_max_size", 14.0)

            buckets: dict[str, list[float]] = {"h1": [], "h2": [], "h3": [], "body": [], "caption": []}
            for sz in unique_sizes:
                ratio = sz / h1_size if h1_size else 0.0
                if sz <= caption_max:
                    buckets["caption"].append(sz)
                elif ratio >= h1_ratio:
                    buckets["h1"].append(sz)
                elif ratio >= h2_ratio:
                    buckets["h2"].append(sz)
                elif ratio >= h3_ratio:
                    buckets["h3"].append(sz)
                else:
                    buckets["body"].append(sz)

            body_bonus = self.pipeline_settings.get("typography_fallback", {}).get(
                "body_size_bonus_over_caption", 2.0
            )
            fallback_size = {"h1": h1_size, "h2": h1_size * h2_ratio, "h3": h1_size * h3_ratio,
                             "body": caption_max + body_bonus, "caption": caption_max}
            fallback_weight = {"h1": 700, "h2": 700, "h3": 600, "body": 400, "caption": 400}

            for role in ("h1", "h2", "h3", "body", "caption"):
                size = max(buckets[role]) if buckets[role] else fallback_size[role]
                typography_tokens[role] = _token(role, size, fallback_weight[role])

            typography_tokens["button"] = _token("button", typography_tokens["body"].size, 600)
            typography_tokens["stat"] = _token("stat", h1_size, 800)
        else:
            logger.warning("⚠️ Не найдено текстовых узлов с font_size — используются дефолты целиком.")
            for role, val in def_scale.items():
                typography_tokens[role] = _token(role, val["size"], val["weight"])

        button_radii = Counter(
            round(button.radius, 2)
            for button in buttons
            if button.radius > 0
        )

        if button_radii:
            btn_radius = button_radii.most_common(1)[0][0]
        elif radii:
            btn_radius = radii.most_common(1)[0][0]
        else:
            btn_radius = r_def["button"]

        card_radius = radii.most_common(1)[0][0] if radii else r_def["card"]

        tokens = DesignTokens(
            colors=resolved_colors,
            typography=typography_tokens,
            radii={"button": btn_radius, "card": card_radius, "badge": r_def["badge"]},
            spacing=self.defaults["spacing"]
        )

        logger.info(
            f"✓ Дизайн-система сформирована: Font='{primary_family}', Canvas={canvas_val}, Primary={primary_val}")
        return DesignSystemSpec(source=source_ref, tokens=tokens, components={"button": buttons})


# --- 4. SECTION BUILDER ---
class SectionBuilder:
    """Строит `SectionSpec`: индексирует дерево, скачивает ассеты и референс секции."""

    def __init__(self, client: Any, export_scale: int = 1, max_icon_dim: float = 56.0) -> None:
        """Сохраняет параметры и загружает индекс ассетов.

        Args:
            client: Клиент Figma API (метод `export_images`, атрибут `timeout`).
            export_scale: Масштаб экспорта PNG.
            max_icon_dim: Максимальная сторона узла, который считается иконкой, px.
        """
        self.client = client
        self.export_scale = export_scale
        self.max_icon_dim = max_icon_dim
        self.asset_manifest = load_asset_manifest()

    def _process_asset(self, node_id: str, url: str | None, asset_type: str, node_index: dict[str, IRNode],
                       timeout: float, assets_dir: Path, file_key: str) -> Asset | None:
        """Скачивает (или берёт из кэша) один ассет и записывает путь в узел.

        Порядок:
          1. Если индекс содержит `<file_key>::<node_id>` и файл на диске есть,
             используется он, сеть не нужна.
          2. Иначе, если `url` пуст, ассет пропускается (узел остаётся без визуала).
          3. Иначе файл скачивается и сохраняется как
             `<первые 12 символов SHA-256 содержимого>.<asset_type>`, запись
             добавляется в индекс.

        `file_key` в ключе индекса не даёт совпасть одинаковым `node_id` из
        разных Figma-файлов. В `node.asset_path` пишется `../../<путь ассета>`,
        то есть путь относительно `output/run_<timestamp>/`.

        Args:
            node_id: Идентификатор узла-ассета.
            url: Ссылка на файл из `export_images`; может быть `None`.
            asset_type: `"svg"` или `"png"`.
            node_index: Индекс `{id: IRNode}` секции.
            timeout: Таймаут скачивания, секунды.
            assets_dir: Каталог для сохранения файлов.
            file_key: Ключ Figma-файла.

        Returns:
            Asset | None: Описание ассета; `None`, если узла нет в индексе,
            нет ни кэша, ни URL или скачивание не удалось.
        """
        if node_id not in node_index:
            return None

        target_node = node_index[node_id]
        manifest_key = _manifest_key(file_key=file_key, node_id=node_id, )

        # 1. Уже знаем путь с прошлого успешного прогона — используем без сети
        cached_rel_path = self.asset_manifest.get(manifest_key)

        if cached_rel_path and (PROJECT_ROOT / cached_rel_path).exists():
            target_node.asset_path = f"../../{cached_rel_path}"

            logger.info(f"  ⚡ [АССЕТ-КЭШ] {file_key}:{node_id} уже скачан ранее: {cached_rel_path}")

            return Asset(
                id=node_id,
                name=target_node.name,
                asset_type=asset_type,
                path=cached_rel_path,
                geometry=target_node.rel_geometry.model_copy(),
                source_node_id=node_id,
            )

        # 2. Нет в манифесте и нет URL
        if not url:
            logger.warning(f"  ⚠️ Нет ни кэша, ни URL для ассета {file_key}:{node_id} ({target_node.name}) — "
                           f"узел останется без визуала.")
            return None

        # 3. Обычная сетевая загрузка + запись в manifest
        try:
            resp = requests.get(url, timeout=timeout, )
            resp.raise_for_status()
            content = resp.content
            content_hash = hashlib.sha256(content).hexdigest()[:12]
            fname = f"{content_hash}.{asset_type}"
            fpath = assets_dir / fname

            if not fpath.exists():
                fpath.write_bytes(content)

            rel_path = f"assets/{fname}"

            target_node.asset_path = f"../../{rel_path}"

            self.asset_manifest[manifest_key] = rel_path
            save_asset_manifest(self.asset_manifest)

            return Asset(
                id=node_id,
                name=target_node.name,
                asset_type=asset_type,
                path=rel_path,
                geometry=target_node.rel_geometry.model_copy(),
                source_node_id=node_id,
            )

        except Exception as e:
            logger.warning(
                f"  Не удалось скачать ассет "
                f"{file_key}:{node_id}: {e}"
            )
            return None

    def build(
            self,
            root_ir: IRNode,
            source_ref: SourceRef,
            transform: TransformSpec,
            section_id: str,
            section_name: str | None = None,
            assets_dir: Path | None = None,
            project_root: Path | None = None,
    ) -> SectionSpec:
        """Собирает `SectionSpec` для одной секции.

        Шаги: индексация узлов и текстов; разметка ассетов
        (`AssetClassifier.classify_and_mark`); скачивание SVG; скачивание PNG и
        референса секции (`ref_<хэш id>.png`, существующий файл не
        перекачивается); оценка адаптивности: секция `responsive_capable`, если
        больше половины узлов привязаны через `scale`, `stretch` или `center`.

        Args:
            root_ir: Нормализованное дерево секции.
            source_ref: Ссылка на источник секции.
            transform: Политика переноса стиля секции.
            section_id: Идентификатор секции.
            section_name: Название; по умолчанию имя корневого узла.
            assets_dir: Каталог ассетов; по умолчанию `ASSETS_DIR`.
            project_root: Корень проекта для относительного пути референса;
                по умолчанию `PROJECT_ROOT`.

        Returns:
            SectionSpec: Секция с деревом, текстами, ассетами и путём к
            референсу (`reference_image` пуст, если референс получить не удалось).
        """
        logger.info(f"Построение секции '{section_name or root_ir.name}' (id: {section_id})...")

        # Определяем локальные пути с фоллбэком на глобальные константы
        _assets_dir = assets_dir or globals().get("ASSETS_DIR", Path("assets"))
        _project_root = project_root or globals().get("PROJECT_ROOT", Path("."))

        texts: dict[str, dict[str, Any]] = {}
        node_index: dict[str, IRNode] = {}
        ref_path: str | None = None

        def index_and_collect(n: IRNode) -> None:
            """Заполняет `node_index` и `texts` для поддерева.

            Args:
                n: Текущий узел.
            """
            node_index[n.id] = n
            if n.type == "TEXT":
                texts[n.id] = {
                    "text": n.characters or "",
                    "font_size": n.style.font_size,
                    "font_weight": n.style.font_weight,
                    "name": n.name
                }
            for ch in n.children:
                index_and_collect(ch)

        index_and_collect(root_ir)
        logger.info(f"Индексация завершена: {len(node_index)} узлов, {len(texts)} текстовых элементов")

        svg_ids, png_ids = AssetClassifier.classify_and_mark(root_ir, max_icon_dim=self.max_icon_dim)
        assets: list[Asset] = []
        timeout = getattr(self.client, "timeout", 30)

        # 1. Скачивание и дедупликация SVG ассетов
        if svg_ids:
            logger.info(f"Скачивание {len(svg_ids)} SVG ассетов...")
            svg_res = self.client.export_images(source_ref.resource_id, svg_ids, scale=1, fmt="svg")
            svg_urls = svg_res.get("images", {})

            for a_id in svg_ids:
                url = svg_urls.get(a_id)
                asset = self._process_asset(
                    node_id=a_id,
                    url=url,
                    asset_type="svg",
                    node_index=node_index,
                    timeout=timeout,
                    assets_dir=_assets_dir,
                    file_key=source_ref.resource_id,
                )
                if asset:
                    assets.append(asset)

        # 2. Скачивание и дедупликация PNG ассетов + Запрос референса секции
        all_png_ids = list(set(png_ids + ([source_ref.node_id] if source_ref.node_id else [])))
        if all_png_ids:
            logger.info(f"Скачивание {len(png_ids)} PNG ассетов + референс секции...")
            png_res = self.client.export_images(source_ref.resource_id, all_png_ids, scale=self.export_scale, fmt="png")
            png_urls = png_res.get("images", {})

            # Обработка PNG иконок/изображений
            for a_id in set(png_ids):
                url = png_urls.get(a_id)
                asset = self._process_asset(
                    node_id=a_id,
                    url=url,
                    asset_type="png",
                    node_index=node_index,
                    timeout=timeout,
                    assets_dir=_assets_dir,
                    file_key=source_ref.resource_id,
                )
                if asset:
                    assets.append(asset)

            # 3. Сохранение эталонного скриншота (референса) секции
            if source_ref.node_id:
                ref_id = normalize_figma_id(source_ref.node_id)
                ref_fname = f"ref_{hashlib.md5(ref_id.encode()).hexdigest()[:8]}.png"
                ref_fpath = _assets_dir / ref_fname

                if not ref_fpath.exists():
                    ref_url = png_urls.get(ref_id)
                    if ref_url:
                        try:
                            resp = requests.get(ref_url, timeout=timeout)
                            resp.raise_for_status()
                            ref_fpath.write_bytes(resp.content)
                            logger.info(f"✓ Скачан эталонный референс секции: {ref_fname}")
                        except Exception as e:
                            logger.error(f"❌ Не удалось сохранить референс секции {ref_id}: {e}")
                    else:
                        logger.warning(
                            f"⚠️ Figma API не вернул export-URL для референса секции {ref_id} "
                            f"в этом запуске, а локального кэша {ref_fname} тоже нет — "
                            "визуальное сравнение для этой секции будет невозможно."
                        )

                if ref_fpath.exists():
                    try:
                        ref_path = str(ref_fpath.relative_to(_project_root))
                    except ValueError:
                        ref_path = str(ref_fpath)
                else:
                    logger.error(
                        f"❌ Референс секции {ref_id} отсутствует и не может быть получен "
                        f"(нет ни кэша, ни API-ответа). reference_image останется пустым."
                    )

        # Секция считается адаптивной, если больше половины узлов привязаны через scale/stretch/center
        adaptive_count = 0
        total_count = 0
        for node in node_index.values():
            total_count += 1
            if node.layout.h_constraint in ("scale", "stretch", "center") or \
                    node.layout.v_constraint in ("scale", "stretch", "center"):
                adaptive_count += 1
        responsive_capable = (total_count > 0) and (adaptive_count / total_count > 0.5)
        logger.info(
            f"  Адаптивность секции: {adaptive_count}/{total_count} узлов с scale/stretch/center "
            f"({'РЕСПОНСИВНАЯ' if responsive_capable else 'FIXED, будет масштабироваться целиком'})"
        )

        logger.info(f"✓ Секция '{section_name or root_ir.name}' готова (ассетов сохранено: {len(assets)})")
        return SectionSpec(
            id=section_id,
            name=section_name or root_ir.name,
            source=source_ref,
            geometry=root_ir.rel_geometry,
            root_node=root_ir,
            texts=texts,
            assets=assets,
            reference_image=ref_path,
            transform=transform,
            responsive_capable=responsive_capable,
        )


# --- 5. PIPELINE EXECUTION ---
def run_extraction(manifest_path: Path, force: bool = False) -> None:
    """Извлекает дизайн-систему и секции из Figma и сохраняет spec-файл.

    Этапы:
      1. Дизайн-система: узел `style_source` (IR, разметка ассетов, PNG-референс
         для сэмплинга цвета, `StyleBuilder.build`).
      2. Секции: для каждой секции манифеста `SectionBuilder.build`.
    Результат записывается в `SPECS_DIR/<имя манифеста>_spec.json`:
    `style_spec`, `sections`, а также разделы `render`, `qa`, `llm` манифеста.

    `force` заставляет заново запросить данные Figma API вместо локального
    JSON-кэша. Уже скачанные ассеты и референсы он не перекачивает.

    Args:
        manifest_path: Путь к манифесту лендинга.
        force: Принудительно обновить кэш ответов Figma API.

    Raises:
        KeyError: Если в манифесте нет обязательных ключей.
        ValueError: Если нет `FIGMA_TOKENS` или `node_id` источника стиля/секции.
        PermissionError: Токены Figma в кулдауне, кэша для отката нет.
        RuntimeError: Запрос к Figma API не удался после всех попыток.
    """
    t_start = time.perf_counter()
    logger.info("=" * 60)
    logger.info(f"🚀 СТАРТ ЭКСТРАКЦИИ ПО МАНИФЕСТУ: {manifest_path.name}")
    logger.info("=" * 60)

    manifest = ConfigLoader.load(manifest_path)
    raw_tokens = env.list("FIGMA_TOKENS")
    tokens: list[str] = [t for t in raw_tokens if t]
    client = FigmaClient(tokens=tokens, config=DEFAULTS, pipeline_settings=PIPELINE_SETTINGS)
    scale = DEFAULTS["figma"]["export_scale"]
    max_icon_dim = ANALYSIS_RULES.get("asset_detection", {}).get("max_icon_dimension", 56.0)

    logger.info("\n--- ЭТАП 1/2: ИЗВЛЕЧЕНИЕ ДИЗАЙН-СИСТЕМЫ ---")
    s_cfg = manifest["style_source"]
    s_ref = SourceRef(provider=s_cfg.get("provider", "figma"), resource_id=s_cfg["file_key"], node_id=s_cfg["node_id"])
    style_node_id: str | None = s_ref.node_id
    if not style_node_id:
        raise ValueError(
            "Поле style_source.node_id обязательно в манифесте — стиль не может быть извлечён без исходного узла.")

    s_raw = client.get_nodes(s_ref.resource_id, [style_node_id], force=force)
    s_doc = s_raw["nodes"][normalize_figma_id(style_node_id)]["document"]

    style_ir = FigmaNormalizer.normalize_node(s_doc)

    # Разметка render_strategy нужна фильтру надёжности фона (passes_bg_reliability_filter); id для экспорта не используются.
    AssetClassifier.classify_and_mark(style_ir, max_icon_dim=max_icon_dim)

    style_ref_fname = f"style_ref_{hashlib.md5(style_node_id.encode()).hexdigest()[:8]}.png"
    style_ref_png_path: Path = ASSETS_DIR / style_ref_fname
    resolved_style_ref_path: Path | None = None

    if style_ref_png_path.exists():
        logger.info(f"⚡ [АССЕТ-КЭШ] PNG-референс style_source уже на диске: {style_ref_fname}")
        resolved_style_ref_path = style_ref_png_path
    else:
        try:
            style_png_res = client.export_images(s_ref.resource_id, [style_node_id], scale=scale, fmt="png")
            style_png_url = style_png_res.get("images", {}).get(normalize_figma_id(style_node_id))
            if style_png_url:
                resp = requests.get(style_png_url, timeout=DEFAULTS["figma"]["timeout_seconds"])
                resp.raise_for_status()
                style_ref_png_path.write_bytes(resp.content)
                logger.info(f"✓ Скачан PNG-референс style_source: {style_ref_fname}")
                resolved_style_ref_path = style_ref_png_path
        except Exception as inner_err:
            err_str = str(inner_err)
            logger.warning(
                f"⚠️ Не удалось скачать PNG-референс style_source ({err_str}). Сэмплинг цвета будет пропущен.")

    style_spec = StyleBuilder(DEFAULTS, ANALYSIS_RULES, PIPELINE_SETTINGS).build(
        style_ir, s_ref, reference_image_path=resolved_style_ref_path
    )

    sections_cfg = manifest.get("sections", [])
    logger.info(f"\n--- ЭТАП 2/2: ИЗВЛЕЧЕНИЕ СЕКЦИЙ (Всего: {len(sections_cfg)}) ---")
    sections = []
    section_builder = SectionBuilder(client, export_scale=scale, max_icon_dim=max_icon_dim)

    for idx, sec_cfg in enumerate(sections_cfg, 1):
        logger.info(f"\n[{idx}/{len(sections_cfg)}] Обработка секции: {sec_cfg.get('name', sec_cfg['id'])}")
        src_cfg = sec_cfg["source"]
        src_ref = SourceRef(provider=src_cfg.get("provider", "figma"), resource_id=src_cfg["file_key"],
                            node_id=src_cfg["node_id"])

        transform = TransformSpec.model_validate(sec_cfg.get("transform", {}))

        section_node_id: str | None = src_ref.node_id
        if not section_node_id:
            raise ValueError(f"Секция '{sec_cfg.get('id')}' не имеет node_id в манифесте.")

        c_raw = client.get_nodes(src_ref.resource_id, [section_node_id], force=force)
        c_doc = c_raw["nodes"][normalize_figma_id(section_node_id)]["document"]
        sec_ir = FigmaNormalizer.normalize_node(c_doc)

        section_spec = section_builder.build(
            root_ir=sec_ir,
            source_ref=src_ref,
            transform=transform,
            section_id=sec_cfg["id"],
            section_name=sec_cfg.get("name")
        )
        sections.append(section_spec)

    manifest_stem = manifest_path.stem
    out_path = SPECS_DIR / f"{manifest_stem}_spec.json"
    landing_data = {
        "manifest_path": str(manifest_path.relative_to(PROJECT_ROOT)),
        "project_name": manifest.get("project_name", "PoC Landing"),
        "style_spec": style_spec.model_dump(),
        "sections": [s.model_dump() for s in sections],
        "render": manifest.get("render", {}),
        "qa": manifest.get("qa", {}),
        "llm": manifest.get("llm", {})
    }
    out_path.write_text(json.dumps(landing_data, indent=2, ensure_ascii=False), encoding="utf-8")

    total_time = time.perf_counter() - t_start
    logger.info("=" * 60)
    logger.info(f"✨ ЭКСТРАКЦИЯ УСПЕШНО ЗАВЕРШЕНА ЗА {total_time:.2f} сек!")
    logger.info("=" * 60)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="configs/landing_manifest.json", help="Путь к манифесту")
    parser.add_argument("--force", action="store_true", help="Принудительно обновить кэш Figma")
    args = parser.parse_args()
    run_extraction(PROJECT_ROOT / args.config, force=args.force)
