# python_style_extractor/scripts/color_resolution.py

"""Цветовые утилиты pipeline «Figma → Landing».

Модуль — единый источник правил, общих для `figma_extractor.py` и
`generate_landing.py` (чтобы не синхронизировать их вручную в двух местах):

    * разбор и преобразование цветов (`rgba(...)`, hex, модель HLS);
    * перекраска (retint) цветов под акцентный цвет дизайн-системы;
    * выбор акцентного токена и контрастного цвета текста;
    * оценка «надёжности» узла как источника фонового цвета и сбор кандидатов
      на canvas/surface (используют `StyleBuilder` и `ReferenceRenderer`);
    * сэмплинг цветов из PNG: доминирующие цвета, акцент ассета, цвет под
      прозрачностью.
"""

import colorsys
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from scripts.models import IRNode, normalize_color_to_rgba
from scripts.models import ResolvedToken

logger = logging.getLogger(__name__)

DECORATIVE_RAW_TYPES_DEFAULT = ("VECTOR", "BOOLEAN_OPERATION", "STAR", "LINE", "REGULAR_POLYGON", "ELLIPSE")

# Запасное значение для `reference_bg_detection.min_alpha` на случай, если ключ
# отсутствует в `analysis_rules.json`.
DEFAULT_MIN_BG_ALPHA = 0.4


def alpha_from_rgba(color: str) -> float:
    """Возвращает альфа-канал цвета из строки `rgba(...)`.

    Значение используется как множитель уверенности: полупрозрачный узел
    вносит меньший вес в оценку «это настоящий сплошной фон».

    Args:
        color: Цвет в формате `rgba(r, g, b, a)`.

    Returns:
        float: Альфа-канал; `1.0`, если строка пустая, не начинается с `rgba`
        (hex, `rgb(...)`) или альфу не удалось разобрать.
    """
    if not color or not color.startswith("rgba"):
        return 1.0
    try:
        return float(color.rstrip(")").split(",")[-1])
    except (ValueError, IndexError):
        return 1.0


def parse_rgba_channels(rgba_str: str) -> tuple[float, float, float, float] | None:
    """Извлекает каналы `(r, g, b, a)` из строки цвета.

    Строка сначала проходит через `normalize_color_to_rgba` (единственное место
    с правилом перевода hex в rgba, см. `scripts/models.py`; ту же нормализацию
    `ResolvedToken` применяет к токенам). Собственной арифметики перевода hex
    здесь нет: правило конвертации меняется в одном месте.

    Args:
        rgba_str: Цвет, например `"rgba(38, 42, 84, 0.4)"` или hex.

    Returns:
        tuple[float, float, float, float] | None: `r`, `g`, `b` в диапазоне
        0..1, `a` как в строке (0..1). `None` для пустой или нераспознанной
        строки (например, `"transparent"`): вызывающий код должен обработать
        этот случай, а не упасть.
    """
    if not rgba_str:
        return None
    s = normalize_color_to_rgba(rgba_str.strip())
    if not s.startswith("rgba("):
        return None
    try:
        inner = s[len("rgba("):-1]
        parts = [p.strip() for p in inner.split(",")]
        if len(parts) != 4:
            return None
        r, g, b = (float(p) / 255.0 for p in parts[:3])
        a = float(parts[3])
        return r, g, b, a
    except (ValueError, IndexError):
        return None


def retint_bg_color_toward_token(
        node: IRNode,
        target_token: str | None,
        preserve_source_saturation: bool = False,
) -> bool:
    """Перекрашивает `node.style.bg_color` в сторону целевого токена.

    Объединяет связку «`compute_retint_filter` -> `apply_retint_to_rgba` ->
    `retinted=True`», общую для веток button, card и badge в
    `StyleTransferEngine.apply_component_style`. В отличие от
    `StyleTransferEngine.apply_retint_to_native_decoration`, светлота не
    меняется (`brightness=1.0`): для заливок компонентов нужен сдвиг оттенка,
    а при `preserve_source_saturation=True` ещё и сохранение насыщенности
    источника. Изменяет `node.style` на месте.

    Args:
        node: Узел, чья заливка перекрашивается.
        target_token: Целевой цвет (`rgba(...)` или hex); `None`, если токен не
            определён.
        preserve_source_saturation: Если `True`, к насыщенности применяется
            коэффициент `saturate` из `compute_retint_filter`; иначе насыщенность
            не меняется.

    Returns:
        bool: `True`, если перекраска применена (обновлены `bg_color` и
        `retinted`). `False`, если `bg_color` пуст или прозрачен либо
        `compute_retint_filter` не нашёл подходящего преобразования (см.
        `min_saturation`). Fallback при `False` выбирает вызывающий код.
    """
    if not node.style.bg_color or node.style.bg_color == "transparent":
        return False
    filt = compute_retint_filter(node.style.bg_color, target_token)
    if filt is None:
        return False
    saturate = filt["saturate"] if preserve_source_saturation else 1.0
    node.style.bg_color = apply_retint_to_rgba(
        node.style.bg_color, filt["hue_deg"], saturate, 1.0,
    )
    node.style.retinted = True
    return True


