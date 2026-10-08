"""Модели данных pipeline «Figma → Landing» (pydantic).

Модуль описывает:
    * токены дизайн-системы и происхождение их значений;
    * IR (промежуточное представление) дерева Figma: геометрию, раскладку, стиль;
    * политики переноса стиля, спецификации секций и лендинга;
    * результаты семантической классификации текстовых узлов.

Модели общие для `figma_extractor.py` (записывает spec) и `generate_landing.py`
(читает spec и строит лендинг).
"""

import re
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

# Семантическая роль текстового узла.
SemanticRole = Literal[
    "heading_h1", "heading_h2", "heading_h3",
    "body", "button_label", "stat", "caption", "nav_link"
]
# Роль компонента, определённая структурными эвристиками.
ComponentRole = Literal[
    "button", "card", "badge", "nav", "hero", "section", "container"
]
# Допустимые HTML-теги узла.
HTMLTag = Literal[
    "h1", "h2", "h3", "h4", "p", "button", "a", "span", "div", "section", "img"
]

# Hex-цвет: `#rgb` или `#rrggbb`.
_HEX_COLOR_RE = re.compile(r"^#([0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")


# --- 1. PROVENANCE & TOKENS ---
class SourceRef(BaseModel):
    """Ссылка на источник данных (файл и узел в Figma).

    Attributes:
        provider: Провайдер данных.
        resource_id: Идентификатор ресурса; для Figma это ключ файла.
        node_id: Идентификатор узла внутри файла.
        version: Версия ресурса, если известна.
        metadata: Дополнительные данные источника.
    """

    provider: str = "figma"
    resource_id: str
    node_id: str | None = None
    version: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


def normalize_color_to_rgba(value: str) -> str:
    """Приводит hex-цвет к каноническому формату `rgba(r, g, b, 1.0)`.

    Токены дизайн-системы приходят из разных источников: Figma API отдаёт
    `rgba(...)`, а `configs/defaults.json` записан в hex (`#rrggbb`). Цветовая
    математика (`parse_rgba_channels` и всё, что на ней построено:
    `rgba_lightness`, `pick_lightest_token`, `pick_contrasting_text_color`,
    `compute_retint_filter`, `color_distance_hls`) работает с форматом
    `rgba(...)`. Поэтому нормализация выполняется в одной точке, на границе
    модели `ResolvedToken`, и действует сразу на все токены, в том числе
    подставленные из конфига. Новые форматы (`rgb()`, именованные CSS-цвета)
    добавляются здесь же.

    Args:
        value: Цветовая строка. Поддерживаются `#rgb` и `#rrggbb` (регистр не
            важен, пробелы по краям допускаются).

    Returns:
        str: `rgba(r, g, b, 1.0)` для hex-цвета. Любая другая строка, а также
        нестроковое значение возвращаются без изменений.
    """
    if not isinstance(value, str):
        return value
    stripped = value.strip()
    match = _HEX_COLOR_RE.match(stripped)
    if not match:
        return value
    hex_part = match.group(1)
    if len(hex_part) == 3:
        hex_part = "".join(ch * 2 for ch in hex_part)
    r = int(hex_part[0:2], 16)
    g = int(hex_part[2:4], 16)
    b = int(hex_part[4:6], 16)
    return f"rgba({r}, {g}, {b}, 1.0)"


class ResolvedToken(BaseModel):
    """Значение токена дизайн-системы с указанием происхождения и уверенности.

    Строковое значение при создании нормализуется (`normalize_color_to_rgba`):
    hex-цвет превращается в `rgba(...)`.

    Attributes:
        value: Значение токена: цвет, число (радиус, отступ) или строка.
        source: Происхождение: `figma` — извлечено из макета, `config` —
            значение по умолчанию из конфига, `override` — переопределено в
            манифесте секции.
        confidence: Уверенность в том, что значение извлечено корректно, 0..1.
    """

    value: str | float | int
    source: Literal["figma", "config", "override"] = "figma"
    confidence: float = 1.0

    @field_validator("value", mode="before")
    @classmethod
    def _normalize_color_value(cls, v: Any) -> Any:
        """Нормализует строковое значение токена (hex -> rgba).

        Числовые токены (радиусы, отступы) не затрагиваются.

        Args:
            v: Сырое значение поля `value`.

        Returns:
            Any: Нормализованная строка либо исходное нестроковое значение.
        """
        return normalize_color_to_rgba(v) if isinstance(v, str) else v


