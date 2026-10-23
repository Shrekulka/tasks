# forest-advice-bot/services/language.py

"""Проверка языка ответа модели. Язык задаётся профилем из JSON-файла, а не зашит в код.

Новый язык подключается файлом в каталоге профилей (LANGUAGE_PROFILES_DIR), код не меняется.
Проверка эвристическая и дешёвая: она не заменяет детектор языка, а отсекает типичные промахи
моделей (русский вместо украинского). Качество измеряется на размеченном корпусе
tests/eval/language_cases.json.
"""

import json
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from config_data.config import ConfigError

DEFAULT_PROFILES_DIR = Path(__file__).resolve().parent.parent / "data" / "languages"
_WORD_RE = re.compile(r"[^\W\d_]+", re.UNICODE)


@dataclass(frozen=True, slots=True)
class LanguageProfile:
    code: str
    scripts: frozenset[str]  # допустимые письменности (первое слово имени символа в Unicode)
    main_script: str
    answer_phrase: str  # как просить язык в промпте, на языке самого промпта («українською мовою»)
    min_script_share: float  # минимальная доля букв основной письменности
    own_letters: frozenset[str]  # буквы, которые есть в языке и обычно есть в тексте
    foreign_letters: frozenset[str]  # буквы близкого языка, которых в этом языке нет
    own_words: frozenset[str]  # служебные слова только этого языка
    foreign_words: frozenset[str]  # служебные слова близкого языка, которых в этом языке нет


def load_profile(path: Path) -> LanguageProfile:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return LanguageProfile(
            code=str(raw["code"]),
            scripts=frozenset(raw["scripts"]),
            main_script=str(raw["main_script"]),
            answer_phrase=str(raw["answer_phrase"]),
            min_script_share=float(raw["min_script_share"]),
            own_letters=frozenset(raw.get("own_letters", "")),
            foreign_letters=frozenset(raw.get("foreign_letters", "")),
            own_words=frozenset(w.casefold() for w in raw.get("own_words", [])),
            foreign_words=frozenset(w.casefold() for w in raw.get("foreign_words", [])),
        )
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ConfigError(
            {"ANSWER_LANGUAGE": [f"профиль языка {path} не прочитан: {exc}"]}
        ) from exc


def supported_languages(directory: Path = DEFAULT_PROFILES_DIR) -> tuple[str, ...]:
    return tuple(sorted(p.stem for p in directory.glob("*.json")))


def get_profile(code: str, directory: Path = DEFAULT_PROFILES_DIR) -> LanguageProfile:
    path = directory / f"{code}.json"
    if not path.is_file():
        available = ", ".join(supported_languages(directory)) or "нет файлов"
        raise ConfigError(
            {
                "ANSWER_LANGUAGE": [
                    f"нет профиля языка «{code}» в {directory}, доступны: {available}"
                ]
            }
        )
    return load_profile(path)


def _script(char: str) -> str:
    return unicodedata.name(char, "UNKNOWN").split()[0]


class LanguageGuard:
    """Отвечает на вопрос «похож ли текст на ожидаемый язык»."""

    def __init__(self, profile: LanguageProfile, *, marker_min_chars: int) -> None:
        self._profile = profile
        self._marker_min_chars = marker_min_chars

    def looks_right(self, text: str) -> bool:
        profile = self._profile
        letters = [c for c in text if c.isalpha()]
        if not letters:
            return False
        scripts = [_script(c) for c in letters]
        if any(script not in profile.scripts for script in scripts):
            return False
        if scripts.count(profile.main_script) / len(letters) < profile.min_script_share:
            return False

        own = sum(1 for c in letters if c in profile.own_letters)
        foreign = sum(1 for c in letters if c in profile.foreign_letters)
        if foreign > own:
            return False
        words = {w.casefold() for w in _WORD_RE.findall(text)}
        if words & profile.foreign_words:
            return False
        # В достаточно длинном тексте встречаются свои буквы или свои служебные слова.
        has_own_word = bool(words & profile.own_words)
        return not (len(letters) >= self._marker_min_chars and own == 0 and not has_own_word)