def compute_retint_filter(
        source_rgba: str | None,
        target_rgba: str | None,
        min_saturation: float = 0.15,
) -> dict[str, float] | None:
    """Считает параметры перекраски исходного цвета в целевой.

    Чистый `hue-rotate` сохраняет насыщенность и светлоту источника. Если у
    целевого акцента они заметно другие, результат одного сдвига оттенка
    выглядит тусклым или «кислотным». Поэтому помимо угла считаются коэффициенты
    для `saturate()` и `brightness()`. Все значения вычисляются из HLS-компонент
    обоих цветов, а не подбираются под конкретный макет:

      * `hue_deg` — `(целевой оттенок - исходный) * 360`, по модулю 360;
      * `saturate` — отношение насыщенностей, ограничено диапазоном 0.3..3.0;
      * `brightness` — отношение светлот, ограничено диапазоном 0.5..1.8
        (`1.0`, если светлота источника не больше 0.01).

    Args:
        source_rgba: Исходный цвет; `None` допустим.
        target_rgba: Целевой цвет; `None` допустим.
        min_saturation: Минимальная насыщенность (HLS) обоих цветов. Серые
            цвета не перекрашиваются.

    Returns:
        dict[str, float] | None: Словарь с ключами `hue_deg`, `saturate`,
        `brightness`. `None`, если любой из цветов не разобран либо его
        насыщенность меньше `min_saturation`.
    """
    src = parse_rgba_channels(source_rgba) if source_rgba else None
    dst = parse_rgba_channels(target_rgba) if target_rgba else None
    if src is None or dst is None:
        return None

    src_h, src_l, src_s = colorsys.rgb_to_hls(src[0], src[1], src[2])
    dst_h, dst_l, dst_s = colorsys.rgb_to_hls(dst[0], dst[1], dst[2])

    if src_s < min_saturation or dst_s < min_saturation:
        return None

    hue_deg = round(((dst_h - src_h) * 360.0) % 360.0, 1)
    saturate_ratio = round(dst_s / max(src_s, 0.01), 2)
    brightness_ratio = round(dst_l / max(src_l, 0.01), 2) if src_l > 0.01 else 1.0

    # Общий предел коэффициентов CSS-фильтра: защищает от крайних случаев
    # (например, почти чёрный исходный акцент), когда коэффициент получился бы
    # огромным.
    saturate_ratio = max(0.3, min(saturate_ratio, 3.0))
    brightness_ratio = max(0.5, min(brightness_ratio, 1.8))

    return {"hue_deg": hue_deg, "saturate": saturate_ratio, "brightness": brightness_ratio}


def apply_retint_to_rgba(
        rgba_str: str,
        hue_deg: float,
        saturate_ratio: float = 1.0,
        brightness_ratio: float = 1.0,
) -> str:
    """Применяет сдвиг оттенка, насыщенности и светлоты к цвету `rgba(...)`.

    Используется для native-декораций (сплошная заливка формы: кружок-подложка,
    точка-акцент) и цветов теней. Для растровых ассетов перекраска идёт через
    CSS-фильтр на `<img>`, а здесь браузер должен сразу получить готовый цвет в
    `background-color`: фильтр на самой форме каскадно затронул бы и её
    дочерние узлы. Параметры соответствуют полям результата
    `compute_retint_filter`; расчёт идёт в модели HLS.

    Args:
        rgba_str: Исходный цвет.
        hue_deg: Угол сдвига оттенка, градусы.
        saturate_ratio: Коэффициент насыщенности; результат ограничен 0..1.
        brightness_ratio: Коэффициент светлоты; результат ограничен 0..1.

    Returns:
        str: Новая строка `rgba(...)` с исходной альфой; исходная строка без
        изменений, если цвет не удалось разобрать.
    """
    channels = parse_rgba_channels(rgba_str)
    if channels is None:
        return rgba_str
    r, g, b, a = channels

    hue, lightness, sat = colorsys.rgb_to_hls(r, g, b)
    hue = (hue + hue_deg / 360.0) % 1.0
    sat = max(0.0, min(sat * saturate_ratio, 1.0))
    lightness = max(0.0, min(lightness * brightness_ratio, 1.0))

    new_r, new_g, new_b = colorsys.hls_to_rgb(hue, lightness, sat)
    return (
        f"rgba({round(new_r * 255)}, {round(new_g * 255)}, "
        f"{round(new_b * 255)}, {a})"
    )


