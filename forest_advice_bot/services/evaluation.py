# forest-advice-bot/services/evaluation.py

"""Оценка качества поиска и подбор порога релевантности. Без БД и без вызовов LLM.

Папка с документами индексируется в памяти теми же парсером, чанкером и эмбеддером, что и
в боте. Для каждого контрольного вопроса считаются расстояния до всех фрагментов, а затем
перебираются пороги: для каждого видно, сколько вопросов с ответом бот нашёл бы и сколько
вопросов без ответа он бы честно отклонил.
"""

import asyncio
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from config_data.config import RagConfig
from errors.errors import DocumentError
from models.methods import FoundChunk
from models.models import SourceType
from services.archive_sync import scan_archive
from services.chunker import chunk_segments
from services.embeddings import Embedder
from services.ingestion import title_from
from services.parsers import ParseLimits, extract_segments
from services.retrieval import filter_relevant, select_diverse

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_MATCH_MODES = frozenset({"any", "all"})


@dataclass(frozen=True, slots=True)
class EvalQuestion:
    question: str
    expected: tuple[str, ...]  # названия документов (имя файла без расширения); пусто: ответа нет
    match: str = "any"  # any: достаточно одного из expected, all: нужны все
    note: str = ""
    kind: str = ""  # answerable, near_miss (рядом по теме, ответа нет), off_topic, ambiguous

    @property
    def answerable(self) -> bool:
        return bool(self.expected)

    @property
    def group(self) -> str:
        """Группа для отчёта: явная kind либо answerable/off_topic по наличию expected."""
        return self.kind or ("answerable" if self.expected else "off_topic")


@dataclass(frozen=True, slots=True)
class IndexedChunk:
    title: str
    text: str
    vector: list[float]


@dataclass(slots=True)
class IndexReport:
    documents: int = 0
    chunks: int = 0
    skipped: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class QuestionOutcome:
    question: EvalQuestion
    best_distance: float | None
    docs: tuple[tuple[str, float], ...]  # (документ, лучшее расстояние) в итоговой выдаче
    ok: bool

    @property
    def titles(self) -> tuple[str, ...]:
        return tuple(title for title, _ in self.docs)


@dataclass(frozen=True, slots=True)
class ThresholdResult:
    threshold: float
    hit_rate: float | None  # доля вопросов с ответом, на которые нашлись нужные документы
    reject_rate: float | None  # доля вопросов без ответа, которые бот отклонил бы
    outcomes: tuple[QuestionOutcome, ...]

    @property
    def llm_requests(self) -> int:
        """Сколько вопросов прошли бы порог и потратили бы запрос к LLM (из квоты в сутки)."""
        return sum(1 for o in self.outcomes if o.docs)

    @property
    def balanced(self) -> float:
        rates = [r for r in (self.hit_rate, self.reject_rate) if r is not None]
        return sum(rates) / len(rates) if rates else 0.0


@dataclass(frozen=True, slots=True)
class Recommendation:
    value: float  # рекомендуемый порог: середина устойчивого диапазона
    result: ThresholdResult  # результаты при ближайшем к нему проверенном пороге
    low: float  # нижняя граница диапазона с тем же качеством
    high: float  # верхняя граница


# ---------------------------------------------------------------------- данные


def load_questions(path: Path) -> list[EvalQuestion]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list) or not raw:
        raise ValueError("файл вопросов должен содержать непустой список")
    questions: list[EvalQuestion] = []
    for index, item in enumerate(raw, start=1):
        match = item.get("match", "any")
        expected = item.get("expected")
        if not isinstance(item.get("question"), str) or not isinstance(expected, list):
            raise ValueError(f"вопрос №{index}: нужны поля question (строка) и expected (список)")
        if match not in _MATCH_MODES:
            raise ValueError(f"вопрос №{index}: match должен быть any или all")
        questions.append(
            EvalQuestion(
                item["question"], tuple(expected), match, item.get("note", ""), item.get("kind", "")
            )
        )
    return questions


async def build_index(
    directory: Path, embedder: Embedder, rag: RagConfig
) -> tuple[list[IndexedChunk], IndexReport]:
    """Индексирует папку в памяти. Дубли по SHA-256 и нечитаемые файлы пропускаются."""
    limits = ParseLimits.from_config(rag)
    files = await asyncio.to_thread(scan_archive, directory)
    report = IndexReport()
    index: list[IndexedChunk] = []
    seen: set[str] = set()

    for relative, (path, sha) in files.items():
        if sha in seen:
            report.skipped.append(f"{relative}: дубль")
            continue
        seen.add(sha)
        try:
            data = await asyncio.to_thread(path.read_bytes)
            _, segments = await asyncio.to_thread(extract_segments, path.name, data, limits)
        except DocumentError as error:
            report.skipped.append(f"{relative}: {type(error).__name__}")
            continue
        chunks = chunk_segments(segments, size=rag.chunk_size, overlap=rag.chunk_overlap)
        vectors = await embedder.embed_passages([c.text for c in chunks])
        title = title_from(path.name, sha)
        index.extend(
            IndexedChunk(title, chunk.text, vector)
            for chunk, vector in zip(chunks, vectors, strict=True)
        )
        report.documents += 1
        report.chunks += len(chunks)
    return index, report


