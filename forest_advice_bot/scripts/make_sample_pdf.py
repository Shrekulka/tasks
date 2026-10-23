# forest-advice-bot/scripts/make_sample_pdf.py

"""Один раз собирает текстовый PDF на украинском для samples/archive/.

Нужен reportlab (только для этого скрипта, в зависимости проекта он не входит):
    pip install reportlab
    python -m scripts.make_sample_pdf /usr/share/fonts/truetype/dejavu/DejaVuSans.ttf

Первый аргумент: путь к TTF-шрифту с кириллицей (DejaVu Sans, Noto Sans и подобные).
Готовый PDF лежит в репозитории, повторно запускать скрипт не нужно.
"""

import sys
from pathlib import Path

from reportlab.lib.pagesizes import A4
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

DEFAULT_OUTPUT = Path("samples/archive/tuman-orientyry.pdf")
FONT_NAME = "SampleFont"

PAGES = [
    (
        "Як не заблукати в тумані",
        [
            "У густому тумані не відходьте далеко від знайомої стежки.",
            "Рухайтеся повільно й час від часу подавайте голос, щоб друзі могли вас знайти.",
            "Орієнтуйтеся за струмком: він завжди тече вниз, до річки.",
        ],
    ),
    (
        "Що робити, якщо ви все ж заблукали",
        [
            "Зупиніться й не біжіть навмання: так легко піти ще далі від дому.",
            "Дочекайтеся, поки туман розсіється, або знайдіть сухе укриття під великим деревом.",
            "Не залишайте слідів їжі: вони приваблюють небажаних гостей.",
        ],
    ),
]


def build(font_path: Path, output: Path) -> None:
    pdfmetrics.registerFont(TTFont(FONT_NAME, str(font_path)))
    output.parent.mkdir(parents=True, exist_ok=True)
    pdf = canvas.Canvas(str(output), pagesize=A4, invariant=1)  # invariant: стабильные байты
    width, height = A4
    for title, lines in PAGES:
        pdf.setFont(FONT_NAME, 18)
        pdf.drawString(60, height - 80, title)
        pdf.setFont(FONT_NAME, 12)
        y = height - 120
        for line in lines:
            pdf.drawString(60, y, line)
            y -= 22
        pdf.showPage()
    pdf.save()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    build(Path(sys.argv[1]), Path(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_OUTPUT)
    print(f"Створено {DEFAULT_OUTPUT}")