def apply_hue_rotate_to_rgba(source_rgba: str, hue_deg: float) -> str:
    """Поворачивает оттенок цвета на `hue_deg` градусов.

    Сохраняет светлоту, насыщенность и альфу. Поворот выполняется в модели HLS
    непосредственно над числовым значением цвета, поэтому результат не совпадает
    пиксель в пиксель с CSS-фильтром `hue-rotate()`, который работает иначе.

    Args:
        source_rgba: Исходный цвет.
        hue_deg: Угол поворота оттенка, градусы.

    Returns:
        str: Новая строка `rgba(...)`; исходная строка без изменений, если цвет
        не удалось разобрать.
    """
    parsed = parse_rgba_channels(source_rgba)
    if parsed is None:
        return source_rgba
    r, g, b, a = parsed
    h, l, s = colorsys.rgb_to_hls(r, g, b)
    new_h = (h + hue_deg / 360.0) % 1.0
    nr, ng, nb = colorsys.hls_to_rgb(new_h, l, s)
    return f"rgba({round(nr * 255)}, {round(ng * 255)}, {round(nb * 255)}, {a})"


def rgba_lightness(rgba_str: str | None) -> float | None:
    """Возвращает светлоту цвета в модели HLS без учёта альфы.

    Общая точка сравнения «светлее/темнее» между цветами: та же модель HLS, что
    использует `compute_retint_filter`, поэтому второго способа измерять яркость
    не нужно.

    Args:
        rgba_str: Цвет в формате `rgba(...)` или hex; `None` допустим.

    Returns:
        float | None: Светлота 0..1; `None`, если цвет не задан или не разобран.
    """
    channels = parse_rgba_channels(rgba_str) if rgba_str else None
    if channels is None:
        return None
    r, g, b, _a = channels
    _h, l, _s = colorsys.rgb_to_hls(r, g, b)
    return l


def resolve_reliable_accent_token(
        color_tokens: dict[str, ResolvedToken],
        min_confidence: float = 0.6,
        preferred_order: tuple[str, ...] = ("primary", "accent", "secondary"),
) -> tuple[str | None, str]:
    """Выбирает токен, по которому управляется перекраска и заливка CTA.

    Токену нельзя доверять только по имени (`primary`): учитывается `confidence`,
    рассчитанная в `figma_extractor.py`. Низкая `confidence` означает, что цвет
    извлечён не из настоящей кнопки, а из произвольного декоративного кандидата.

    Токены перебираются в порядке `preferred_order`; возвращается первый с
    `confidence >= min_confidence`. Если такого нет, возвращается первый из
    найденных токенов (в лог пишется предупреждение): деградация мягкая, без
    исключения. `preferred_order` и `min_confidence` настраиваются и не привязаны
    к конкретному проекту.

    Args:
        color_tokens: Цветовые токены дизайн-системы.
        min_confidence: Минимальная `confidence`, при которой токен считается
            надёжным.
        preferred_order: Порядок предпочтения ролей токена.

    Returns:
        tuple[str | None, str]: `(значение, использованный ключ)`. Если ни одного
        токена из `preferred_order` нет, `(None, первый ключ из preferred_order)`
        либо `(None, "")` при пустом `preferred_order`. Вызывающий код обрабатывает
        `None` так же, как в `compute_retint_filter`.
    """
    best_fallback_key: str | None = None
    best_fallback_val: str | None = None

    for key in preferred_order:
        token = color_tokens.get(key)
        if token is None:
            continue
        if best_fallback_val is None:
            best_fallback_key, best_fallback_val = key, str(token.value)
        if token.confidence >= min_confidence:
            return str(token.value), key

    if best_fallback_val is not None:
        logger.warning(
            f"⚠️ Ни один из токенов {preferred_order} не набрал min_confidence={min_confidence:.2f} — "
            f"используется наименее ненадёжный доступный кандидат '{best_fallback_key}' "
            f"(confidence={color_tokens[best_fallback_key].confidence:.2f}). Стоит проверить источник этого токена "
            "в Figma-референсе."
        )
        return best_fallback_val, best_fallback_key

    return None, preferred_order[0] if preferred_order else ""