# ---------------------------------------------------------------------- расчёт


def rank_candidates(
    query_vector: Sequence[float], index: Sequence[IndexedChunk], limit: int
) -> list[FoundChunk]:
    """Ближайшие фрагменты по косинусному расстоянию (векторы нормализованы)."""
    titles = {title: number for number, title in enumerate(dict.fromkeys(c.title for c in index))}
    scored = []
    for number, chunk in enumerate(index):
        dot = sum(a * b for a, b in zip(query_vector, chunk.vector, strict=True))
        distance = max(1.0 - dot, 0.0)
        scored.append(
            FoundChunk(
                chunk_id=number,
                document_id=titles[chunk.title],
                content=chunk.text,
                page=None,
                section=None,
                distance=distance,
                title=chunk.title,
                source_type=SourceType.ARCHIVE,
                uploader_name=None,
                uploader_username=None,
                uploaded_at=_EPOCH,
            )
        )
    scored.sort(key=lambda c: (c.distance, c.chunk_id))
    return scored[:limit]


def evaluate_threshold(
    questions: Sequence[EvalQuestion],
    candidates: Sequence[Sequence[FoundChunk]],
    *,
    threshold: float,
    top_k: int,
    margin: float = 0.0,
) -> ThresholdResult:
    outcomes: list[QuestionOutcome] = []
    for question, found in zip(questions, candidates, strict=True):
        relevant = filter_relevant(found, threshold=threshold, margin=margin)
        selected = select_diverse(relevant, top_k)
        docs: dict[str, float] = {}
        for chunk in selected:  # selected отсортирован по расстоянию: первое вхождение лучшее
            docs.setdefault(chunk.title, chunk.distance)
        titles = tuple(docs)
        if not question.answerable:
            ok = not selected
        elif question.match == "all":
            ok = all(title in titles for title in question.expected)
        else:
            ok = any(title in titles for title in question.expected)
        best = found[0].distance if found else None
        outcomes.append(QuestionOutcome(question, best, tuple(docs.items()), ok))

    def rate(answerable: bool) -> float | None:
        group = [o.ok for o in outcomes if o.question.answerable is answerable]
        return sum(group) / len(group) if group else None

    return ThresholdResult(threshold, rate(True), rate(False), tuple(outcomes))


def sweep(
    questions: Sequence[EvalQuestion],
    candidates: Sequence[Sequence[FoundChunk]],
    *,
    top_k: int,
    margin: float = 0.0,
) -> list[ThresholdResult]:
    """Результат для каждого значимого порога: им служат сами расстояния кандидатов."""
    distances = sorted({round(c.distance, 4) for found in candidates for c in found})
    return [
        evaluate_threshold(questions, candidates, threshold=value, top_k=top_k, margin=margin)
        for value in distances
    ]


def recommend_threshold(
    results: Sequence[ThresholdResult], *, min_recall: float = 1.0
) -> Recommendation | None:
    """Порог, который не теряет вопросы с ответом и отклоняет как можно больше лишнего.

    Политика «полнота прежде всего»: не ответить на законный вопрос хуже, чем лишний раз
    спросить LLM, которая вернёт found=false (вторая линия защиты). Поэтому сначала
    отбираются пороги с полнотой не ниже min_recall, среди них берётся лучшее отклонение
    лишнего, и рекомендуется СЕРЕДИНА диапазона с таким качеством. Середина устойчивее
    острого максимума: небольшой сдвиг расстояний на новых документах не ломает результат.
    Если полноты min_recall не достичь, берутся пороги с наибольшей достижимой полнотой.
    """

    def recall(result: ThresholdResult) -> float:
        return 1.0 if result.hit_rate is None else result.hit_rate

    def reject(result: ThresholdResult) -> float | None:
        return result.reject_rate

    if not results:
        return None
    candidates = [r for r in results if recall(r) >= min_recall - 1e-9]
    if not candidates:
        best_recall = max(recall(r) for r in results)
        candidates = [r for r in results if math.isclose(recall(r), best_recall)]

    rejects = [reject(r) for r in candidates if reject(r) is not None]
    best_reject = max(rejects) if rejects else None
    if not best_reject:  # вопросов без ответа нет или ни один порог их не отклоняет
        first = candidates[0]
        return Recommendation(first.threshold, first, first.threshold, first.threshold)

    plateau = [r for r in candidates if math.isclose(reject(r) or 0.0, best_reject)]
    low, high = plateau[0].threshold, plateau[-1].threshold
    middle = round((low + high) / 2, 3)
    nearest = min(plateau, key=lambda r: abs(r.threshold - middle))
    return Recommendation(middle, nearest, low, high)


