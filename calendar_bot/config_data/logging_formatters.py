# config_data/logging_formatters.py

"""
Форматтеры логов для консоли и файла.

ColoredLevelFormatter — разный цвет на каждый уровень (подход из fit_trainer_bot),
но через makeLogRecord (подход из handwriting_converter) — оригинальный record не мутируется,
поэтому FileHandler не получает ANSI-мусора.

FileEmojiFormatter — эмодзи вместо ANSI в файловых логах: читаемо везде (grep, IDE, tail -f).
"""

import logging

from colorama import Back, Fore, Style, init

init(autoreset=True)

# ── Цвета для консоли (весь уровень целиком, а не только levelname) ──────────
_LEVEL_COLORS: dict[int, str] = {
    logging.DEBUG: Fore.CYAN,
    logging.INFO: Fore.GREEN + Style.BRIGHT,
    logging.WARNING: Back.YELLOW + Style.BRIGHT + Fore.BLACK,
    logging.ERROR: Back.MAGENTA + Style.BRIGHT + Fore.BLACK,
    logging.CRITICAL: Back.RED + Style.BRIGHT + Fore.WHITE,
}

# ── Эмодзи-иконки для файловых логов ─────────────────────────────────────────
_FILE_ICONS: dict[int, str] = {
    logging.DEBUG: "🔵 DEBUG   ",
    logging.INFO: "🟢 INFO    ",
    logging.WARNING: "🟡 WARNING ",
    logging.ERROR: "🔴 ERROR   ",
    logging.CRITICAL: "💀 CRITICAL",
}

_DATE_FMT = "%Y-%m-%d %H:%M:%S"

# Шаблон консоли: время │ уровень │ модуль │ файл:строка │ функция │ сообщение
# %(pathname)s вместо %(filename)s — полный путь к файлу (удобнее при нескольких
# модулях с одинаковыми именами файлов).
_CONSOLE_TEMPLATE = (
    f"{Fore.MAGENTA}%(asctime)s{Style.RESET_ALL}"
    " │ %(levelname)s"
    f" │ {Fore.BLUE}%(name)-20s{Style.RESET_ALL}"
    f" │ {Fore.LIGHTCYAN_EX}%(pathname)s{Style.RESET_ALL}"
    f":{Fore.YELLOW}%(lineno)d{Style.RESET_ALL}"
    f" │ {Fore.GREEN}%(funcName)s{Style.RESET_ALL}"
    f" │ {Style.BRIGHT}{Fore.WHITE}%(message)s{Style.RESET_ALL}"
)

_FILE_TEMPLATE = (
    "%(asctime)s | %(levelname)-12s | %(name)-20s | "
    "%(pathname)s:%(lineno)d | %(funcName)s | %(message)s"
)


class ColoredLevelFormatter(logging.Formatter):
    """
    Консольный форматтер с per-level раскраской.

    Ключевая особенность: каждый уровень логирования имеет свой уникальный
    цвет для ВСЕЙ строки (не только для levelname), как в fit_trainer_bot —
    при беглом взгляде на терминал сразу видно критичность записи.

    Работает через makeLogRecord (копия записи) — оригинальный LogRecord
    не мутируется, поэтому FileHandler, если он зарегистрирован параллельно,
    получает чистый текст без ANSI-кодов.
    """

    def format(self, record: logging.LogRecord) -> str:
        color = _LEVEL_COLORS.get(record.levelno, "")
        reset = Style.RESET_ALL

        # Копируем запись — не трогаем оригинал
        r = logging.makeLogRecord(record.__dict__)

        # Красим levelname + сообщение целиком
        r.levelname = f"{color}{record.levelname:<8}{reset}"
        r.msg = f"{color}{record.getMessage()}{reset}"
        r.args = ()  # getMessage() уже применил args — сбрасываем, чтобы не было двойного форматирования

        return logging.Formatter(_CONSOLE_TEMPLATE, datefmt=_DATE_FMT).format(r)


class FileEmojiFormatter(logging.Formatter):
    """
    Файловый форматтер с эмодзи-иконками вместо ANSI-кодов.

    Почему эмодзи, а не просто текст?
    - Читаемо в любом редакторе и при `grep`/`tail -f`
    - Мгновенно виден уровень без чтения слова ('🔴' vs 'ERROR')
    - Не засоряет файл ANSI escape-последовательностями (\x1b[31m и т.п.)
    """

    def format(self, record: logging.LogRecord) -> str:
        r = logging.makeLogRecord(record.__dict__)
        r.levelname = _FILE_ICONS.get(record.levelno, "⚪ UNKNOWN  ")
        return logging.Formatter(_FILE_TEMPLATE, datefmt=_DATE_FMT).format(r)