def pick_lightest_token(color_tokens: dict[str, str], background_rgba: str | None = None) -> str:
    """Выбирает из токенов цвет, который будет светлее всех поверх заданного фона.

    Сравнивается не «сырая» светлота токена, а эффективная после
    альфа-композитинга поверх фона:

        effective_L = a * L(token) + (1 - a) * L(bg)

    Так полупрозрачный токен вроде `rgba(255, 255, 255, 0.08)` не считается
    «самым светлым»: поверх тёмного фона он почти не виден, и заливка кнопки им
    была бы практически прозрачной. Формула не зависит от имён токенов и canvas.

    Args:
        color_tokens: Цветовые токены `{имя: значение}`; значения, которые не
            удалось разобрать, пропускаются.
        background_rgba: Цвет фона. Если не задан или не разобран, светлота фона
            принимается равной 0.

    Returns:
        str: Исходное значение самого светлого токена; `"rgba(255, 255, 255, 1.0)"`,
        если ни один токен не разобран.
    """
    bg_l = rgba_lightness(background_rgba) if background_rgba else 0.0
    if bg_l is None:
        bg_l = 0.0

    best_val: str | None = None
    best_eff_l = -1.0
    for val in color_tokens.values():
        channels = parse_rgba_channels(val) if val else None
        if channels is None:
            continue
        r, g, b, a = channels
        _h, l, _s = colorsys.rgb_to_hls(r, g, b)
        eff_l = a * l + (1.0 - a) * bg_l
        if eff_l > best_eff_l:
            best_val, best_eff_l = val, eff_l

    return best_val if best_val is not None else "rgba(255, 255, 255, 1.0)"


def pick_contrasting_text_color(background_rgba: str | None, color_tokens: dict[str, str]) -> str:
    """Выбирает читаемый цвет текста для заданного фона.

    Заменяет фиксированную привязку по роли (`role_color_map["button_label"]`),
    которая ломается, когда фон элемента оказывается светлым (например, заливка
    кнопки динамически стала светлой, см. `pick_lightest_token`). Из двух
    кандидатов, самого тёмного (токен `canvas`) и самого светлого
    (`pick_lightest_token`), выбирается тот, у которого больше разница светлоты
    (HLS) с фоном; при равенстве выбирается светлый. Конкретные цвета в коде не
    зашиты, поэтому правило работает для любого набора токенов.

    Args:
        background_rgba: Цвет фона; `None` допустим.
        color_tokens: Цветовые токены `{имя: значение}`. Используются `canvas` и
            `text_primary`.

    Returns:
        str: Цвет текста. Если фон неизвестен или прозрачен, токен
        `text_primary` (по умолчанию `"rgba(255, 255, 255, 1.0)"`).
    """
    bg_l = rgba_lightness(background_rgba)
    if bg_l is None:
        # Фон неизвестен или прозрачен: берём text_primary (по умолчанию светлый,
        # безопасный вариант для тёмных тем).
        return str(color_tokens.get("text_primary", "rgba(255, 255, 255, 1.0)"))

    dark_candidate = str(color_tokens.get("canvas", "rgba(0, 0, 0, 1.0)"))
    light_candidate = pick_lightest_token(color_tokens, color_tokens.get("canvas"))

    dark_l = rgba_lightness(dark_candidate)
    light_l = rgba_lightness(light_candidate)
    dark_l = dark_l if dark_l is not None else 0.0
    light_l = light_l if light_l is not None else 1.0

    return light_candidate if abs(light_l - bg_l) >= abs(bg_l - dark_l) else dark_candidate


def color_distance_hls(color_a: str | None, color_b: str | None) -> float:
    """Оценивает, насколько два цвета непохожи, в модели HLS.

    Грубая перцептивная мера: `|ΔL| * 3 + |ΔS| + Δhue`, где `Δhue` — кратчайшее
    расстояние по кругу оттенков. Светлота взвешена сильнее: для вопроса «видна
    ли граница панели на фоне canvas» разница светлот обычно важнее разницы
    оттенков.

    Args:
        color_a: Первый цвет; `None` допустим.
        color_b: Второй цвет; `None` допустим.

    Returns:
        float: Расстояние. `999.0`, если любой из цветов не разобран: вызывающий
        код должен трактовать это как «неизвестно, различимы ли» и не блокировать
        по такому сравнению.
    """
    a = parse_rgba_channels(color_a) if color_a else None
    b = parse_rgba_channels(color_b) if color_b else None
    if a is None or b is None:
        return 999.0
    ha, la, sa = colorsys.rgb_to_hls(a[0], a[1], a[2])
    hb, lb, sb = colorsys.rgb_to_hls(b[0], b[1], b[2])
    hue_diff = min(abs(ha - hb), 1.0 - abs(ha - hb))
    return abs(la - lb) * 3.0 + abs(sa - sb) + hue_diff


def parse_color_to_rgb_tuple(value: Any) -> tuple[int, int, int] | None:
    """Разбирает значение цветового токена в `(r, g, b)`.

    Токен может быть hex (`#rrggbb` из `defaults.json`) или `rgba(r, g, b, a)`
    (из Figma-экстракции); формат заранее неизвестен, поэтому поддерживаются оба.
    Для hex берутся первые 6 символов, поэтому `#rrggbbaa` тоже принимается
    (альфа игнорируется), а короткая запись `#rgb` не поддерживается. Результат
    используется как `composite_bg` в `sample_dominant_colors_from_png`.

    Args:
        value: Значение токена; не строка допустима.

    Returns:
        tuple[int, int, int] | None: Каналы 0..255; `None`, если значение не
        строка или не разобрано.
    """
    if not isinstance(value, str):
        return None
    v = value.strip()
    if v.startswith("#"):
        hexs = v.lstrip("#")
        if len(hexs) >= 6:
            try:
                return int(hexs[0:2], 16), int(hexs[2:4], 16), int(hexs[4:6], 16)
            except ValueError:
                return None
        return None
    if v.startswith("rgba") or v.startswith("rgb"):
        try:
            parts = v[v.index("(") + 1: v.index(")")].split(",")
            r, g, b = (int(round(float(p.strip()))) for p in parts[:3])
            return r, g, b
        except (ValueError, IndexError):
            return None
    return None