class TypographyToken(BaseModel):
    """Токен типографики одной роли (h1, body, caption и т.д.).

    Attributes:
        family: Семейство шрифта.
        size: Размер шрифта, px.
        weight: Вес шрифта (100..900).
        line_height_percent: Высота строки, %; рендерер выводит `line-height: N/100`.
        letter_spacing: Межбуквенный интервал, px.
    """

    family: str
    size: float
    weight: int
    line_height_percent: float = 120.0
    letter_spacing: float = 0.0


class DesignTokens(BaseModel):
    """Набор токенов дизайн-системы.

    Attributes:
        colors: Цветовые токены по именам (`canvas`, `surface`, `primary` и т.д.).
        typography: Токены типографики по ролям.
        radii: Радиусы скругления по ролям компонентов, px.
        spacing: Отступы по именам (`md` и т.д.), px.
        extras: Дополнительные данные.
    """

    colors: dict[str, ResolvedToken] = Field(default_factory=dict)
    typography: dict[str, TypographyToken] = Field(default_factory=dict)
    radii: dict[str, float] = Field(default_factory=dict)
    spacing: dict[str, float] = Field(default_factory=dict)
    extras: dict[str, Any] = Field(default_factory=dict)


class ComponentVariant(BaseModel):
    """Вариант компонента, извлечённый из дизайн-системы (например, кнопка).

    Attributes:
        name: Имя узла-компонента.
        role: Роль компонента.
        padding_top: Верхний внутренний отступ, px.
        padding_right: Правый внутренний отступ, px.
        padding_bottom: Нижний внутренний отступ, px.
        padding_left: Левый внутренний отступ, px.
        radius: Радиус скругления, px.
        bg_color: Цвет заливки.
        text_color: Цвет текста.
        border_color: Цвет рамки.
    """

    name: str
    role: ComponentRole = "button"
    padding_top: float = 0.0
    padding_right: float = 0.0
    padding_bottom: float = 0.0
    padding_left: float = 0.0
    radius: float = 0.0
    bg_color: str | None = None
    text_color: str | None = None
    border_color: str | None = None


class DesignSystemSpec(BaseModel):
    """Дизайн-система: источник, токены и варианты компонентов.

    Attributes:
        source: Источник, из которого извлечена дизайн-система.
        tokens: Токены цвета, типографики, радиусов и отступов.
        components: Варианты компонентов по ролям (например, `{"button": [...]}`).
    """

    source: SourceRef
    tokens: DesignTokens
    components: dict[str, list[ComponentVariant]] = Field(default_factory=dict)


# --- 2. FIGMA IR (Intermediate Representation) ---
class Geometry(BaseModel):
    """Положение и размеры узла.

    Attributes:
        x: Координата X относительно родителя, px.
        y: Координата Y относительно родителя, px.
        width: Ширина, px.
        height: Высота, px.
        parent_width: Ширина родителя, px.
        parent_height: Высота родителя, px.
        absolute_x: Координата X в документе Figma, px.
        absolute_y: Координата Y в документе Figma, px.
    """

    # Координаты относительно родителя.
    x: float = 0.0
    y: float = 0.0

    width: float = 0.0
    height: float = 0.0

    parent_width: float = 0.0
    parent_height: float = 0.0

    # Абсолютные координаты в документе Figma.
    absolute_x: float = 0.0
    absolute_y: float = 0.0


class GradientStop(BaseModel):
    """Опорная точка градиента.

    Attributes:
        color: Цвет точки.
        position: Положение на линии градиента, 0..1.
    """

    color: str
    position: float = 0.0


