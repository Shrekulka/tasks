# forest-advice-bot/services/prompts.py

"""Тексты промптов из файлов (PROMPTS_DIR), а не из кода.

Порядок поиска: <каталог>/<код языка>/<имя>.txt, затем <каталог>/<имя>.txt. В тексте один
плейсхолдер $answer_phrase (поле answer_phrase профиля языка). Версия промпта это хэш итогового
текста: правка файла сама сбрасывает кэш ответов.
"""

import hashlib
from dataclasses import dataclass
from pathlib import Path
from string import Template

from config_data.config import ConfigError
from services.language import LanguageProfile

DEFAULT_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "data" / "prompts"
SYSTEM_NAME = "system"
STRICT_NAME = "strict_reminder"


def prompt_version(prompt: str) -> str:
    """Версия промпта = хэш его текста."""
    return hashlib.sha256(prompt.encode()).hexdigest()[:8]


def _read(directory: Path, language: str, name: str) -> str:
    for path in (directory / language / f"{name}.txt", directory / f"{name}.txt"):
        if path.is_file():
            try:
                return path.read_text(encoding="utf-8")
            except OSError as exc:
                raise ConfigError({"PROMPTS_DIR": [f"файл {path} не прочитан: {exc}"]}) from exc
    raise ConfigError(
        {"PROMPTS_DIR": [f"нет файла {name}.txt ни в {directory / language}, ни в {directory}"]}
    )


@dataclass(frozen=True, slots=True)
class PromptTexts:
    """Готовые тексты промптов для одного языка ответа."""

    system: str
    strict_reminder: str

    @classmethod
    def for_profile(
        cls, profile: LanguageProfile, directory: Path = DEFAULT_PROMPTS_DIR
    ) -> "PromptTexts":
        values = {"answer_phrase": profile.answer_phrase}

        def render(name: str) -> str:
            template = _read(directory, profile.code, name)
            try:
                return Template(template).substitute(values)
            except (KeyError, ValueError) as exc:
                raise ConfigError(
                    {"PROMPTS_DIR": [f"{name}.txt: неверный плейсхолдер ({exc!r})"]}
                ) from exc

        # Файл читается как есть, но лишний перевод строки в конце system не нужен модели.
        return cls(system=render(SYSTEM_NAME).strip(), strict_reminder=render(STRICT_NAME).rstrip())

    @property
    def version(self) -> str:
        return prompt_version(self.system + self.strict_reminder)