def passes_bg_reliability_filter(node: IRNode, cfg: dict) -> bool:
    """Проверяет, что узел вообще годится как кандидат на роль фона.

    Узел отсекается, если:
      * его `type` входит в `exclude_node_types` (по умолчанию `ASSET`);
      * исходный тип Figma (`extras["raw_type"]`) входит в `exclude_raw_types`
        (по умолчанию декоративные векторные типы);
      * режим наложения не входит в `allowed_blend_modes` (по умолчанию
        `NORMAL`, `PASS_THROUGH`);
      * альфа `bg_color` меньше `min_alpha` (по умолчанию `DEFAULT_MIN_BG_ALPHA`).

    Порог альфы — жёсткий отсекающий критерий наравне с типом и режимом
    наложения, а не только понижающий множитель в `bg_reliability_weight`:
    почти прозрачная «стеклянная» панель не может быть фоном, даже если занимает
    большую часть секции. Узел без `bg_color` проверку альфы проходит (альфа
    считается равной 1.0): наличие заливки проверяет вызывающий код.

    Args:
        node: Проверяемый узел.
        cfg: Раздел `reference_bg_detection` из `analysis_rules.json`.

    Returns:
        bool: `True`, если узел может быть кандидатом на фон.
    """
    exclude_types = set(cfg.get("exclude_node_types", ["ASSET"]))
    exclude_raw_types = set(cfg.get("exclude_raw_types", list(DECORATIVE_RAW_TYPES_DEFAULT)))
    allowed_blend = set(cfg.get("allowed_blend_modes", ["NORMAL", "PASS_THROUGH"]))
    min_alpha = float(cfg.get("min_alpha", DEFAULT_MIN_BG_ALPHA))

    if node.type in exclude_types:
        return False
    raw_type = node.extras.get("raw_type") if isinstance(node.extras, dict) else None
    if raw_type in exclude_raw_types:
        return False
    if node.style.blend_mode not in allowed_blend:
        return False
    if alpha_from_rgba(node.style.bg_color or "") < min_alpha:
        return False
    return True


def bg_reliability_weight(node: IRNode, cfg: dict) -> float:
    """Считает множитель веса кандидата на фон среди прошедших фильтр.

    Применяется к узлам, уже прошедшим `passes_bg_reliability_filter` (альфа не
    меньше `min_alpha`). Вес равен произведению штрафа за градиентную
    аппроксимацию (цвет такого узла — лишь средняя точка градиента) и остаточной
    альфы `bg_color`.

    Args:
        node: Кандидат на роль фона.
        cfg: Раздел `reference_bg_detection` из `analysis_rules.json`
            (`gradient_penalty`, по умолчанию 0.6).

    Returns:
        float: Множитель веса.
    """
    gradient_penalty = float(cfg.get("gradient_penalty", 0.6))
    grad_mult = gradient_penalty if node.style.bg_is_gradient_approx else 1.0
    return grad_mult * alpha_from_rgba(node.style.bg_color or "")


