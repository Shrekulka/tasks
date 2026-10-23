# forest-advice-bot/scripts/gen_config_docs.py

"""Генерирует docs/configuration.md из .env.example: переменные, значения и пояснения.

Единый источник правды один: .env.example. Таблица не правится руками, поэтому не устаревает.
    python -m scripts.gen_config_docs            # обновить docs/configuration.md
    python -m scripts.gen_config_docs --check    # проверить, что файл актуален (код выхода 1)
"""

import re
import sys
from pathlib import Path

ENV_EXAMPLE = Path(".env.example")
OUTPUT = Path("docs/configuration.md")

_SECTION_RE = re.compile(r"^#\s*-{3,}\s*(?P<title>.+?)\s*-{3,}\s*$")
_VAR_RE = re.compile(r"^(?P<name>[A-Z][A-Z0-9_]*)=(?P<value>.*)$")

HEADER = """# Переменные окружения

Файл создаётся скриптом `python -m scripts.gen_config_docs` из `.env.example`. Не редактируйте его
вручную: правьте комментарии и значения в `.env.example`.
"""


def parse_env_example(text: str) -> list[tuple[str, list[tuple[str, str, str]]]]:
    """[(раздел, [(имя, значение по умолчанию, описание)])]."""
    sections: list[tuple[str, list[tuple[str, str, str]]]] = []
    pending: list[str] = []
    last_description = ""
    for raw in text.splitlines():
        line = raw.strip()
        section = _SECTION_RE.match(line)
        if section:
            sections.append((section["title"], []))
            pending = []
            continue
        if line.startswith("#"):
            pending.append(line.lstrip("#").strip())
            continue
        match = _VAR_RE.match(line)
        if match and sections:
            description = " ".join(part for part in pending if part)
            if description:
                last_description = description
            else:
                description = "(см. описание выше)" if last_description else ""
            sections[-1][1].append((match["name"], match["value"], description))
            pending = []
        elif not line:
            continue
    return sections


def _cell(text: str) -> str:
    return text.replace("|", "\\|") or "—"


def render(text: str) -> str:
    lines = [HEADER]
    for title, variables in parse_env_example(text):
        if not variables:
            continue
        lines += [f"## {title}", "", "| Переменная | По умолчанию | Назначение |", "|---|---|---|"]
        for name, value, description in variables:
            default = f"`{value}`" if value else "(пусто)"
            lines.append(f"| `{name}` | {default} | {_cell(description)} |")
        lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"


def main(argv: list[str]) -> int:
    expected = render(ENV_EXAMPLE.read_text(encoding="utf-8"))
    if "--check" in argv:
        actual = OUTPUT.read_text(encoding="utf-8") if OUTPUT.exists() else ""
        if actual != expected:
            print(f"{OUTPUT} устарел: выполните python -m scripts.gen_config_docs", file=sys.stderr)
            return 1
        return 0
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(expected, encoding="utf-8")
    print(f"Обновлён {OUTPUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
