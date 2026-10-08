# python_style_extractor/scripts/ir_fidelity.py

"""Структурная проверка достоверности извлечения Figma → IR.

Результат проверки разделён на две категории:

    * issues (блокирующие): отрицательные ширина или высота узла, TEXT-узел без
      `characters`, несовпадение `parent_width`/`parent_height` ребёнка с
      размерами родителя. В корректном IR этих значений быть не может.
    * warnings (неблокирующие): сигналы вроде TEXT-узла без `text_color`. В норме
      на этапе извлечения такого быть не должно, но если на новом макете оно
      появится, предупреждение в отчёте заметнее, чем молчаливая регрессия.
"""

from typing import Any

from scripts.models import IRNode, ResolvedSectionSpec


class IRFidelityChecker:
    """Проверяет достоверность IR-дерева секции после извлечения из Figma."""

    @staticmethod
    def check(section: ResolvedSectionSpec) -> dict[str, Any]:
        """Проверяет исходное дерево секции (`original_root`) и собирает замечания.

        Блокирующие замечания (`issues`):
          * отрицательные `rel_geometry.width` или `rel_geometry.height`;
          * TEXT-узел с `characters is None`;
          * `parent_width` или `parent_height` ребёнка не равны ширине или высоте
            родителя (сравнение точное, без допуска).

        Неблокирующее замечание (`warnings`): TEXT-узел без `text_color`.

        Args:
            section: Секция; проверяется её `original_root`.

        Returns:
            dict[str, Any]: Словарь с ключами `passed` (`True`, если блокирующих
            замечаний нет), `issues` и `warnings` (списки описаний).
        """
        issues: list[str] = []
        warnings: list[str] = []
        root = section.original_root

        def walk(node: IRNode) -> None:
            """Проверяет `node` и рекурсивно его детей, дополняя `issues` и `warnings`.

            Args:
                node: Текущий узел.
            """
            if node.rel_geometry.width < 0:
                issues.append(f"{node.id}: negative width ({node.rel_geometry.width})")
            if node.rel_geometry.height < 0:
                issues.append(f"{node.id}: negative height ({node.rel_geometry.height})")

            if node.type == "TEXT":
                if node.characters is None:
                    issues.append(f"{node.id}: TEXT node without characters")
                if not node.style.text_color:
                    warnings.append(f"{node.id}: TEXT node without text_color at extraction stage")

            for child in node.children:
                if child.rel_geometry.parent_width != node.rel_geometry.width:
                    issues.append(
                        f"{child.id}: invalid parent_width ({child.rel_geometry.parent_width} != {node.rel_geometry.width})"
                    )
                if child.rel_geometry.parent_height != node.rel_geometry.height:
                    issues.append(
                        f"{child.id}: invalid parent_height ({child.rel_geometry.parent_height} != {node.rel_geometry.height})"
                    )
                walk(child)

        walk(root)
        return {"passed": not issues, "issues": issues, "warnings": warnings}