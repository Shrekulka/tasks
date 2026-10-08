# python_style_extractor/tests/test_reference_bg_detection.py

"""Тесты определения цвета фона секции.

Проверяются кандидаты на фон (`collect_background_candidates`) и цвет фона
эталонного рендера (`ReferenceRenderer.resolve_reference_bg_color`).
"""

from pathlib import Path

from PIL import Image

from scripts.color_resolution import (
    collect_background_candidates,
)
from scripts.generate_landing import ReferenceRenderer
from scripts.models import IRNode, Geometry, IRLayout, IRStyle
from scripts.models import ResolvedSectionSpec, SourceRef


def _create_node(node_id: str, name: str, node_type: str, x: float, y: float, w: float, h: float,
                 bg_color: str | None, blend_mode: str = "PASS_THROUGH", bg_is_gradient: bool = False,
                 opacity: float = 1.0, parent_w: float = 0, parent_h: float = 0) -> IRNode:
    """Создаёт узел IR с геометрией и заливкой для тестов.

    Args:
        node_id: Идентификатор узла.
        name: Имя узла.
        node_type: Тип узла (он же записывается в `extras["raw_type"]`).
        x: Координата X относительно родителя, px.
        y: Координата Y относительно родителя, px.
        w: Ширина, px.
        h: Высота, px.
        bg_color: Цвет заливки.
        blend_mode: Режим наложения.
        bg_is_gradient: `bg_color` — аппроксимация градиента.
        opacity: Непрозрачность узла.
        parent_w: Ширина родителя, px.
        parent_h: Высота родителя, px.

    Returns:
        IRNode: Созданный узел без детей.
    """
    return IRNode(
        id=node_id,
        name=name,
        type=node_type,
        rel_geometry=Geometry(x=x, y=y, width=w, height=h, parent_width=parent_w, parent_height=parent_h),
        layout=IRLayout(width=w, height=h),
        style=IRStyle(bg_color=bg_color, blend_mode=blend_mode, bg_is_gradient_approx=bg_is_gradient, opacity=opacity),
        extras={"raw_type": node_type}
    )


def test_gradient_glow_and_hue_overlay_excluded_from_bg_vote() -> None:
    """Градиентное свечение и HUE-оверлей не должны побеждать в голосовании за фон."""
    # Корневой фрейм секции.
    root = _create_node("root", "Root", "FRAME", 0, 0, 1200, 800, bg_color=None)
    # Декоративное свечение (ASSET, градиентная аппроксимация).
    glow = _create_node("glow", "Vector", "ASSET", 0, 0, 1000, 1000, "rgba(255,255,255,1.0)",
                        bg_is_gradient=True, parent_w=1200, parent_h=800)
    # HUE-наложение.
    hue = _create_node("hue", "Rectangle", "RECTANGLE", 0, 0, 900, 900, "rgba(114,233,95,1.0)",
                       blend_mode="HUE", parent_w=1200, parent_h=800)
    # Реальный фон.
    real_bg = _create_node("bg", "CTA", "FRAME", 0, 0, 1200, 800, "rgba(157,157,157,1.0)",
                           parent_w=1200, parent_h=800)
    root.children = [glow, hue, real_bg]

    cfg = {
        "exclude_node_types": ["ASSET"],
        "exclude_raw_types": ["VECTOR", "BOOLEAN_OPERATION", "STAR", "LINE", "REGULAR_POLYGON"],
        "allowed_blend_modes": ["NORMAL", "PASS_THROUGH"],
        "gradient_penalty": 0.6,
        "min_coverage_ratio": 0.35,
        "depth_priority_falloff": 0.02,
        "png_fallback_resize": 40
    }

    candidates = collect_background_candidates(root, root_w=1200, root_h=800, cfg=cfg)
    # Должен остаться только real_bg (фон).
    assert len(candidates) == 1
    assert candidates[0][1] == "rgba(157,157,157,1.0)"


def test_falls_back_to_png_sampling_when_no_node_reaches_coverage_threshold(tmp_path: Path) -> None:
    """Если ни один узел не покрывает 35% секции, срабатывает PNG-сэмплинг."""
    # Секция с маленькими фоновыми полосками (coverage < 0.35).
    root = _create_node("root", "Root", "FRAME", 0, 0, 1200, 800, bg_color=None)
    strip1 = _create_node("strip1", "Blur Down", "RECTANGLE", 0, 0, 1200, 100, "rgba(2,1,13,1.0)",
                          parent_w=1200, parent_h=800)
    strip2 = _create_node("strip2", "Blur Down 2", "RECTANGLE", 0, 700, 1200, 100, "rgba(2,1,13,1.0)",
                          parent_w=1200, parent_h=800)
    root.children = [strip1, strip2]

    # PNG-файл с доминирующим тёмным цветом.
    png_path = tmp_path / "ref.png"
    img = Image.new("RGB", (100, 100), color=(2, 1, 13))  # тёмный
    img.save(png_path)

    section = ResolvedSectionSpec(
        id="test_section",
        name="Test",
        source=SourceRef(resource_id="file_key", node_id="node"),
        geometry=root.rel_geometry,
        resolved_root=root,
        original_root=root,
        assets=[],
        reference_image=str(png_path),
        responsive_capable=False
    )

    bg_color, source = ReferenceRenderer.resolve_reference_bg_color(section, design_system=None)
    assert source == "png_sampling"
    assert bg_color == "rgba(2, 1, 13, 1.0)"


