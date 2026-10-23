# forest-advice-bot/models/models.py

"""ORM-модели. Размерность вектора НЕ зашита: её проверяет код при старте (см. database.py)."""

import enum
from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import BigInteger, DateTime, Enum, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from database.database import Base


class SourceType(enum.StrEnum):
    ARCHIVE = "archive"
    USER = "user"


class DocStatus(enum.StrEnum):
    PROCESSING = "processing"
    READY = "ready"
    FAILED = "failed"


def _enum(cls: type[enum.StrEnum]) -> Enum:
    return Enum(
        cls,
        native_enum=False,
        length=16,
        values_callable=lambda members: [m.value for m in members],
    )


class Document(Base):
    __tablename__ = "documents"
    __table_args__ = (Index("ix_documents_source", "source_type", "source_path"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    sha256: Mapped[str] = mapped_column(String(64), unique=True)
    title: Mapped[str] = mapped_column(Text)
    filename: Mapped[str] = mapped_column(Text)
    source_type: Mapped[SourceType] = mapped_column(_enum(SourceType))
    source_path: Mapped[str | None] = mapped_column(Text, default=None)
    uploader_tg_id: Mapped[int | None] = mapped_column(BigInteger, default=None, index=True)
    uploader_name: Mapped[str | None] = mapped_column(Text, default=None)
    uploader_username: Mapped[str | None] = mapped_column(Text, default=None)
    uploaded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    format: Mapped[str] = mapped_column(String(8))
    chunk_count: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[DocStatus] = mapped_column(_enum(DocStatus), default=DocStatus.PROCESSING)

    chunks: Mapped[list["Chunk"]] = relationship(
        back_populates="document",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class Chunk(Base):
    __tablename__ = "chunks"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    document_id: Mapped[int] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), index=True
    )
    chunk_index: Mapped[int] = mapped_column(Integer)
    page: Mapped[int | None] = mapped_column(Integer, default=None)
    section: Mapped[str | None] = mapped_column(Text, default=None)
    content: Mapped[str] = mapped_column(Text)
    embedding: Mapped[list[float]] = mapped_column(Vector())  # без размерности

    document: Mapped[Document] = relationship(back_populates="chunks")


class AppMeta(Base):
    """Служебные значения приложения: модель эмбеддингов и её размерность."""

    __tablename__ = "app_meta"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text)