class GradientData(BaseModel):
    """Градиентная заливка, приведённая к виду, пригодному для CSS.

    Attributes:
        figma_type: Исходный тип градиента в Figma (`GRADIENT_LINEAR` и др.).
        css_type: Тип CSS-градиента.
        angle_deg: Угол, градусы CSS; для `radial` не используется.
        stops: Опорные точки градиента.
        handle_positions: Ручки градиента в нормализованных координатах Figma.
        center_x_pct: Центр по X, % (для radial и conic).
        center_y_pct: Центр по Y, % (для radial и conic).
    """

    figma_type: str | None = None
    css_type: Literal["linear", "radial", "conic"] = "linear"
    angle_deg: float = 180.0
    stops: list[GradientStop] = Field(default_factory=list)
    handle_positions: list[dict[str, float]] = Field(default_factory=list)
    # Значения по умолчанию соответствуют центру объекта.
    center_x_pct: float = 50.0
    center_y_pct: float = 50.0


class IRLayout(BaseModel):
    """Раскладка узла: Auto Layout, отступы, режимы размера, констрейнты.

    Attributes:
        is_flex: Узел использует Auto Layout (рендерится как flex-контейнер).
        direction: Направление flex-контейнера.
        gap: Промежуток между детьми, px.
        padding_top: Верхний внутренний отступ, px.
        padding_right: Правый внутренний отступ, px.
        padding_bottom: Нижний внутренний отступ, px.
        padding_left: Левый внутренний отступ, px.
        align_items: Значение CSS `align-items`.
        justify_content: Значение CSS `justify-content`.
        sizing_horizontal: Режим размера по горизонтали (`FIXED`, `HUG`, `FILL`).
        sizing_vertical: Режим размера по вертикали (`FIXED`, `HUG`, `FILL`).
        intended_single_line: Текст задуман однострочным (эвристика по высоте).
        wrap: Дети переносятся на новую строку.
        layout_strategy: Стратегия раскладки: `flex` — Auto Layout, `relative` —
            есть дети без Auto Layout, `flow` — узел без детей.
        h_constraint: Горизонтальный констрейнт Figma.
        v_constraint: Вертикальный констрейнт Figma.
        layout_positioning: Позиционирование внутри Auto Layout родителя
            (`ABSOLUTE` — узел вынесен из потока).
        primary_axis_sizing_mode: Режим размера по основной оси Auto Layout.
        counter_axis_sizing_mode: Режим размера по поперечной оси Auto Layout.
        layout_align: Выравнивание узла внутри родителя (`layoutAlign` Figma).
        layout_grow: Коэффициент растяжения узла (`layoutGrow` Figma).
        width: Ширина, px.
        height: Высота, px.
        clips_content: Содержимое обрезается по границам узла.
        extras: Дополнительные данные Figma.
    """

    is_flex: bool = False
    direction: Literal["row", "column"] = "column"
    gap: float = 0.0

    padding_top: float = 0.0
    padding_right: float = 0.0
    padding_bottom: float = 0.0
    padding_left: float = 0.0

    align_items: str = "flex-start"
    justify_content: str = "flex-start"

    sizing_horizontal: str = "FIXED"
    sizing_vertical: str = "FIXED"

    intended_single_line: bool = False
    wrap: bool = False

    layout_strategy: Literal["flex", "relative", "flow"] = "flow"
    h_constraint: Literal["left", "right", "center", "scale", "stretch"] = "left"
    v_constraint: Literal["top", "bottom", "center", "scale", "stretch"] = "top"
    layout_positioning: Literal["AUTO", "ABSOLUTE"] = "AUTO"

    primary_axis_sizing_mode: str = "AUTO"
    counter_axis_sizing_mode: str = "AUTO"

    layout_align: str = "INHERIT"
    layout_grow: float = 0.0

    width: float = 0.0
    height: float = 0.0

    clips_content: bool = False

    extras: dict[str, Any] = Field(default_factory=dict)


class ImageFillSpec(BaseModel):
    """Полное описание заливки изображением (Figma IMAGE paint).

    Сохраняется не только факт «здесь картинка», но и параметры, без которых её
    нельзя корректно рендерить и классифицировать: режим масштабирования,
    трансформация и фильтры.

    Attributes:
        image_ref: Ссылка на изображение в Figma.
        scale_mode: Режим масштабирования (`FILL`, `FIT`, `TILE` и т.д.).
        image_transform: Матрица трансформации изображения.
        filters: Фильтры изображения (exposure, contrast и т.п.) по именам.
        opacity: Непрозрачность заливки, 0..1.
    """

    image_ref: str | None = None
    scale_mode: str | None = None
    image_transform: list[list[float]] | None = None
    filters: dict[str, float] = Field(default_factory=dict)
    opacity: float = 1.0