def sample_dominant_colors_from_png(
        png_path: Path,
        k: int,
        resize_dim: int,
        min_alpha: float = 0.5,
        composite_bg: tuple[int, int, int] | None = None,
) -> list[str]:
    """Сэмплирует `k` самых частых цветов из PNG-референса секции или стиля.

    Изображение читается как RGBA, а не через `convert("RGB")`: Figma
    экспортирует секции без фона с прозрачными областями `RGBA(0, 0, 0, 0)`, и
    после `convert("RGB")` такие пиксели стали бы чёрными и побеждали бы в
    подсчёте частот. Поэтому:

      1. Изображение приводится к `resize_dim` x `resize_dim`.
      2. Пиксели с альфой меньше `min_alpha` исключаются: они не несут
         достоверной информации о цвете дизайна.
      3. Если задан `composite_bg`, оставшиеся полупрозрачные пиксели
         композитятся поверх него (`alpha * fg + (1 - alpha) * bg`): это точнее
         «сырого» цвета пикселя.
      4. Если пикселей не осталось, возвращается пустой список. Вызывающий код
         переходит на следующий уровень fallback; «нет данных» не подменяется
         угадыванием.

    Args:
        png_path: Путь к PNG.
        k: Сколько цветов вернуть.
        resize_dim: Сторона квадрата, к которому приводится изображение, px.
        min_alpha: Минимальная альфа (0..1), с которой пиксель учитывается.
        composite_bg: Цвет `(r, g, b)`, поверх которого композитятся
            полупрозрачные пиксели; `None` — без композитинга.

    Returns:
        list[str]: До `k` цветов `rgba(r, g, b, 1.0)` по убыванию частоты. Пустой
        список, если файл не удалось открыть или надёжных пикселей нет (в лог
        пишется предупреждение).
    """
    import numpy as np
    from PIL import Image
    try:
        img = Image.open(png_path).convert("RGBA").resize((resize_dim, resize_dim))
    except Exception as e:
        logger.warning(f"⚠️ Не удалось открыть PNG для сэмплинга цвета ({png_path}): {e}")
        return []

    arr = np.array(img).reshape(-1, 4).astype(np.float64)
    rgb = arr[:, :3]
    alpha = arr[:, 3] / 255.0

    reliable_mask = alpha >= min_alpha
    if not np.any(reliable_mask):
        logger.warning(
            f"⚠️ Все пиксели {png_path.name} имеют alpha < {min_alpha} — "
            "надёжный сэмплинг доминирующего цвета невозможен, возвращаю пустой результат."
        )
        return []

    reliable_rgb = rgb[reliable_mask]
    reliable_alpha = alpha[reliable_mask]

    if composite_bg is not None:
        bg = np.array(composite_bg, dtype=np.float64).reshape(1, 3)
        a_col = reliable_alpha.reshape(-1, 1)
        reliable_rgb = reliable_rgb * a_col + bg * (1.0 - a_col)

    reliable_rgb = np.clip(np.round(reliable_rgb), 0, 255).astype(np.uint8)
    unique_colors, counts = np.unique(reliable_rgb, axis=0, return_counts=True)
    top_indices = np.argsort(-counts)[:k]
    return [
        f"rgba({int(unique_colors[i][0])}, {int(unique_colors[i][1])}, {int(unique_colors[i][2])}, 1.0)"
        for i in top_indices
    ]


def sample_accent_color_from_asset(
        asset_path: Path,
        k: int = 8,
        resize_dim: int = 48,
        min_alpha: float = 0.5,
        min_saturation: float = 0.15,
) -> str | None:
    """Определяет доминирующий насыщенный цвет по файлу экспортированного ассета.

    Универсальный fallback для случая, когда акцентный цвет не представлен
    отдельным RGB-токеном в IR: он может быть «запечён» в `image_fill`
    (текстура или фото с фильтрами), в градиент или в обводку. Тогда `bg_color`
    узла равен `None`, и структурный поиск цвета
    (`StyleTransferEngine._find_accent_source_color`) ничего не находит.

    Использует `sample_dominant_colors_from_png`. Берётся не самый частый цвет, а
    первый по частоте с насыщенностью (HLS) не меньше `min_saturation`: в
    декоративном ассете самый частый цвет обычно фон или тень (серый, почти
    чёрный), а визуальный акцент — 2-й или 3-й по частоте.

    Args:
        asset_path: Путь к файлу ассета.
        k: Сколько самых частых цветов рассматривать.
        resize_dim: Сторона квадрата, к которому приводится изображение, px.
        min_alpha: Минимальная альфа (0..1), с которой пиксель учитывается.
        min_saturation: Минимальная насыщенность (HLS) цвета-акцента.

    Returns:
        str | None: Цвет `rgba(r, g, b, 1.0)`. `None`, если файл не PNG (для SVG
        растровый анализ неприменим, цвет читается из структурных полей Figma на
        более точных шагах) либо среди `k` цветов нет достаточно насыщенного
        (ассет ахроматичный, перекраска ему не нужна).
    """
    if asset_path.suffix.lower() != ".png":
        return None

    dominant = sample_dominant_colors_from_png(
        asset_path, k=k, resize_dim=resize_dim, min_alpha=min_alpha,
    )

    for rgba_str in dominant:
        channels = parse_rgba_channels(rgba_str)
        if channels is None:
            continue
        r, g, b, _a = channels
        _h, _l, s = colorsys.rgb_to_hls(r, g, b)
        if s >= min_saturation:
            return rgba_str

    return None


@dataclass(frozen=True)
class OpaqueRegionStats:
    """Статистика непрозрачной области PNG (результат `analyze_opaque_region`).

    Attributes:
        coverage_ratio: Доля пикселей с альфой не меньше `min_alpha`, 0..1.
        color_std: Евклидова норма стандартных отклонений каналов RGB
            непрозрачных пикселей (шкала 0..255); мала для плоской заливки.
        dominant_color_coverage: Доля непрозрачных пикселей, лежащих в пределах
            `dominant_color_tolerance` (расстояние в RGB) от самого частого цвета.
        sample_count: Число непрозрачных пикселей.
    """

    coverage_ratio: float
    color_std: float
    dominant_color_coverage: float
    sample_count: int