# ---------------------------------------------------------------------- отчёт


def _percent(value: float | None) -> str:
    return "—" if value is None else f"{value * 100:.0f}%"


def format_report(
    *,
    index_report: IndexReport,
    current: ThresholdResult,
    results: Sequence[ThresholdResult],
    recommendation: Recommendation | None,
    top_k: int,
    margin: float = 0.0,
    min_recall: float = 1.0,
    max_rows: int = 15,
    settings: Mapping[str, str] | None = None,
) -> str:
    """Отчёт в Markdown: его можно вставить в README.

    settings: параметры прогона (модель, префиксы, чанкинг, top_k). Без них результаты до и
    после правок нельзя сравнивать, поэтому они печатаются в шапке отчёта.
    """
    lines = []
    if settings:
        lines += ["### Параметры прогона", ""] + [
            f"- {key}: `{value}`" for key, value in settings.items()
        ]
        lines.append("")
    lines += [
        f"Проиндексировано документов: {index_report.documents}, "
        f"фрагментов: {index_report.chunks}.",
    ]
    lines += [f"Пропущено: {item}" for item in index_report.skipped]
    lines += [
        "",
        f"### Вопросы при пороге {current.threshold} (margin={margin})",
        "",
        "| Вопрос | Ожидается | Лучшее расстояние | Найдено | Итог |",
        "|---|---|---|---|---|",
    ]
    for outcome in current.outcomes:
        expected = ", ".join(outcome.question.expected) or "нет ответа"
        best = "—" if outcome.best_distance is None else f"{outcome.best_distance:.3f}"
        found = ", ".join(f"{title} {distance:.3f}" for title, distance in outcome.docs) or "—"
        mark = "✅" if outcome.ok else "❌"
        lines.append(f"| {outcome.question.question} | {expected} | {best} | {found} | {mark} |")

    groups: dict[str, list[QuestionOutcome]] = {}
    for outcome in current.outcomes:
        groups.setdefault(outcome.question.group, []).append(outcome)
    lines += [
        "",
        "### Метрики по группам вопросов",
        "",
        "| Группа | Вопросов | Верно | Уйдёт в LLM |",
        "|---|---|---|---|",
    ]
    for name, items in sorted(groups.items()):
        ok = sum(o.ok for o in items)
        llm = sum(1 for o in items if o.docs)
        lines.append(f"| {name} | {len(items)} | {_percent(ok / len(items))} | {llm} |")
    false_accept = None if current.reject_rate is None else 1 - current.reject_rate
    false_reject = None if current.hit_rate is None else 1 - current.hit_rate
    lines += [
        "",
        f"Hit@{top_k} (вопросы с ответом): {_percent(current.hit_rate)}. "
        f"Ложные допуски (вопрос без ответа дошёл до LLM): {_percent(false_accept)}. "
        f"Ложные отказы (ответ есть, бот сказал «не знайшов»): {_percent(false_reject)}. "
        f"Запросов к LLM на {len(current.outcomes)} вопросов: {current.llm_requests}.",
        "",
        "### Подбор порога",
        "",
        "| Порог | Нашлось нужное | Отклонено лишнее | Баланс |",
        "|---|---|---|---|",
    ]
    step = max(len(results) // max_rows, 1)
    shown = list(results[::step])
    if recommendation is not None and recommendation.result not in shown:
        shown.append(recommendation.result)
    for result in sorted(shown, key=lambda r: r.threshold):
        lines.append(
            f"| {result.threshold:.3f} | {_percent(result.hit_rate)} | "
            f"{_percent(result.reject_rate)} | {result.balanced * 100:.0f}% |"
        )

    lines.append("")
    if recommendation is None:
        lines.append("Порог подобрать не удалось: нет данных.")
        return "\n".join(lines)

    result = recommendation.result
    lines.append(
        f"**Рекомендация:** `RELEVANCE_THRESHOLD={recommendation.value}` "
        f"(нашлось нужное {_percent(result.hit_rate)}, отклонено лишнее "
        f"{_percent(result.reject_rate)})."
    )
    lines.append(
        f"Политика: полнота не ниже {_percent(min_recall)}, затем максимум отклонения. "
        f"Такое качество держится в диапазоне порогов {recommendation.low:.3f}…"
        f"{recommendation.high:.3f}: значение выбрано в его середине."
    )
    if result.reject_rate is not None and result.reject_rate < 1.0:
        lines.append(
            "Часть вопросов без ответа проходит порог и уйдёт в LLM: она должна вернуть "
            "found=false. Проверьте такие вопросы вручную (✗ в таблице)."
        )
    return "\n".join(lines)
