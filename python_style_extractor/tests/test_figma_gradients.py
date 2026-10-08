# python_style_extractor/tests/test_figma_gradients.py

"""Тесты разбора градиентов Figma и их вывода в CSS."""

import pytest

from scripts.figma_extractor import (
    figma_gradient_to_css_angle,
    parse_gradient_fill,
)
from scripts.generate_landing import WebRenderer
from scripts.models import GradientData, GradientStop


@pytest.mark.parametrize(
    ("start", "end", "expected"),
    [
        ({"x": 0.5, "y": 0.0}, {"x": 0.5, "y": 1.0}, 180.0),
        ({"x": 0.0, "y": 0.5}, {"x": 1.0, "y": 0.5}, 90.0),
        ({"x": 0.5, "y": 1.0}, {"x": 0.5, "y": 0.0}, 0.0),
        ({"x": 1.0, "y": 0.5}, {"x": 0.0, "y": 0.5}, 270.0),
    ],
)
def test_figma_gradient_to_css_angle(start: dict[str, float], end: dict[str, float], expected: float) -> None:
    """Угол CSS по двум ручкам: вниз — 180, вправо — 90, вверх — 0, влево — 270."""
    assert figma_gradient_to_css_angle([start, end]) == expected


def test_figma_gradient_to_css_angle_missing_handles_uses_fallback() -> None:
    """Без ручек возвращается запасной угол 180°."""
    assert figma_gradient_to_css_angle([]) == 180.0


def test_figma_gradient_to_css_angle_degenerate_handles_use_fallback() -> None:
    """Совпадающие ручки (нулевой вектор) дают запасной угол 180°."""
    assert figma_gradient_to_css_angle([{"x": 0.5, "y": 0.5}, {"x": 0.5, "y": 0.5}]) == 180.0


def test_radial_gradient_preserves_center() -> None:
    """Радиальный градиент сохраняет центр из первой ручки (в процентах)."""
    fill = {
        "type": "GRADIENT_RADIAL",
        "gradientHandlePositions": [
            {"x": 0.65, "y": 0.20},
            {"x": 0.65, "y": 1.00},
            {"x": 1.00, "y": 0.20},
        ],
        "gradientStops": [
            {"position": 0.0, "color": {"r": 1.0, "g": 1.0, "b": 1.0, "a": 1.0}},
            {"position": 1.0, "color": {"r": 0.0, "g": 0.0, "b": 0.0, "a": 0.0}},
        ],
    }

    grad = parse_gradient_fill(fill, fill_opacity=1.0)

    assert grad is not None
    assert grad.css_type == "radial"
    assert grad.center_x_pct == pytest.approx(65.0)
    assert grad.center_y_pct == pytest.approx(20.0)


def test_angular_gradient_maps_to_conic() -> None:
    """Угловой градиент Figma превращается в CSS `conic-gradient` с центром из первой ручки."""
    fill = {
        "type": "GRADIENT_ANGULAR",
        "gradientHandlePositions": [
            {"x": 0.5, "y": 0.5},
            {"x": 1.0, "y": 0.5},
            {"x": 0.5, "y": 1.0},
        ],
        "gradientStops": [
            {"position": 0.0, "color": {"r": 1.0, "g": 0.0, "b": 0.0, "a": 1.0}},
            {"position": 1.0, "color": {"r": 0.0, "g": 0.0, "b": 1.0, "a": 1.0}},
        ],
    }

    grad = parse_gradient_fill(fill, fill_opacity=1.0)

    assert grad is not None
    assert grad.css_type == "conic"
    assert grad.center_x_pct == pytest.approx(50.0)
    assert grad.center_y_pct == pytest.approx(50.0)


def test_diamond_gradient_uses_explicit_approximation() -> None:
    """Diamond-градиент приближается `conic-градиентом`: точного аналога в CSS нет."""
    fill = {
        "type": "GRADIENT_DIAMOND",
        "gradientHandlePositions": [
            {"x": 0.5, "y": 0.5},
            {"x": 0.5, "y": 1.0},
            {"x": 1.0, "y": 0.5},
        ],
        "gradientStops": [
            {"position": 0.0, "color": {"r": 1.0, "g": 1.0, "b": 1.0, "a": 1.0}},
            {"position": 1.0, "color": {"r": 0.0, "g": 0.0, "b": 0.0, "a": 1.0}},
        ],
    }

    grad = parse_gradient_fill(fill, fill_opacity=1.0)

    assert grad is not None
    assert grad.css_type == "conic"


def test_radial_gradient_renderer_uses_center() -> None:
    """`WebRenderer.gradient_to_css` выводит центр радиального градиента в строку CSS."""
    grad = GradientData(
        figma_type="GRADIENT_RADIAL",
        css_type="radial",
        center_x_pct=65.0,
        center_y_pct=20.0,
        stops=[
            GradientStop(color="rgba(255, 255, 255, 1.0)", position=0.0),
            GradientStop(color="rgba(0, 0, 0, 0.0)", position=1.0),
        ],
    )

    css = WebRenderer.gradient_to_css(grad)

    assert css == (
        "radial-gradient("
        "ellipse at 65.00% 20.00%, "
        "rgba(255, 255, 255, 1.0) 0.0%, "
        "rgba(0, 0, 0, 0.0) 100.0%"
        ")"
    )