def test_absolute_figma_root_coordinates_do_not_break_coverage() -> None:
    """Фон находится на нормализованном дереве, даже если корень лежит в абсолютных координатах Figma.

    `FigmaNormalizer.normalize_node` обнуляет `rel_geometry.x/y` корня
    (`is_root = p_w == 0.0 and p_h == 0.0`), а исходные абсолютные координаты
    сохраняет в `absolute_x/absolute_y` для диагностики. Поэтому
    `collect_background_candidates` смещение корня сама не компенсирует.

    Тест прогоняет сырой Figma-подобный узел с `absoluteBoundingBox` в абсолютных
    координатах (x=45137, y=76116) через настоящий `normalize_node` и проверяет
    два инварианта:
      1. `normalize_node` обнуляет координаты корня и сохраняет абсолютные;
      2. скоринг фона находит фон на нормализованном дереве без ручной
         компенсации координат.
    """
    from scripts.figma_extractor import FigmaNormalizer

    raw_root = {
        "id": "root",
        "name": "Root",
        "type": "FRAME",
        "absoluteBoundingBox": {
            "x": 45137.0, "y": 76116.0, "width": 1726.0, "height": 707.0
        },
        "fills": [],
        "children": [
            {
                "id": "bg",
                "name": "Background",
                "type": "FRAME",
                "absoluteBoundingBox": {
                    "x": 45137.0, "y": 76116.0, "width": 1726.0, "height": 707.0
                },
                "fills": [
                    {
                        "type": "SOLID",
                        "visible": True,
                        "opacity": 1.0,
                        "color": {"r": 0.0392, "g": 0.0784, "b": 0.1176, "a": 1.0},
                    }
                ],
                "children": [],
            }
        ],
    }

    root_ir = FigmaNormalizer.normalize_node(raw_root)

    # --- Инвариант 1: normalize_node обнуляет координаты КОРНЯ ---
    assert root_ir.rel_geometry.x == 0.0
    assert root_ir.rel_geometry.y == 0.0
    # Абсолютные координаты не теряются: они нужны для диагностики и QA.
    assert root_ir.rel_geometry.absolute_x == 45137.0
    assert root_ir.rel_geometry.absolute_y == 76116.0

    # Дочерний узел получает ЛОКАЛЬНОЕ смещение относительно родителя
    # (тоже 0, 0: его абсолютные координаты совпадают с координатами корня).
    bg_node = root_ir.children[0]
    assert bg_node.rel_geometry.x == 0.0
    assert bg_node.rel_geometry.y == 0.0

    cfg = {
        "exclude_node_types": ["ASSET"],
        "exclude_raw_types": ["VECTOR", "BOOLEAN_OPERATION", "STAR", "LINE", "REGULAR_POLYGON"],
        "allowed_blend_modes": ["NORMAL", "PASS_THROUGH"],
        "gradient_penalty": 0.6,
        "min_coverage_ratio": 0.35,
        "min_alpha": 0.4,
        "depth_priority_falloff": 0.02,
    }

    # --- Инвариант 2: скоринг фона работает на нормализованном дереве ---
    candidates = collect_background_candidates(
        root_ir, root_ir.rel_geometry.width, root_ir.rel_geometry.height, cfg
    )
    assert len(candidates) == 1
    assert candidates[0][1] == "rgba(10, 20, 30, 1.0)"


def test_low_alpha_glass_surface_excluded_even_with_high_coverage() -> None:
    """«Стеклянная» панель с альфой 0.01 не может быть фоном, даже при большом покрытии.

    Панель `rgba(157, 157, 157, 0.01)` покрывает около 58% секции, но на деле
    почти прозрачна. `min_alpha` отсекает такие узлы жёстко, иначе после
    нормализации координат она осталась бы единственным кандидатом и «выиграла»
    бы по умолчанию.
    """
    root = _create_node("root", "Root", "FRAME", 0, 0, 1726, 707, bg_color=None)
    cta = _create_node(
        "cta", "CTA", "FRAME", 194, 89, 1338, 529,
        "rgba(157, 157, 157, 0.01)", parent_w=1726, parent_h=707,
    )
    root.children = [cta]

    cfg = {
        "exclude_node_types": ["ASSET"],
        "exclude_raw_types": ["VECTOR", "BOOLEAN_OPERATION", "STAR", "LINE", "REGULAR_POLYGON"],
        "allowed_blend_modes": ["NORMAL", "PASS_THROUGH"],
        "gradient_penalty": 0.6,
        "min_coverage_ratio": 0.35,
        "min_alpha": 0.4,
        "depth_priority_falloff": 0.02,
    }

    candidates = collect_background_candidates(root, root_w=1726, root_h=707, cfg=cfg)
    assert candidates == []
