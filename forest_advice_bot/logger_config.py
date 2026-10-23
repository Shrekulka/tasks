"""logger_config.py

Централизованная настройка логирования.

Что умеет:
    * цветной вывод в консоль (свой цвет на каждый уровень), цвет включается только в терминале;
    * запись в файл с ежедневной ротацией (опционально);
    * в каждой строке: время, уровень, файл:строка, КЛАСС.метод(), сообщение;
    * имя класса определяется автоматически по `self`/`cls` в месте вызова;
    * дополнительные поля: log.info("...", extra={"order_id": 42}) -> [order_id=42];
    * МАСКИРОВАНИЕ СЕКРЕТОВ: токен Telegram, ключи OpenRouter и Groq, заголовки Bearer заменяются
      заглушками прямо в итоговой строке (и в traceback тоже);
    * необработанные исключения (в том числе внутри asyncio-задач) попадают в лог.

Использование. Один раз, в точке входа (bot.py):

    from logger_config import setup_logging
    setup_logging(level=config.log.level, color=config.log.color)

В любом другом модуле больше ничего настраивать не нужно:

    import logging
    log = logging.getLogger(__name__)
    log.info("Готово: %s", value)          # %-форматирование, а не f-строка
"""

from __future__ import annotations

import logging
import re
import sys
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

from colorama import Back, Fore, Style, just_fix_windows_console

DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
PLAIN_FORMAT = (
    "%(asctime)s | %(levelname)-8s | %(filename)s:%(lineno)d | "
    "%(classname)s.%(funcName)s() | %(message)s"
)

# Библиотеки, которые на уровне INFO/DEBUG слишком болтливы.
DEFAULT_QUIET_LOGGERS: dict[str, int] = {
    "sqlalchemy.engine": logging.WARNING,
    "sentence_transformers": logging.WARNING,
    "urllib3": logging.WARNING,
    "httpx": logging.WARNING,  # huggingface_hub пишет по строке на каждый HEAD-запрос
    "httpcore": logging.WARNING,
    "huggingface_hub": logging.WARNING,
    "filelock": logging.WARNING,
    "asyncio": logging.WARNING,
    "aiohttp.access": logging.WARNING,
}

# ----------------------------------------------------------------------------------------
# Маскирование секретов
# ----------------------------------------------------------------------------------------

# Токен Telegram выглядит как 123456789:AAH... и входит в URL запросов к Telegram
# (https://api.telegram.org/bot<TOKEN>/...), поэтому может всплыть в тексте исключения.
_SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\d{6,12}:[A-Za-z0-9_-]{30,}"), "<telegram-token>"),
    (re.compile(r"sk-or-[A-Za-z0-9_-]{20,}"), "<openrouter-key>"),  # ключи OpenRouter
    (re.compile(r"gsk_[A-Za-z0-9]{20,}"), "<groq-key>"),  # ключи Groq (на случай возврата)
    (re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]{10,}"), r"\1<hidden>"),
)


def redact(text: str) -> str:
    """Заменяет известные виды секретов заглушками."""
    for pattern, replacement in _SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


# ----------------------------------------------------------------------------------------
# Фильтр и форматтеры
# ----------------------------------------------------------------------------------------

# Всё, что есть в LogRecord «из коробки». Остальное считаем данными из extra={...}.
_STANDARD_RECORD_ATTRS = set(logging.makeLogRecord({}).__dict__.keys()) | {
    "message",
    "asctime",
    "classname",
}


def _format_extra(record: logging.LogRecord) -> str:
    extra_items = {
        key: value for key, value in record.__dict__.items() if key not in _STANDARD_RECORD_ATTRS
    }
    if not extra_items:
        return ""
    rendered = ", ".join(f"{key}={value!r}" for key, value in extra_items.items())
    return f" [{rendered}]"


