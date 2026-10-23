# forest-advice-bot/scripts/eval_retrieval.py

"""Оценка качества поиска и подбор RELEVANCE_THRESHOLD по контрольным вопросам.

Запуск из корня проекта:
    python -m scripts.eval_retrieval [--archive ПАПКА] [--questions ФАЙЛ.json]
        [--margin 0.05] [--min-recall 1.0]

Используется настоящая модель эмбеддингов из .env (EMBEDDING_MODEL). База данных и LLM не
нужны, поэтому суточная квота OpenRouter не тратится. Отчёт в Markdown можно вставить в README.
"""

import argparse
import asyncio
import sys
from pathlib import Path

from config_data.config import load_rag_config
from services.embeddings import Embedder, SentenceTransformerEmbedder
from services.evaluation import (
    build_index,
    evaluate_threshold,
    format_report,
    load_questions,
    rank_candidates,
    recommend_threshold,
    sweep,
)

DEFAULT_QUESTIONS = Path("tests/eval/questions.json")


async def run_eval(
    *,
    embedder: Embedder,
    rag,
    archive: Path,
    questions_path: Path,
    margin: float | None = None,
    min_recall: float = 1.0,
) -> str:
    """Возвращает отчёт в Markdown. Отдельная функция, чтобы тесты подставляли свой эмбеддер."""
    questions = load_questions(questions_path)
    index, index_report = await build_index(archive, embedder, rag)
    if not index:
        raise SystemExit(f"В папке {archive} нет документов, которые удалось проиндексировать.")

    limit = rag.top_k * rag.candidate_factor
    candidates = [
        rank_candidates(await embedder.embed_query(q.question), index, limit) for q in questions
    ]
    margin = rag.relevance_margin if margin is None else margin
    results = sweep(questions, candidates, top_k=rag.top_k, margin=margin)
    current = evaluate_threshold(
        questions, candidates, threshold=rag.relevance_threshold, top_k=rag.top_k, margin=margin
    )
    return format_report(
        index_report=index_report,
        current=current,
        results=results,
        recommendation=recommend_threshold(results, min_recall=min_recall),
        top_k=rag.top_k,
        margin=margin,
        min_recall=min_recall,
        settings={
            "модель": embedder.model_name,
            "префиксы": f"{rag.embedding_query_prefix!r} / {rag.embedding_passage_prefix!r}",
            "чанкинг": f"{rag.chunk_size} символов, перекрытие {rag.chunk_overlap}",
            "top_k / candidate_factor": f"{rag.top_k} / {rag.candidate_factor}",
            "пачка эмбеддингов": str(rag.embedding_batch_size),
            "порог / margin": f"{rag.relevance_threshold} / {margin}",
            "вопросов": str(len(questions)),
        },
    )


async def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--archive", type=Path, default=None, help="папка с документами")
    parser.add_argument("--questions", type=Path, default=DEFAULT_QUESTIONS)
    parser.add_argument(
        "--margin",
        type=float,
        default=None,
        help="RELEVANCE_MARGIN для пробы (по умолчанию из .env)",
    )
    parser.add_argument(
        "--min-recall",
        type=float,
        default=1.0,
        help="минимальная доля найденных вопросов с ответом",
    )
    args = parser.parse_args(argv)

    rag = load_rag_config()
    archive = args.archive or Path("samples/archive")
    embedder = await asyncio.to_thread(SentenceTransformerEmbedder.load, rag)
    report = await run_eval(
        embedder=embedder,
        rag=rag,
        archive=archive,
        questions_path=args.questions,
        margin=args.margin,
        min_recall=args.min_recall,
    )
    print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(sys.argv[1:])))