class IRStyle(BaseModel):
    """Визуальный стиль узла.

    Attributes:
        bg_color: Цвет заливки.
        text_color: Цвет текста.
        text_gradient: Градиентная заливка текста.
        font_family: Семейство шрифта.
        font_size: Размер шрифта, px.
        font_weight: Вес шрифта.
        line_height_percent: Высота строки, %.
        letter_spacing: Межбуквенный интервал, px.
        text_align: Выравнивание текста (значение CSS `text-align`).
        border_radius: Радиус скругления, px.
        border_color: Цвет рамки.
        border_width: Толщина рамки, px.
        opacity: Непрозрачность узла, 0..1.
        blend_mode: Режим наложения Figma.
        bg_is_gradient_approx: `bg_color` — лишь средняя точка градиента.
        bg_gradient: Градиентная заливка.
        bg_is_image: Заливка — изображение.
        image_fill: Параметры заливки изображением.
        retint_hue_deg: Угол `hue-rotate` перекраски, градусы.
        retint_saturate: Коэффициент `saturate()` перекраски.
        retint_brightness: Коэффициент `brightness()` перекраски.
        retinted: Перекраска применена.
        custom_css: Дополнительные CSS-декларации узла.
        extras: Дополнительные данные Figma (эффекты и т.п.).
    """

    bg_color: str | None = None
    text_color: str | None = None
    text_gradient: GradientData | None = None
    font_family: str | None = None
    font_size: float | None = None
    font_weight: int | None = None
    line_height_percent: float | None = None
    letter_spacing: float | None = None
    text_align: str | None = None
    border_radius: float = 0.0
    border_color: str | None = None
    border_width: float = 0.0
    opacity: float = 1.0
    blend_mode: str = "PASS_THROUGH"
    bg_is_gradient_approx: bool = False
    bg_gradient: GradientData | None = None
    bg_is_image: bool = False
    image_fill: ImageFillSpec | None = None
    retint_hue_deg: float | None = None
    retint_saturate: float | None = None
    retint_brightness: float | None = None
    # Единый признак «перекраска применена» для обоих механизмов: CSS-фильтр для
    # assets и raster_composite (`retint_hue_deg` и др.) и прямая перезапись
    # `bg_color` для native-декораций (`apply_retint_to_rgba`). У native-декораций
    # hue-rotate не участвует, `retint_hue_deg` остаётся None, и о перекраске
    # говорит только этот флаг (его читает `audit_retint_coverage`).
    retinted: bool = False
    custom_css: dict[str, str] = Field(default_factory=dict)
    extras: dict[str, Any] = Field(default_factory=dict)


class IRNode(BaseModel):
    """Узел IR-дерева: нормализованное представление узла Figma.

    Attributes:
        id: Идентификатор узла Figma.
        name: Имя узла.
        type: Тип узла Figma (`FRAME`, `TEXT`, `GROUP` и т.д.). Не меняется после
            извлечения: способ вывода определяет `render_strategy`.
        rel_geometry: Положение и размеры.
        layout: Раскладка.
        style: Стиль.
        characters: Текст узла (для `TEXT`).
        semantic_role: Семантическая роль текста.
        component_role: Роль компонента.
        html_tag: HTML-тег, в который рендерится узел.
        asset_id: Идентификатор экспортируемого ассета.
        asset_path: Путь к файлу ассета относительно каталога запуска.
        asset_format: Формат ассета.
        render_strategy: Способ вывода: `native` — HTML и CSS, `asset` — готовый
            файл (SVG/PNG), `raster_composite` — растеризованная композиция.
        component_name: Имя компонента для `COMPONENT` и `INSTANCE`.
        extras: Дополнительные данные.
        children: Дочерние узлы.
    """

    id: str
    name: str
    type: str
    rel_geometry: Geometry
    layout: IRLayout
    style: IRStyle
    characters: str | None = None
    semantic_role: SemanticRole | None = None
    component_role: ComponentRole | None = None
    html_tag: HTMLTag = "div"
    asset_id: str | None = None
    asset_path: str | None = None
    asset_format: Literal["png", "svg"] | None = None
    render_strategy: Literal["native", "asset", "raster_composite"] = "native"
    component_name: str | None = None
    extras: dict[str, Any] = Field(default_factory=dict)
    children: list['IRNode'] = Field(default_factory=list)


