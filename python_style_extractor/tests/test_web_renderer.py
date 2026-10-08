# python_style_extractor/tests/test_web_renderer.py

"""Тесты `WebRenderer.node_to_html`: рендер кнопки и подстановка веб-шрифта."""

from scripts.generate_landing import WebRenderer
from scripts.models import IRNode, Geometry, IRLayout, IRStyle


def _leaf_text(node_id: str, text: str, role: str = "body", tag: str = "p") -> IRNode:
    """Создаёт листовой TEXT-узел.

    Args:
        node_id: Идентификатор узла.
        text: Текст (он же имя узла).
        role: Семантическая роль текста.
        tag: HTML-тег узла.

    Returns:
        IRNode: TEXT-узел размером 100 x 20.
    """
    return IRNode(
        id=node_id, name=text, type="TEXT",
        rel_geometry=Geometry(x=0, y=0, width=100, height=20),
        layout=IRLayout(width=100, height=20),
        style=IRStyle(), characters=text, semantic_role=role, html_tag=tag,
    )


def test_button_role_never_wraps_a_heading() -> None:
    """Кнопка с заголовком внутри рендерится одним тегом `<button>`, без вложенных кнопок."""
    heading = _leaf_text("h1", "Some Heading", role="heading_h1", tag="h1")
    root = IRNode(
        id="root", name="Frame", type="FRAME",
        rel_geometry=Geometry(x=0, y=0, width=200, height=100),
        layout=IRLayout(is_flex=True, width=200, height=100),
        style=IRStyle(), component_role="button", html_tag="button",
        children=[heading],
    )
    out = WebRenderer.node_to_html(root)
    # При нарушении тест должен упасть, а не молча сгенерировать некорректную разметку.
    assert out.count("<button") == 1


def test_registered_font_alias_is_resolved_in_css() -> None:
    """Шрифт Figma из `font_registry.json` заменяется в CSS веб-заменителем.

    Здесь `Aventa` заменяется на `Montserrat`; исходное имя в CSS не остаётся.
    """
    node = IRNode(
        id="font-test",
        name="Font Test",
        type="TEXT",
        rel_geometry=Geometry(x=0, y=0, width=300, height=40),
        layout=IRLayout(width=300, height=40),
        style=IRStyle(font_family="Aventa", font_size=20, font_weight=400),
        characters="Font Test",
        semantic_role="body",
        html_tag="p",
    )

    out = WebRenderer.node_to_html(node)

    assert "font-family: 'Montserrat', sans-serif" in out
    assert "font-family: 'Aventa', sans-serif" not in out
