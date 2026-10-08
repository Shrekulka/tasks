# python_style_extractor/tests/test_gradient_text_color.py

"""Тесты цвета текста для TEXT-узлов с градиентной и обычной заливкой."""

from typing import Any

from scripts.figma_extractor import FigmaNormalizer


def _text_node_with_gradient_fill() -> dict[str, Any]:
    """Возвращает минимальный сырой Figma-узел TEXT с заливкой GRADIENT_LINEAR.

    В самой заливке нет ключа `color`: цвет задан только в `gradientStops`.

    Returns:
        dict[str, Any]: Узел в формате Figma API.
    """
    return {
        "id": "1122:6773",
        "name": "Create and share your Affiliate link",
        "type": "TEXT",
        "characters": "Create and share your Affiliate link",
        "opacity": 1.0,
        "absoluteBoundingBox": {"x": 100, "y": 100, "width": 326, "height": 14},
        "style": {
            "fontFamily": "Aventa",
            "fontSize": 20.0,
            "fontWeight": 400,
            "lineHeightPercent": 80.97,
            "letterSpacing": -0.4,
        },
        "fills": [
            {
                "blendMode": "NORMAL",
                "type": "GRADIENT_LINEAR",
                "visible": True,
                "gradientHandlePositions": [
                    {"x": 0.5, "y": 0.0}, {"x": 0.5, "y": 1.0}, {"x": 0.49, "y": 0.0}
                ],
                "gradientStops": [
                    {"color": {"r": 0.8275, "g": 0.8353, "b": 0.898, "a": 1.0}, "position": 0.0},
                    {"color": {"r": 0.6824, "g": 0.6902, "b": 0.7882, "a": 1.0}, "position": 1.0},
                ],
            }
        ],
        "strokes": [],
        "children": [],
    }


def test_gradient_fill_text_gets_approximated_color() -> None:
    """Регрессионный тест: `text_color` для TEXT с GRADIENT_LINEAR не должен быть None.

    Цвет аппроксимируется средней точкой градиента (индекс `len // 2`, то есть 1
    для двух остановок), как это сделано для `bg_color`.
    """
    node = _text_node_with_gradient_fill()
    ir = FigmaNormalizer.normalize_node(node)

    assert ir.style.text_color is not None
    # rgba(round(0.6824*255), round(0.6902*255), round(0.7882*255), 1.0)
    assert ir.style.text_color == "rgba(174, 176, 201, 1.0)"


def test_solid_fill_text_still_works_as_before() -> None:
    """Обычная SOLID-заливка текста по-прежнему даёт точный цвет."""
    node = _text_node_with_gradient_fill()
    node["fills"] = [
        {"type": "SOLID", "visible": True, "opacity": 1.0,
         "color": {"r": 1.0, "g": 1.0, "b": 1.0, "a": 1.0}}
    ]
    ir = FigmaNormalizer.normalize_node(node)
    assert ir.style.text_color == "rgba(255, 255, 255, 1.0)"


def test_invisible_fill_is_skipped_and_falls_through_to_next() -> None:
    """Заливка с `visible=False` пропускается, если следующая по списку заливка валидна."""
    node = _text_node_with_gradient_fill()
    node["fills"] = [
        {"type": "SOLID", "visible": False, "color": {"r": 0, "g": 0, "b": 0, "a": 1.0}},
        {"type": "SOLID", "visible": True, "opacity": 1.0,
         "color": {"r": 0.2, "g": 0.4, "b": 0.6, "a": 1.0}},
    ]
    ir = FigmaNormalizer.normalize_node(node)
    assert ir.style.text_color == "rgba(51, 102, 153, 1.0)"
