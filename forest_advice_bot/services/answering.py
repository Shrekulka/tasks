# forest-advice-bot/services/answering.py

"""Ответ по документам: промпт, разбор JSON модели, проверка, источники из реестра.

Модель не может выдумать источник: названия, авторов и даты код берёт из БД по номерам
фрагментов, а номера проверяет (вне диапазона отбрасываются).
"""

import html
import json
import logging
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from config_data.config import LLMConfig, RagConfig
from lexicon import lexicon_uk as lexicon
from models.methods import FoundChunk
from models.models import SourceType
from services.answer_cache import AnswerCache
from services.language import LanguageGuard, get_profile
from services.prompts import PromptTexts
from services.retrieval import SupportsRetrieve
from utils.utils import TELEGRAM_MESSAGE_LIMIT

log = logging.getLogger(__name__)

_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)
_PROMPT_TAG_RE = re.compile(r"<(/?)(fragment|question)", re.IGNORECASE)


# ---------------------------------------------------------------------- промпт


@dataclass(frozen=True, slots=True)
class Prompt:
    messages: list[dict[str, str]]
    fragments: list[FoundChunk]  # именно эти фрагменты попали в промпт, номер = позиция + 1


def _neutralize(text: str) -> str:
    """Теги промпта внутри данных ломаем, чтобы документ не мог «закрыть» фрагмент."""
    return _PROMPT_TAG_RE.sub(r"‹\1\2", text)


def build_prompt(
    question: str,
    fragments: Sequence[FoundChunk],
    *,
    max_context_chars: int,
    system_prompt: str,
) -> Prompt:
    """Нумерованные фрагменты в тегах. Лишнее сверх лимита контекста отбрасывается.

    Первый (лучший) фрагмент остаётся всегда: при необходимости он обрезается.
    """
    included: list[FoundChunk] = []
    blocks: list[str] = []
    total = 0
    for chunk in fragments:
        text = _neutralize(chunk.content)
        if not included and len(text) > max_context_chars:
            text = text[:max_context_chars]
        if included and total + len(text) > max_context_chars:
            break
        included.append(chunk)
        blocks.append(f'<fragment id="{len(included)}">\n{text}\n</fragment>')
        total += len(text)

    user = "\n".join(blocks) + f"\n<question>\n{_neutralize(question)}\n</question>"
    return Prompt(
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user},
        ],
        fragments=included,
    )


def strict_messages(messages: Sequence[Mapping[str, str]], reminder: str) -> list[dict[str, str]]:
    """Тот же запрос с напоминанием о формате в конце последней реплики пользователя."""
    result = [dict(message) for message in messages]
    for message in reversed(result):
        if message.get("role") == "user":
            message["content"] += reminder
            break
    return result


# ---------------------------------------------------------------------- разбор ответа модели


@dataclass(frozen=True, slots=True)
class ModelAnswer:
    found: bool
    answer: str
    used: tuple[int, ...]


def _to_answer(obj: Any) -> ModelAnswer | None:
    if not isinstance(obj, Mapping) or not isinstance(obj.get("found"), bool):
        return None
    answer = obj.get("answer", "")
    used = obj.get("used", [])
    if not isinstance(answer, str) or not isinstance(used, list):
        return None
    numbers = tuple(n for n in used if isinstance(n, int) and not isinstance(n, bool))
    return ModelAnswer(found=obj["found"], answer=answer.strip(), used=numbers)


def parse_model_json(text: str) -> ModelAnswer | None:
    """Терпимый разбор: убирает <think>-блоки и ограждения кода, берёт первый годный объект."""
    cleaned = _THINK_RE.sub("", text)
    candidates = [match.group(1) for match in _FENCE_RE.finditer(cleaned)] + [cleaned]
    decoder = json.JSONDecoder()
    for candidate in candidates:
        for start, char in enumerate(candidate):
            if char != "{":
                continue
            try:
                obj, _ = decoder.raw_decode(candidate[start:])
            except ValueError:
                continue
            parsed = _to_answer(obj)
            if parsed is not None:
                return parsed
    return None


def validate_used(parsed: ModelAnswer, fragment_count: int) -> list[int] | None:
    """Номера использованных фрагментов или None, если ответ недостоверен.

    Номера вне диапазона отбрасываются. found=true без ответа или без единого валидного
    номера считается недостоверным: иначе в ответе не оказалось бы источников.
    """
    valid: list[int] = []
    for number in parsed.used:
        if 1 <= number <= fragment_count and number not in valid:
            valid.append(number)
    if parsed.found and (not parsed.answer or not valid):
        return None
    return valid if parsed.found else []


# ---------------------------------------------------------------------- источники и вывод


def format_sources(fragments: Sequence[FoundChunk], timezone: ZoneInfo) -> list[str]:
    """Строки источников из реестра. Одинаковые (документ, страница) показываются один раз."""
    lines: list[str] = []
    seen: set[tuple[int, int | None]] = set()
    for chunk in fragments:
        key = (chunk.document_id, chunk.page)
        if key in seen:
            continue
        seen.add(key)
        title = html.escape(chunk.title, quote=False)
        page = lexicon.SOURCE_PAGE.format(page=chunk.page) if chunk.page else ""
        if chunk.source_type is SourceType.ARCHIVE:
            lines.append(lexicon.SOURCE_ARCHIVE.format(title=title, page=page))
            continue
        author = html.escape(chunk.uploader_name or lexicon.UNKNOWN_AUTHOR, quote=False)
        if chunk.uploader_username:
            username = html.escape(chunk.uploader_username, quote=False)
            author += lexicon.AUTHOR_USERNAME.format(username=username)
        when = chunk.uploaded_at.astimezone(timezone).strftime(lexicon.DATE_FORMAT)
        lines.append(lexicon.SOURCE_USER.format(title=title, page=page, author=author, when=when))
    return lines