def analyze_opaque_region(
        png_path: Path,
        min_alpha: float = 0.6,
        resize_dim: int = 64,
        dominant_color_tolerance: float = 20.0,
) -> OpaqueRegionStats:
    """Считает статистику непрозрачной области PNG-референса.

    Изображение читается как RGBA и приводится к `resize_dim` x `resize_dim`.
    «Непрозрачными» считаются пиксели с альфой не меньше `min_alpha`. По ним
    оценивается, плоская ли это заливка (малый разброс цвета, явный доминирующий
    цвет) или текстурная сцена. Доминирующий цвет ищется среди цветов,
    округлённых до кратных 8.

    Args:
        png_path: Путь к PNG.
        min_alpha: Минимальная альфа (0..1), с которой пиксель считается
            непрозрачным.
        resize_dim: Сторона квадрата, к которому приводится изображение, px.
        dominant_color_tolerance: Максимальное евклидово расстояние в RGB от
            доминирующего цвета, при котором пиксель относится к нему.

    Returns:
        OpaqueRegionStats: Статистика. Если файл не удалось открыть (в лог пишется
        предупреждение), все поля нулевые. Если непрозрачных пикселей нет,
        `coverage_ratio` равен 0.0, остальные поля нулевые.
    """
    import numpy as np
    from PIL import Image
    try:
        img = Image.open(png_path).convert("RGBA").resize((resize_dim, resize_dim))
    except Exception as e:
        logger.warning(f"⚠️ Не удалось открыть PNG для анализа opaque-региона ({png_path}): {e}")
        return OpaqueRegionStats(coverage_ratio=0.0, color_std=0.0, dominant_color_coverage=0.0, sample_count=0)

    arr = np.array(img).reshape(-1, 4).astype(np.float64)
    rgb = arr[:, :3]
    alpha = arr[:, 3] / 255.0

    mask = alpha >= min_alpha
    coverage_ratio = float(np.mean(mask))

    if not np.any(mask):
        return OpaqueRegionStats(coverage_ratio=coverage_ratio, color_std=0.0, dominant_color_coverage=0.0,
                                 sample_count=0)

    opaque_rgb = rgb[mask]
    color_std = float(np.linalg.norm(opaque_rgb.std(axis=0)))

    quantized = np.round(opaque_rgb / 8.0) * 8.0
    unique_colors, counts = np.unique(quantized, axis=0, return_counts=True)
    dominant_bucket = unique_colors[np.argmax(counts)]
    distances = np.linalg.norm(opaque_rgb - dominant_bucket, axis=1)
    dominant_color_coverage = float(np.mean(distances <= dominant_color_tolerance))

    return OpaqueRegionStats(
        coverage_ratio=coverage_ratio,
        color_std=color_std,
        dominant_color_coverage=dominant_color_coverage,
        sample_count=int(opaque_rgb.shape[0]),
    )


def sample_transparent_backing_rgb(
        png_path: Path,
        max_alpha: float = 0.05,
        resize_dim: int = 64,
) -> str | None:
    """Определяет RGB, записанный в PNG под прозрачными пикселями.

    Зачем это нужно: `compute_ssim` и `compute_masked_metrics`
    (`generate_landing.py`, `QAEngine`) открывают референс через
    `Image.open(...).convert("RGB")` и `.convert("L")`. Pillow при этом просто
    отбрасывает альфа-канал, не компонуя его с фоном, и в сравнении участвует тот
    RGB, который физически записан под прозрачностью. Если рендерер подставит
    другой, пусть и визуально близкий цвет, в областях с яркостью около нуля SSIM
    даст непропорционально большое расхождение (см. `LOW_LUMINANCE_THRESHOLD` в
    `generate_landing.py`).

    Значение не задано константой, а читается из каждого файла отдельно: среди
    пикселей с альфой не больше `max_alpha` берётся самый частый цвет (каналы
    округляются до кратных 4).

    Ограничение: перед анализом изображение приводится к `resize_dim` x
    `resize_dim` методом `Image.resize` с фильтром по умолчанию. Для RGBA Pillow
    выполняет такое сжатие в предумноженном формате, поэтому RGB под полностью
    прозрачными пикселями при этом обнуляется, и функция возвращает
    `rgba(0, 0, 0, 1.0)`, а не цвет, записанный в файле. Проверено на Pillow
    12.1.1: пиксель `(10, 20, 30, 0)` после `resize((64, 64))` становится
    `(0, 0, 0, 0)`.

    Args:
        png_path: Путь к PNG.
        max_alpha: Максимальная альфа (0..1), при которой пиксель считается
            прозрачным.
        resize_dim: Сторона квадрата, к которому приводится изображение, px.

    Returns:
        str | None: Цвет `rgba(r, g, b, 1.0)`. `None`, если файл не удалось
        открыть (в лог пишется предупреждение) или прозрачных пикселей нет:
        вызывающий код остаётся на прежних fallback (`design_system.canvas` или
        значение по умолчанию).
    """
    import numpy as np
    from PIL import Image
    try:
        img = Image.open(png_path).convert("RGBA").resize((resize_dim, resize_dim))
    except Exception as e:
        logger.warning(f"⚠️ Не удалось открыть PNG для сэмплинга backing-цвета ({png_path}): {e}")
        return None

    arr = np.array(img).reshape(-1, 4).astype(np.float64)
    rgb = arr[:, :3]
    alpha = arr[:, 3] / 255.0

    transparent_mask = alpha <= max_alpha
    if not np.any(transparent_mask):
        return None

    transparent_rgb = rgb[transparent_mask]
    quantized = np.round(transparent_rgb / 4.0) * 4.0
    unique_colors, counts = np.unique(quantized, axis=0, return_counts=True)
    dominant = unique_colors[np.argmax(counts)]
    r, g, b = (int(v) for v in dominant)
    return f"rgba({r}, {g}, {b}, 1.0)"