class Asset(BaseModel):
    """Экспортированный ассет (файл PNG или SVG).

    Attributes:
        id: Идентификатор ассета (совпадает с id узла).
        name: Имя узла-источника.
        asset_type: Формат файла.
        path: Путь к файлу относительно корня проекта.
        geometry: Положение и размеры узла-источника.
        source_node_id: Идентификатор узла-источника.
    """

    id: str
    name: str
    asset_type: Literal["png", "svg"] = "png"
    path: str
    geometry: Geometry
    source_node_id: str


# --- 3. POLICIES & SECTIONS ---
class PreservationPolicy(BaseModel):
    """Политика переноса стиля: что сохранить, а что заменить.

    `preserve_*` — что берётся из исходной композиции как есть.
    `transfer_*` — что заменяется визуальным языком источника стиля.
    Сохранение структуры компонента (`preserve_component_structure`) и перенос
    его стиля (`transfer_component_style`) включаются независимо друг от друга.

    Attributes:
        preserve_composition: Сохранять композицию исходной секции.
        preserve_content: Сохранять содержимое; при `False` текст обрезается по
            краям (`strip`).
        preserve_assets: Сохранять ассеты.
        preserve_geometry: Сохранять геометрию; при `False` отступы кнопок
            увеличиваются до минимальных значений из `render_rules.json`.
        preserve_component_structure: Сохранять структуру компонентов.
        transfer_typography: Переносить типографику.
        transfer_component_style: Переносить стиль компонентов (button, card, badge).
        transfer_colors: Переносить цвета текста.
        transfer_effects: Переносить эффекты.
        retint_assets: Перекрашивать акцентный цвет внутри ассетов и
            composite-PNG (иконки, свечение) через CSS `hue-rotate` под целевой
            primary-токен.
    """

    preserve_composition: bool = True
    preserve_content: bool = True
    preserve_assets: bool = True
    preserve_geometry: bool = True
    preserve_component_structure: bool = True

    transfer_typography: bool = True
    transfer_component_style: bool = True
    transfer_colors: bool = True
    transfer_effects: bool = True
    # По умолчанию True: без перекраски секция после style_transfer остаётся
    # «двухцветной» (акцент исходной композиции плюс палитра дизайн-системы на
    # остальном контенте).
    retint_assets: bool = True


class TransformSpec(BaseModel):
    """Настройки переноса стиля для секции.

    Attributes:
        mode: Режим: `native` — без переноса стиля, `style_transfer` — перенос
            стиля дизайн-системы на секцию.
        preservation: Политика сохранения и переноса.
        token_overrides: Переопределения токенов по dot-пути
            (например, `colors.primary`, `typography.button.size`).
    """

    mode: Literal["native", "style_transfer"] = "style_transfer"
    preservation: PreservationPolicy = Field(default_factory=PreservationPolicy)
    token_overrides: dict[str, Any] = Field(default_factory=dict)


class SectionSpec(BaseModel):
    """Секция композиции: IR-дерево и сопутствующие данные.

    Attributes:
        id: Идентификатор секции.
        name: Название секции.
        source: Источник секции (файл и узел Figma).
        geometry: Размеры секции.
        root_node: Корень IR-дерева.
        texts: Метаданные текстовых узлов по их id (`text`, `font_size`,
            `font_weight`, `name`).
        assets: Экспортированные ассеты секции.
        reference_image: Путь к эталонному PNG секции относительно корня проекта.
        transform: Настройки переноса стиля.
        responsive_capable: Секция пригодна для адаптивной раскладки.
    """

    id: str
    name: str
    source: SourceRef
    geometry: Geometry
    root_node: IRNode
    texts: dict[str, dict[str, Any]] = Field(default_factory=dict)
    assets: list[Asset] = Field(default_factory=list)
    reference_image: str | None = None
    transform: TransformSpec = Field(default_factory=TransformSpec)
    responsive_capable: bool = False