def compose_reply(answer: str, sources: Sequence[str]) -> str:
    """Текст ответа (экранирован под parse_mode=HTML) и блок источников."""
    text = html.escape(answer, quote=False)
    if not sources:
        return text
    return f"{text}\n\n{lexicon.SOURCES_HEADER}\n" + "\n".join(sources)


def split_message(text: str, limit: int = TELEGRAM_MESSAGE_LIMIT) -> list[str]:
    """Делит текст на части не длиннее limit: по абзацам, затем по строкам, затем жёстко."""
    if len(text) <= limit:
        return [text]
    parts: list[str] = []
    current = ""
    for line in text.split("\n"):
        while len(line) > limit:  # строка длиннее лимита: режем жёстко
            if current:
                parts.append(current)
                current = ""
            parts.append(line[:limit])
            line = line[limit:]
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) <= limit:
            current = candidate
        else:
            parts.append(current)
            current = line
    if current:
        parts.append(current)
    return parts


# ---------------------------------------------------------------------- сервис


class SupportsAsk(Protocol):
    async def ask(
        self,
        messages: Sequence[Mapping[str, str]],
        *,
        validate: Callable[[str], bool] | None = None,
        options: Mapping[str, Any] | None = None,
        retry_messages: Sequence[Mapping[str, str]] | None = None,
    ) -> Any:
        """Возвращает объект с полями text и model."""


@dataclass(frozen=True, slots=True)
class Answer:
    text: str  # готов к отправке с parse_mode=HTML
    found: bool
    sources: int
    model: str | None = None  # None, если LLM не вызывалась
    from_cache: bool = False


class AnswerService:
    def __init__(
        self,
        retriever: SupportsRetrieve,
        llm: SupportsAsk,
        *,
        rag: RagConfig,
        llm_config: LLMConfig,
        timezone: str,
        cache: AnswerCache[Answer] | None = None,
    ) -> None:
        self._retriever = retriever
        self._llm = llm
        self._max_context_chars = rag.max_context_chars
        self._options = {"temperature": llm_config.temperature, "max_tokens": llm_config.max_tokens}
        self._timezone = ZoneInfo(timezone)
        self._cache = cache
        profile = get_profile(rag.answer_language, rag.language_profiles_dir)
        self._language = LanguageGuard(profile, marker_min_chars=rag.language_marker_min_chars)
        self._prompts = PromptTexts.for_profile(profile, rag.prompts_dir)
        # Всё, от чего зависит ответ, кроме вопроса и версии коллекции (её добавляет кэш).
        self._cache_parts = (
            self._prompts.version,
            rag.answer_language,
            rag.embedding_model,
            rag.top_k,
            rag.relevance_threshold,
            rag.relevance_margin,
            rag.max_context_chars,
            llm_config.temperature,
        )

    async def answer(self, question: str) -> Answer:
        """Ошибки LLM (LLMQuotaExceeded, LLMUnavailable, ...) передаются вызывающему коду."""
        cache_key = (
            self._cache.make_key(question, *self._cache_parts) if self._cache is not None else None
        )
        if self._cache is not None and cache_key is not None:
            cached = self._cache.get(cache_key)
            if cached is not None:
                return replace(cached, from_cache=True)

        answer = await self._answer_uncached(question)
        # Кэшируем только ответы, которые дала и подтвердила LLM. «Нет фрагментов» дёшево
        # пересчитывается, а ошибки сюда не доходят.
        if self._cache is not None and cache_key is not None and answer.model is not None:
            self._cache.put(cache_key, answer)
        return answer

    async def _answer_uncached(self, question: str) -> Answer:
        fragments = await self._retriever.retrieve(question)
        if not fragments:
            return self._no_answer(model=None)

        prompt = build_prompt(
            question,
            fragments,
            max_context_chars=self._max_context_chars,
            system_prompt=self._prompts.system,
        )
        count = len(prompt.fragments)

        def acceptable(text: str) -> bool:
            parsed = parse_model_json(text)
            if parsed is None or validate_used(parsed, count) is None:
                return False
            return not parsed.found or self._language.looks_right(parsed.answer)

        result = await self._llm.ask(
            prompt.messages,
            validate=acceptable,
            options=self._options,
            retry_messages=strict_messages(prompt.messages, self._prompts.strict_reminder),
        )
        parsed = parse_model_json(result.text)
        used = validate_used(parsed, count) if parsed is not None else None
        if parsed is None or used is None or not parsed.found:
            return self._no_answer(model=result.model)

        sources = format_sources([prompt.fragments[n - 1] for n in used], self._timezone)
        return Answer(
            text=compose_reply(parsed.answer, sources),
            found=True,
            sources=len(sources),
            model=result.model,
        )

    @staticmethod
    def _no_answer(*, model: str | None) -> Answer:
        return Answer(text=lexicon.NO_ANSWER, found=False, sources=0, model=model)
