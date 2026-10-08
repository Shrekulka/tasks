# python_style_extractor/scripts/component_heuristics.py

"""Эвристики распознавания компонентов (button, badge, card) по структуре IR.

Модуль вынесен отдельно, чтобы `figma_extractor.py` и `generate_landing.py`
использовали одни и те же правила без лишних зависимостей: он зависит только от
конфигов и IR-моделей и не тянет ни LLM-клиенты, ни Playwright.

Правила читаются из `configs/analysis_rules.json` и `configs/semantic_rules.json`
один раз при импорте модуля.
"""

from scripts.models import IRNode
from scripts.paths import CONFIGS_DIR
from scripts.utils import ConfigLoader, keyword_match

ANALYSIS_RULES = ConfigLoader.load(CONFIGS_DIR / "analysis_rules.json")
SEMANTIC_RULES = ConfigLoader.load(CONFIGS_DIR / "semantic_rules.json")


class ComponentHeuristics:
    """Структурные признаки компонентов Figma/IR без семантического анализа.

    Все пороги берутся из `ANALYSIS_RULES` и `SEMANTIC_RULES`; значения в вызовах
    `.get(ключ, значение)` — запасные на случай отсутствия ключа в конфиге.
    """

    @staticmethod
    def looks_like_badge(node: IRNode, text_children: list[IRNode]) -> bool:
        """Проверяет, похож ли узел на бейдж (короткая «таблетка» с текстом).

        Пороги берутся из `analysis_rules.json -> badge_detection`. Узел считается
        бейджем, если выполнены все условия:
          * радиус скругления не меньше `min_corner_radius` (по умолчанию 40.0);
          * высота (`layout.height`) не больше `max_height` (по умолчанию 40.0);
          * объединённый текст `text_children` не пуст и содержит не больше
            `max_words_in_text` слов (по умолчанию 3).

        Args:
            node: Проверяемый узел.
            text_children: Прямые TEXT-потомки узла.

        Returns:
            bool: `True`, если узел похож на бейдж.
        """
        badge_cfg = ANALYSIS_RULES.get("badge_detection", {})
        min_radius = float(badge_cfg.get("min_corner_radius", 40.0))
        max_height = float(badge_cfg.get("max_height", 40.0))
        max_words = int(badge_cfg.get("max_words_in_text", 3))

        if node.style.border_radius < min_radius:
            return False
        if node.layout.height > max_height:
            return False

        text = " ".join((c.characters or "").strip() for c in text_children).strip()
        if not text:
            return False
        return len(text.split()) <= max_words

    @staticmethod
    def looks_like_button(node: IRNode, text_children: list[IRNode]) -> bool:
        """Проверяет, похож ли узел на кнопку.

        Пороги берутся из `analysis_rules.json -> button_detection`. Узел считается
        кнопкой, если выполнены все условия:
          * это Auto Layout (`layout.is_flex`) и есть хотя бы один TEXT-потомок;
          * `padding_top` не меньше `min_padding_vertical` (по умолчанию 6.0) и
            `padding_left` не меньше `min_padding_horizontal` (по умолчанию 12.0);
          * высота (`layout.height`) больше 0 и не больше `max_height` (по
            умолчанию 68.0);
          * узел не похож на бейдж (`looks_like_badge`).

        Проверяются только верхний и левый отступы, нижний и правый не
        учитываются.

        Args:
            node: Проверяемый узел.
            text_children: Прямые TEXT-потомки узла.

        Returns:
            bool: `True`, если узел похож на кнопку.
        """
        btn_cfg = ANALYSIS_RULES.get("button_detection", {})
        if not node.layout.is_flex or not text_children:
            return False

        min_v = float(btn_cfg.get("min_padding_vertical", 6.0))
        min_h = float(btn_cfg.get("min_padding_horizontal", 12.0))
        max_h = float(btn_cfg.get("max_height", 68.0))

        if node.layout.padding_top < min_v:
            return False
        if node.layout.padding_left < min_h:
            return False
        if not (0 < node.layout.height <= max_h):
            return False
        if ComponentHeuristics.looks_like_badge(node, text_children):
            return False
        return True

    @staticmethod
    def looks_like_card(node: IRNode) -> bool:
        """Проверяет, похож ли узел на карточку.

        Пороги берутся из `analysis_rules.json -> card_detection`, ключевые слова
        из `semantic_rules.json -> card_keywords`. Узел считается карточкой, если:
          * имя узла (в нижнем регистре) совпадает с одним из `card_keywords`;
            в этом случае размер и состав не проверяются; либо
          * одновременно есть заливка (`bg_color` задан и не `transparent`),
            размер не меньше `min_width` x `min_height` (по умолчанию
            180.0 x 100.0, по `layout.width/height`) и не меньше `min_children`
            прямых потомков (по умолчанию 2).

        Args:
            node: Проверяемый узел.

        Returns:
            bool: `True`, если узел похож на карточку.
        """
        card_cfg = ANALYSIS_RULES.get("card_detection", {})
        card_keywords = SEMANTIC_RULES.get("card_keywords", [])

        min_width = float(card_cfg.get("min_width", 180.0))
        min_height = float(card_cfg.get("min_height", 100.0))
        min_children = int(card_cfg.get("min_children", 2))

        if keyword_match(node.name.lower(), card_keywords):
            return True

        has_surface = bool(node.style.bg_color) and node.style.bg_color != "transparent"
        has_enough_children = len(node.children) >= min_children
        has_reasonable_size = node.layout.width >= min_width and node.layout.height >= min_height

        return has_surface and has_reasonable_size and has_enough_children