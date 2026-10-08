# python_style_extractor/tests/test_native_mode_text_fallback.py

"""Тесты подстановки цвета TEXT-узлам без `text_color` в `StyleTransferEngine`."""

from scripts.generate_landing import StyleTransferEngine, RENDER_RULES
from scripts.models import (
    IRNode, Geometry, IRLayout, IRStyle, SourceRef, SectionSpec, TransformSpec,
    DesignSystemSpec, DesignTokens, ResolvedToken, TypographyToken,
)


def _text_node_without_color(node_id: str = "txt", role: str = "heading_h1") -> IRNode:
    """Создаёт TEXT-узел без `text_color`.

    Args:
        node_id: Идентификатор узла.
        role: Семантическая роль текста.

    Returns:
        IRNode: TEXT-узел, у которого цвет не определён на этапе извлечения.
    """
    return IRNode(
        id=node_id, name="Heading", type="TEXT",
        rel_geometry=Geometry(x=0, y=0, width=300, height=40, parent_width=600, parent_height=200),
        layout=IRLayout(width=300, height=40),
        style=IRStyle(text_color=None),  # цвет не определён на этапе извлечения
        characters="Some heading text",
        semantic_role=role,
    )


def _minimal_design_system() -> DesignSystemSpec:
    """Создаёт минимальную дизайн-систему с токенами, нужными для переноса стиля.

    Returns:
        DesignSystemSpec: Дизайн-система с цветами, типографикой `body`, радиусами
        и отступом `md`.
    """
    return DesignSystemSpec(
        source=SourceRef(resource_id="file_key", node_id="1:1"),
        tokens=DesignTokens(
            colors={
                "canvas": ResolvedToken(value="#000000", source="config"),
                "surface": ResolvedToken(value="#111111", source="config"),
                "primary": ResolvedToken(value="#2563eb", source="config"),
                "text_primary": ResolvedToken(value="#ffffff", source="config"),
                "text_secondary": ResolvedToken(value="#94a3b8", source="config"),
                "border": ResolvedToken(value="rgba(255,255,255,0.08)", source="config"),
            },
            typography={"body": TypographyToken(family="Inter", size=16, weight=400)},
            radii={"button": 8.0, "card": 16.0},
            spacing={"md": 16.0},
        ),
        components={"button": []},
    )


def test_native_mode_section_gets_fallback_text_color_from_style_transfer() -> None:
    """В режиме `transform.mode='native'` TEXT-узел без `text_color` получает цвет по роли.

    Подстановка цвета по умолчанию выполняется в `StyleTransferEngine` и вне
    режима `style_transfer`.
    """
    root = IRNode(
        id="root", name="Root", type="FRAME",
        rel_geometry=Geometry(x=0, y=0, width=600, height=200),
        layout=IRLayout(width=600, height=200),
        style=IRStyle(),
        children=[_text_node_without_color()],
    )
    section = SectionSpec(
        id="native_section", name="Native",
        source=SourceRef(resource_id="file_key", node_id="1:1"),
        geometry=root.rel_geometry,
        root_node=root,
        transform=TransformSpec(mode="native"),
    )
    ds = _minimal_design_system()

    resolved = StyleTransferEngine.transfer(section, ds)
    text_node = resolved.resolved_root.children[0]

    assert text_node.style.text_color is not None
    role_color_map = RENDER_RULES.get("role_color_map", {})
    expected_key = role_color_map.get("heading_h1", role_color_map.get("__default__", "text_secondary"))
    expected_color = ds.tokens.colors[expected_key].value
    assert text_node.style.text_color == str(expected_color)


def test_style_transfer_mode_does_not_override_fallback_behavior() -> None:
    """В режиме `style_transfer` текст без цвета тоже получает цвет.

    Подстановка по умолчанию не конфликтует с обычным путём назначения цвета.
    """
    root = IRNode(
        id="root", name="Root", type="FRAME",
        rel_geometry=Geometry(x=0, y=0, width=600, height=200),
        layout=IRLayout(width=600, height=200),
        style=IRStyle(),
        children=[_text_node_without_color(role="body")],
    )
    section = SectionSpec(
        id="transfer_section", name="Transfer",
        source=SourceRef(resource_id="file_key", node_id="1:1"),
        geometry=root.rel_geometry,
        root_node=root,
        transform=TransformSpec(mode="style_transfer"),
    )
    ds = _minimal_design_system()

    resolved = StyleTransferEngine.transfer(section, ds)
    text_node = resolved.resolved_root.children[0]
    assert text_node.style.text_color is not None