class _ClassNameFilter(logging.Filter):
    """Добавляет record.classname: имя класса, если log(...) вызван из метода.

    Ищем в стеке кадр, у которого файл и строка совпадают с местом вызова логгера, и
    смотрим его локальные `self` или `cls`. Используем sys._getframe: он заметно дешевле,
    чем inspect.stack(), который читает исходники с диска.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        # Фильтр стоит на двух обработчиках (консоль и файл): второй раз не считаем.
        if not hasattr(record, "classname"):
            record.classname = self._find_classname(record.pathname, record.lineno)
        return True

    @staticmethod
    def _find_classname(pathname: str, lineno: int) -> str:
        frame = sys._getframe(0)
        while frame is not None:
            if frame.f_code.co_filename == pathname and frame.f_lineno == lineno:
                calling_self = frame.f_locals.get("self")
                if calling_self is not None:
                    return type(calling_self).__name__
                calling_cls = frame.f_locals.get("cls")
                if isinstance(calling_cls, type):
                    return calling_cls.__name__
                return "-"  # вызов из обычной функции или из модуля
            frame = frame.f_back
        return "-"


class PlainFormatter(logging.Formatter):
    """Однотонная строка без ANSI-цветов (для файла и для консоли без цвета)."""

    def __init__(self) -> None:
        super().__init__(PLAIN_FORMAT, datefmt=DATE_FORMAT)

    def format(self, record: logging.LogRecord) -> str:
        record.exc_text = None  # traceback считается один раз и кэшируется в записи: сбрасываем
        return redact(super().format(record) + _format_extra(record))


class ColoredConsoleFormatter(logging.Formatter):
    """Красит строку в цвет её уровня. Саму запись (record.msg) не меняет: одна и та же
    запись может уйти и в файл, а там цветовых кодов быть не должно."""

    LEVEL_COLORS = {
        logging.DEBUG: Fore.CYAN,
        logging.INFO: Fore.GREEN,
        logging.WARNING: Fore.YELLOW + Style.BRIGHT,
        logging.ERROR: Fore.RED + Style.BRIGHT,
        logging.CRITICAL: Fore.WHITE + Back.RED + Style.BRIGHT,
    }

    class _LevelFormatter(logging.Formatter):
        """Красит traceback в красный независимо от уровня сообщения."""

        def formatException(self, exc_info) -> str:  # noqa: N802 (имя из стандартной библиотеки)
            return f"{Fore.RED}{super().formatException(exc_info)}{Style.RESET_ALL}"

    def __init__(self) -> None:
        super().__init__(datefmt=DATE_FORMAT)
        # Форматтер под каждый уровень собираем один раз, а не на каждую запись.
        self._by_level = {
            level: self._LevelFormatter(self._build_format(color), datefmt=DATE_FORMAT)
            for level, color in self.LEVEL_COLORS.items()
        }

    @staticmethod
    def _build_format(level_color: str) -> str:
        reset = Style.RESET_ALL
        return (
            f"{Fore.MAGENTA}%(asctime)s{reset} | "
            f"{level_color}%(levelname)-8s{reset} | "
            f"{Fore.LIGHTCYAN_EX}%(filename)s:%(lineno)d{reset} | "
            f"{Fore.BLUE}%(classname)s.%(funcName)s(){reset} | "
            f"{level_color}%(message)s{reset}"
        )

    def format(self, record: logging.LogRecord) -> str:
        record.exc_text = None
        formatter = self._by_level.get(record.levelno, self._by_level[logging.DEBUG])
        text = formatter.format(record)
        extra = _format_extra(record)
        if extra:
            text += f"{Fore.LIGHTBLACK_EX}{extra}{Style.RESET_ALL}"
        return redact(text)


# ----------------------------------------------------------------------------------------
# Необработанные исключения
# ----------------------------------------------------------------------------------------


def _install_uncaught_exception_logging(logger: logging.Logger) -> None:
    """Исключение, дошедшее до верха программы, по умолчанию уходит в stderr без нашего
    формата. Перехватываем sys.excepthook и пишем его как critical-запись."""

    def _handle(exc_type, exc_value, exc_traceback) -> None:
        if issubclass(exc_type, KeyboardInterrupt):  # Ctrl+C: штатная остановка
            sys.__excepthook__(exc_type, exc_value, exc_traceback)
            return
        logger.critical(
            "Необработанное исключение: приложение аварийно завершается",
            exc_info=(exc_type, exc_value, exc_traceback),
        )

    sys.excepthook = _handle


def log_asyncio_exception(loop, context: dict) -> None:
    """Обработчик для asyncio: `loop.set_exception_handler(log_asyncio_exception)`.

    sys.excepthook не видит исключения внутри фоновых задач (asyncio.create_task), а этот
    обработчик видит: без него такая ошибка молча потерялась бы.
    """
    logger = logging.getLogger("asyncio")
    exception = context.get("exception")
    message = context.get("message", "Необработанная ошибка в asyncio")
    if exception is not None:
        logger.error(message, exc_info=(type(exception), exception, exception.__traceback__))
    else:
        logger.error(message)


# ----------------------------------------------------------------------------------------
# Главная функция
# ----------------------------------------------------------------------------------------


def setup_logging(
    level: int = logging.INFO,
    console_level: int | None = None,
    file_level: int | None = None,
    color: bool | None = None,
    log_to_file: bool = False,
    log_dir: str | Path = "logs",
    log_filename: str = "app.log",
    backup_count: int = 7,
    overwrite_file: bool = False,
    quiet_loggers: dict[str, int] | None = None,
    capture_unhandled: bool = True,
) -> logging.Logger:
    """Настраивает КОРНЕВОЙ логгер один раз при старте.

    После вызова любой модуль делает `logging.getLogger(__name__)`, и его сообщения сами
    попадают в обработчики, настроенные здесь (так устроено распространение записей).

    Args:
        level: общий уровень, если console_level/file_level не заданы отдельно.
        console_level: свой уровень для консоли.
        file_level: свой уровень для файла (например DEBUG при тихой консоли).
        color: True/False, None = цвет только если вывод идёт в терминал. В `docker compose
            logs` терминала нет, поэтому управляющих кодов там не будет.
        log_to_file: писать ли лог ещё и в файл (в Docker не нужно: логи идут в stdout).
        log_dir, log_filename: куда писать файл (папка создаётся сама).
        backup_count: сколько ротированных файлов хранить (ротация раз в сутки).
        overwrite_file: True = перезаписывать файл при каждом запуске, без ротации.
        quiet_loggers: {"имя_логгера": уровень}, дополняет DEFAULT_QUIET_LOGGERS.
        capture_unhandled: логировать необработанные исключения верхнего уровня.
    """
    console_level = level if console_level is None else console_level
    file_level = level if file_level is None else file_level
    use_color = sys.stdout.isatty() if color is None else color

    root_logger = logging.getLogger()
    # Корневой уровень не строже самого мягкого обработчика: иначе DEBUG для файла
    # отсеялся бы раньше, чем дошёл до него.
    root_logger.setLevel(min(console_level, file_level))

    # Старые обработчики не просто убираем, а закрываем: иначе файловые дескрипторы
    # остаются открытыми при повторных вызовах setup_logging().
    for old_handler in root_logger.handlers[:]:
        root_logger.removeHandler(old_handler)
        old_handler.close()

    classname_filter = _ClassNameFilter()

    if use_color:
        just_fix_windows_console()  # нужно только на Windows, на macOS и Linux ничего не делает
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(console_level)
    console_handler.setFormatter(ColoredConsoleFormatter() if use_color else PlainFormatter())
    console_handler.addFilter(classname_filter)
    root_logger.addHandler(console_handler)

    if log_to_file:
        try:
            log_path = Path(log_dir)
            log_path.mkdir(parents=True, exist_ok=True)
            file_handler: logging.Handler
            if overwrite_file:
                file_handler = logging.FileHandler(
                    log_path / log_filename, mode="w", encoding="utf-8"
                )
            else:
                file_handler = TimedRotatingFileHandler(
                    log_path / log_filename,
                    when="midnight",
                    interval=1,
                    backupCount=backup_count,
                    encoding="utf-8",
                )
            file_handler.setLevel(file_level)
            file_handler.setFormatter(PlainFormatter())
            file_handler.addFilter(classname_filter)
            root_logger.addHandler(file_handler)
        except OSError as exc:
            # Из-за файла лога приложение не роняем: консоль продолжает работать.
            root_logger.warning("Не удалось настроить запись логов в файл: %s", exc)

    for logger_name, quiet_level in {**DEFAULT_QUIET_LOGGERS, **(quiet_loggers or {})}.items():
        logging.getLogger(logger_name).setLevel(quiet_level)

    if capture_unhandled:
        _install_uncaught_exception_logging(root_logger)

    return root_logger


if __name__ == "__main__":
    # Демонстрация: `python logger_config.py`. При обычном импорте не выполняется.
    setup_logging(level=logging.DEBUG, color=True)
    log = logging.getLogger(__name__)

    class Demo:
        def run(self) -> None:
            log.debug("Отладочное сообщение")
            log.info("Заказ создан", extra={"order_id": 42})
            fake_key = (
                "sk-or-v1-" + "x" * 32
            )  # собирается на лету: сканеры секретов не видят литерала
            log.warning("Ключ %s не должен попасть в лог", fake_key)
            try:
                1 / 0  # noqa: B018
            except ZeroDivisionError:
                log.exception("Не удалось посчитать")

    Demo().run()
    log.critical("Критическая ошибка")