class ResolvedSectionSpec(BaseModel):
    """Секция после переноса стиля.

    Attributes:
        id: Идентификатор секции.
        name: Название секции.
        source: Источник секции.
        geometry: Размеры секции.
        resolved_root: Дерево с перенесённым стилем.
        original_root: Исходное дерево без изменений (эталон для Extraction QA).
        assets: Ассеты секции.
        reference_image: Путь к эталонному PNG секции.
        responsive_capable: Секция пригодна для адаптивной раскладки.
    """

    id: str
    name: str
    source: SourceRef
    geometry: Geometry
    resolved_root: IRNode
    original_root: IRNode
    assets: list[Asset]
    reference_image: str | None = None
    responsive_capable: bool = False


class ViewportSpec(BaseModel):
    """Вьюпорт для проверки адаптивности.

    Attributes:
        name: Имя вьюпорта (`desktop`, `tablet`, `mobile`).
        width: Ширина, px.
        height: Высота, px.
    """

    name: str
    width: int
    height: int


class QAConfig(BaseModel):
    """Настройки QA.

    Attributes:
        diff_threshold: Порог MAE при сравнении с эталоном.
        ssim_threshold: Порог структурного расхождения (`1 - SSIM`).
        fail_on_console_error: Считать вьюпорт непройденным при ошибках консоли
            или неудачных запросах.
        fail_on_overflow: Считать вьюпорт непройденным при горизонтальном
            переполнении.
        fail_on_empty_space: Считать вьюпорт непройденным при пустом
            пространстве внутри секции.
        viewports: Вьюпорты для проверки.
    """

    diff_threshold: float = 0.18
    ssim_threshold: float = 0.18
    fail_on_console_error: bool = True
    fail_on_overflow: bool = True
    # По умолчанию False: пустое пространство только логируется в qa_report.json
    # (`has_empty_space`, `empty_space_offenders`). Строгий гейт включается в
    # `configs/landing_manifest.json` (раздел `qa`), когда проверка должна стать
    # обязательной для новых секций.
    fail_on_empty_space: bool = False
    viewports: list[ViewportSpec] = Field(default_factory=list)


class RenderConfig(BaseModel):
    """Настройки рендера (раздел `render` манифеста).

    Attributes:
        renderer: Имя рендерера.
        responsive: Признак адаптивной вёрстки.
        language: Код языка для атрибута `lang` у `<html>`.
    """

    renderer: str = "web"
    responsive: bool = True
    language: str = "en"


class LandingSpec(BaseModel):
    """Итоговая спецификация лендинга: единый объект для рендера и QA.

    Attributes:
        project_name: Название проекта.
        design_system: Дизайн-система.
        sections: Секции после переноса стиля.
        render: Настройки рендера.
        qa: Настройки QA.
    """

    project_name: str
    design_system: DesignSystemSpec
    sections: list[ResolvedSectionSpec]
    render: RenderConfig = Field(default_factory=RenderConfig)
    qa: QAConfig


# --- 4. SEMANTIC RESOLUTION ---
class ResolutionResult(BaseModel):
    """Результат классификации текстового узла.

    Attributes:
        node_id: Идентификатор узла.
        semantic_role: Семантическая роль.
        html_tag: HTML-тег.
        source: Откуда решение: `rules` — детерминированные правила, `llm` —
            языковая модель, `fallback` — нейтральное решение по умолчанию.
        confidence: Уверенность в решении, 0..1.
    """

    node_id: str
    semantic_role: SemanticRole
    html_tag: HTMLTag
    source: Literal["rules", "llm", "fallback"]
    confidence: float = 1.0


class ElementDecision(BaseModel):
    """Решение LLM по одному узлу.

    Attributes:
        id: Идентификатор узла.
        semantic_role: Семантическая роль.
        html_tag: HTML-тег.
        confidence: Уверенность модели, 0..1.
    """

    id: str
    semantic_role: SemanticRole
    html_tag: HTMLTag
    confidence: float = 1.0


class SemanticMap(BaseModel):
    """Ответ LLM: решения по узлам.

    Attributes:
        decisions: Список решений.
    """

    decisions: list[ElementDecision]


IRNode.model_rebuild()