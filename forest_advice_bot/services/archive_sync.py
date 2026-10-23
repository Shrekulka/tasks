# forest-advice-bot/services/archive_sync.py

"""Синхронизация папки со стандартными документами с базой.

Ядро (plan_archive_sync) чистое и тестируется без БД. Остальное применяет план: сканирует
папку, считает SHA-256, добавляет, переименовывает, повышает и удаляет документы.
"""

import asyncio
import hashlib
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from errors.errors import DocumentError, DuplicateDocument
from models.methods import (
    delete_archive_documents,
    load_sync_state,
    promote_to_archive,
    relink_archive_document,
)
from models.models import SourceType
from services.ingestion import DocumentSource, IngestionService
from services.parsers import supported_extensions

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SyncPlan:
    add: dict[str, str]  # sha -> путь: новый файл, индексируем
    promote: dict[str, str]  # sha -> путь: документ зверя становится стандартным (D33)
    relink: dict[str, str]  # sha -> новый путь архивного документа, без переиндексации
    remove: frozenset[str]  # sha архивных документов, которых больше нет на диске
    duplicates: dict[str, list[str]]  # sha -> лишние пути (только лог)


def plan_archive_sync(
    disk: Mapping[str, str],
    archive_known: Mapping[str, str],
    user_known: frozenset[str] = frozenset(),
) -> SyncPlan:
    """Строит план синхронизации архива.

    disk: относительный путь -> SHA-256 файла на диске.
    archive_known: SHA-256 -> путь в БД, только документы source_type=archive.
    user_known: SHA-256 документов зверей (source_type=user).

    Документы зверей в `remove` попасть не могут: туда входят только ключи archive_known.
    """
    paths_by_sha: dict[str, list[str]] = {}
    for path, sha in sorted(disk.items()):
        paths_by_sha.setdefault(sha, []).append(path)

    add: dict[str, str] = {}
    promote: dict[str, str] = {}
    relink: dict[str, str] = {}
    duplicates: dict[str, list[str]] = {}

    for sha, paths in paths_by_sha.items():
        canonical = paths[0]
        if sha in archive_known:
            if archive_known[sha] in paths:
                canonical = archive_known[sha]  # текущий путь жив: ничего не меняем
            else:
                relink[sha] = canonical
        elif sha in user_known:
            promote[sha] = canonical
        else:
            add[sha] = canonical

        extras = [path for path in paths if path != canonical]
        if extras:
            duplicates[sha] = extras

    remove = frozenset(set(archive_known) - set(paths_by_sha))
    return SyncPlan(add, promote, relink, remove, duplicates)


# ---------------------------------------------------------------------- файлы


def scan_archive(directory: Path) -> dict[str, tuple[Path, str]]:
    """Относительный путь (POSIX) -> (полный путь, SHA-256) для поддерживаемых файлов.

    Скрытые файлы и папки (начинаются с точки: .DS_Store и подобные) пропускаются.
    Блокирующая операция: вызывайте через asyncio.to_thread.
    """
    extensions = set(supported_extensions())
    found: dict[str, tuple[Path, str]] = {}
    for path in sorted(directory.rglob("*")):
        relative = path.relative_to(directory)
        if any(part.startswith(".") for part in relative.parts):
            continue
        if not path.is_file() or path.suffix.lower() not in extensions:
            continue
        with path.open("rb") as handle:
            digest = hashlib.file_digest(handle, "sha256").hexdigest()
        found[relative.as_posix()] = (path, digest)
    return found


@dataclass(slots=True)
class SyncReport:
    added: int = 0
    promoted: int = 0
    relinked: int = 0
    removed: int = 0
    skipped_duplicates: int = 0
    failed: int = 0

    def __str__(self) -> str:
        return (
            f"добавлено {self.added}, повышено {self.promoted}, переименовано {self.relinked}, "
            f"удалено {self.removed}, дублей пропущено {self.skipped_duplicates}, "
            f"ошибок {self.failed}"
        )


async def sync_archive(
    session_factory: async_sessionmaker[AsyncSession],
    ingestion: IngestionService,
    directory: Path,
    *,
    on_change: Callable[[], None] | None = None,
) -> SyncReport:
    """Приводит базу в соответствие с папкой. Идемпотентна: повторный запуск ничего не меняет.

    Ошибка одного файла логируется и не останавливает остальные. Если папки нет или она
    пуста, удаление архивных документов НЕ выполняется: пустой каталог чаще означает
    неудачное монтирование, чем намерение стереть весь архив.
    """
    report = SyncReport()
    if not await asyncio.to_thread(directory.is_dir):
        log.warning("Папка архива %s не найдена: синхронизация пропущена", directory)
        return report

    disk_files = await asyncio.to_thread(scan_archive, directory)
    disk = {path: sha for path, (_, sha) in disk_files.items()}

    async with session_factory() as session:
        archive_known, user_known = await load_sync_state(session)

    plan = plan_archive_sync(disk, archive_known, user_known)

    for sha, extras in plan.duplicates.items():
        log.warning("В архиве дубли (%s): лишние файлы %s пропущены", sha[:8], ", ".join(extras))
        report.skipped_duplicates += len(extras)

    remove = plan.remove
    if remove and not disk:
        log.warning("Папка архива пуста: удаление %d документов пропущено", len(remove))
        remove = frozenset()

    async with session_factory() as session:
        report.removed = await delete_archive_documents(session, remove)
        for sha, path in plan.relink.items():
            report.relinked += await relink_archive_document(session, sha, path)
        for sha, path in plan.promote.items():
            report.promoted += await promote_to_archive(session, sha, path)
        await session.commit()

    for relative in plan.add.values():
        full_path, _ = disk_files[relative]
        source = DocumentSource(
            source_type=SourceType.ARCHIVE, filename=full_path.name, source_path=relative
        )
        try:
            data = await asyncio.to_thread(full_path.read_bytes)
            await ingestion.ingest(data, source)
            report.added += 1
        except DuplicateDocument:
            report.skipped_duplicates += 1
            log.warning("Файл архива %s уже есть в коллекции", relative)
        except DocumentError as exc:
            report.failed += 1
            log.warning("Файл архива %s пропущен: %s", relative, exc)
        except Exception:
            report.failed += 1
            log.exception("Файл архива %s не удалось обработать", relative)

    if on_change is not None and (report.added or report.promoted or report.removed):
        on_change()
    log.info("Синхронизация архива: %s", report)
    return report
