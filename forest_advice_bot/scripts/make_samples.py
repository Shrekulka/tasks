# forest-advice-bot/scripts/make_samples.py

"""Создаёт вымышленные документы в samples/archive/.

Запуск из корня проекта: python -m scripts.make_samples [каталог]
Текстовый PDF (tuman-orientyry.pdf) собирается отдельно: scripts/make_sample_pdf.py.
Нужны python-docx и pypdf (оба нужны проекту и для парсеров). Текстовые PDF в скрипте не
создаются: сохраните любой из DOCX как PDF (Word или LibreOffice: «Сохранить как PDF»).
"""

import shutil
import sys
from pathlib import Path

from docx import Document
from pypdf import PdfWriter

DEFAULT_DIR = Path("samples/archive")

# Короткие советы: имя файла -> (заголовок, абзацы). Все персонажи и факты вымышлены.
SHORT_TXT = {
    "zymovi-zapasy.txt": (
        "Зимові запаси",
        [
            "Горіхи й жолуді краще ховати у кількох місцях, а не в одній норі.",
            "Перевіряйте запаси раз на місяць: вологі жолуді швидко пліснявіють.",
        ],
    ),
    "vodopij-v-posukhu.txt": (
        "Водопій у посуху",
        [
            "У посуху ходіть до струмка на світанку: вода тоді найчистіша.",
            "Не пийте з калюж, де вода стоїть більше трьох днів.",
        ],
    ),
}

# Два документи з протилежними порадами про один і той самий випадок (перевірка T13).
CONFLICT_DOCX = {
    "zustrich-z-vovkom-pora-1.docx": (
        "Зустріч з вовком: порада старого борсука",
        ["Якщо зустріли вовка, стійте нерухомо й не дивіться йому в очі. Тікати не можна."],
    ),
    "zustrich-z-vovkom-pora-2.docx": (
        "Зустріч з вовком: порада молодої білки",
        ["Якщо зустріли вовка, негайно лізьте на найвище дерево. Нерухомо стояти небезпечно."],
    ),
}

# Ещё один DOCX на отдельную тему (не пересекается с остальными вопросами набора).
EXTRA_DOCX = {
    "khovanky-pravyla.docx": (
        "Правила гри в хованки для малих звірят",
        [
            "Той, хто водить, рахує до двадцяти й не підглядає.",
            "Ховатися можна лише в межах галявини: у чагарник за струмком заходити заборонено.",
            "Знайденого звірятка не лякають: його просто називають на ім'я.",
        ],
    ),
}

GUIDE_SECTIONS = [
    ("Вступ", "Цей путівник допоможе звіринцю пережити будь-яку пору року."),
    ("Весна", "Навесні стежте за повенями: низькі нори заливає першими."),
    ("Літо", "Влітку ховайтеся від спеки в тіні старих дубів."),
    ("Осінь", "Восени запасайте їжу, але не залишайте її на відкритих місцях."),
    ("Зима", "Узимку тримайтеся разом: гуртом тепліше."),
    ("Хвороби", "Хворого звіра не виганяйте: ізолюйте на кілька днів і носіть йому воду."),
    ("Гості з міста", "Незнайомих гостей зустрічайте обережно й не показуйте їм запаси."),
]


def _write_docx(path: Path, title: str, paragraphs: list[str]) -> None:
    document = Document()
    document.add_heading(title, level=1)
    for text in paragraphs:
        document.add_paragraph(text)
    document.save(path)


def main(target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)

    for name, (title, paragraphs) in SHORT_TXT.items():
        (target / name).write_text("\n\n".join([title, *paragraphs]) + "\n", encoding="utf-8")

    # Дубль: побайтово одинаковый файл под другим именем (перевірка D5 и дублей архіву).
    # Имя копии должно сортироваться ПОСЛЕ оригинала: при дубликатах канонический путь это
    # лексикографически первый, и название документа берётся из него. Символ «_» больше «.»,
    # а «-» меньше, поэтому «zymovi-zapasy-kopiya.txt» стал бы «оригиналом».
    shutil.copyfile(target / "zymovi-zapasy.txt", target / "zymovi-zapasy_kopiya.txt")

    for name, (title, paragraphs) in {**CONFLICT_DOCX, **EXTRA_DOCX}.items():
        _write_docx(target / name, title, paragraphs)

    # Довгий путівник: кожен розділ повторюється кілька разів, щоб вийшов великий документ.
    guide = Document()
    guide.add_heading("Великий лісовий путівник", level=1)
    for title, text in GUIDE_SECTIONS:
        guide.add_heading(title, level=2)
        for part in range(1, 6):
            guide.add_paragraph(f"{text} (нотатка {part}). " * 6)
    guide.save(target / "velykyi-putivnyk.docx")

    # PDF без текстового шару: порожня сторінка (перевірка відмови для сканів, D10).
    writer = PdfWriter()
    writer.add_blank_page(width=595, height=842)
    with (target / "skan-bez-tekstu.pdf").open("wb") as handle:
        writer.write(handle)

    print(f"Створено {len(list(target.iterdir()))} файлів у {target}")


if __name__ == "__main__":
    main(Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_DIR)
