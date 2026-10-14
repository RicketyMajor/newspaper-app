"""ORM models. All datetimes are naive UTC; edition_date is the local date in general.timezone."""

from __future__ import annotations

import enum
from datetime import UTC, date, datetime

from sqlalchemy import Column, Date, Enum, ForeignKey, LargeBinary, String, Table, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class EditionStatus(enum.StrEnum):
    generating = "generating"
    ready = "ready"
    failed = "failed"


class ReadingState:
    """Read / favorite / saved flags, shared by notes and articles."""

    is_read: Mapped[bool] = mapped_column(default=False)
    is_favorite: Mapped[bool] = mapped_column(default=False)
    is_saved: Mapped[bool] = mapped_column(default=False)


note_articles = Table(
    "note_articles",
    Base.metadata,
    Column("note_id", ForeignKey("notes.id", ondelete="CASCADE"), primary_key=True),
    Column("article_id", ForeignKey("articles.id", ondelete="CASCADE"), primary_key=True),
)


class Edition(Base):
    __tablename__ = "editions"

    id: Mapped[int] = mapped_column(primary_key=True)
    edition_date: Mapped[date] = mapped_column(Date, unique=True)
    status: Mapped[EditionStatus] = mapped_column(
        Enum(EditionStatus, native_enum=False), default=EditionStatus.generating
    )
    pid: Mapped[int | None]
    started_at: Mapped[datetime] = mapped_column(default=utcnow)
    finished_at: Mapped[datetime | None]
    error: Mapped[str | None] = mapped_column(Text)
    feed_errors: Mapped[str | None] = mapped_column(Text)

    notes: Mapped[list[Note]] = relationship(
        back_populates="edition", cascade="all, delete-orphan", order_by="Note.rank"
    )


class Article(ReadingState, Base):
    """An original piece fetched from a feed."""

    __tablename__ = "articles"

    id: Mapped[int] = mapped_column(primary_key=True)
    url: Mapped[str] = mapped_column(String(2000), unique=True)
    title: Mapped[str] = mapped_column(Text)
    summary: Mapped[str] = mapped_column(Text, default="")
    content: Mapped[str | None] = mapped_column(Text)
    content_source: Mapped[str | None] = mapped_column(String(10))  # "full" | "excerpt"
    source_name: Mapped[str] = mapped_column(String(200))
    section: Mapped[str] = mapped_column(String(100))
    language: Mapped[str] = mapped_column(String(10), default="es")
    published_at: Mapped[datetime | None]
    fetched_at: Mapped[datetime] = mapped_column(default=utcnow)
    embedding: Mapped[bytes | None] = mapped_column(LargeBinary)

    @property
    def when(self) -> datetime:
        return self.published_at or self.fetched_at


class Note(ReadingState, Base):
    """An integrated note: one event, written from one or more articles."""

    __tablename__ = "notes"

    id: Mapped[int] = mapped_column(primary_key=True)
    edition_id: Mapped[int] = mapped_column(ForeignKey("editions.id", ondelete="CASCADE"))
    section: Mapped[str] = mapped_column(String(100))
    rank: Mapped[int]
    is_front: Mapped[bool] = mapped_column(default=False)
    headline: Mapped[str] = mapped_column(Text)
    dek: Mapped[str] = mapped_column(Text, default="")
    body: Mapped[str] = mapped_column(Text)
    differences: Mapped[str | None] = mapped_column(Text)
    single_source: Mapped[bool] = mapped_column(default=False)
    excerpt_only: Mapped[bool] = mapped_column(default=False)
    drafted: Mapped[bool] = mapped_column(default=True)
    read_minutes: Mapped[int] = mapped_column(default=1)
    interest: Mapped[str | None] = mapped_column(Text)  # the reader's phrase the model picked
    interest_liked: Mapped[bool | None]  # True: "sí", False: "no"

    edition: Mapped[Edition] = relationship(back_populates="notes")
    articles: Mapped[list[Article]] = relationship(
        secondary=note_articles, order_by=Article.published_at
    )