def collect_background_candidates(
        root: IRNode, root_w: float, root_h: float, cfg: dict,
) -> list[tuple[float, str, str]]:
    """Собирает кандидатов на роль фона секции, отсортированных по убыванию score.

    Узел становится кандидатом, если у него есть заливка (`bg_color` не пуст и не
    `transparent`), он проходит `passes_bg_reliability_filter` и покрывает не
    меньше `min_coverage_ratio` площади секции. Оценка:
    `coverage * bg_reliability_weight * depth_bonus`, где
    `depth_bonus = 1 / (1 + depth * depth_priority_falloff)`: чем глубже узел,
    тем ниже его приоритет.

    Координаты считаются от левого верхнего угла секции. Функция рассчитывает на
    то, что у корня `rel_geometry.x/y` равны 0 (так задаёт
    `FigmaNormalizer.normalize_node` для корневого узла): обход стартует с
    `(0, 0)` и суммирует локальные смещения потомков относительно родителей.
    Если у корня смещение ненулевое, все координаты, а значит и покрытие,
    сместятся.

    Args:
        root: Корневой узел секции.
        root_w: Ширина секции, px.
        root_h: Высота секции, px.
        cfg: Раздел `reference_bg_detection` из `analysis_rules.json`
            (`min_coverage_ratio`, `depth_priority_falloff`, `gradient_penalty` и
            ключи `passes_bg_reliability_filter`).

    Returns:
        list[tuple[float, str, str]]: Кортежи `(score, цвет, описание узла)`;
        описание имеет вид `<имя>(depth=N,cov=0.xx)`. Пустой список, если
        подходящих узлов нет.
    """
    min_coverage = float(cfg.get("min_coverage_ratio", 0.35))
    depth_falloff = float(cfg.get("depth_priority_falloff", 0.02))

    candidates: list[tuple[float, str, str]] = []

    def walk(node: IRNode, abs_x: float, abs_y: float, depth: int) -> None:
        """Добавляет узел в `candidates`, если он подходит, и обходит его детей.

        Args:
            node: Текущий узел.
            abs_x: X начала родителя относительно секции, px.
            abs_y: Y начала родителя относительно секции, px.
            depth: Глубина узла (у корня 0).
        """
        if (node.style.bg_color and node.style.bg_color != "transparent"
                and passes_bg_reliability_filter(node, cfg)):
            nx = abs_x + node.rel_geometry.x
            ny = abs_y + node.rel_geometry.y
            w = node.rel_geometry.width
            h = node.rel_geometry.height
            if root_w > 0 and root_h > 0 and w > 0 and h > 0:
                overlap_w = max(0.0, min(nx + w, root_w) - max(nx, 0.0))
                overlap_h = max(0.0, min(ny + h, root_h) - max(ny, 0.0))
                coverage = (overlap_w * overlap_h) / (root_w * root_h)
                if coverage >= min_coverage:
                    depth_bonus = 1.0 / (1.0 + depth * depth_falloff)
                    score = coverage * bg_reliability_weight(node, cfg) * depth_bonus
                    candidates.append((
                        score, node.style.bg_color,
                        f"{node.name}(depth={depth},cov={coverage:.2f})"
                    ))
        for ch in node.children:
            walk(ch, abs_x + node.rel_geometry.x, abs_y + node.rel_geometry.y, depth + 1)

    walk(root, 0.0, 0.0, 0)
    candidates.sort(key=lambda c: c[0], reverse=True)
    return candidates
