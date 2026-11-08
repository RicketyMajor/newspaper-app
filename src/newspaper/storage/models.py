"""ORM models. All datetimes are naive UTC; edition_date is the local date in general.timezone."""

from __future__ import annotations

import enum
from datetime import UTC, date, datetime

from sqlalchemy import Column, Date, Enum, ForeignKey, LargeBinary, String, Table, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def clamped(at: datetime | None, floor: datetime | None = None) -> datetime:
    """A moment a device reported, in UTC (the web's device_time), as naive UTC like utcnow():
    never in the future, never before `floor`. None means now."""
    now = utcnow()
    if at is None:
        return now
    moment = min(at.replace(tzinfo=None), now)
    return max(moment, floor) if floor else moment


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
    # The daily notice (#17): when it went out, when the phone got it, saved its copy (#20a)
    # and tapped it, and the first opening from any device. Opening is not reading.
    notified_at: Mapped[datetime | None]
    delivered_at: Mapped[datetime | None]
    cached_at: Mapped[datetime | None]
    clicked_at: Mapped[datetime | None]
    opened_at: Mapped[datetime | None]

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
    # Its photo on the outlet (#18): the feed's, replaced by the page's og:image. http(s) only.
    image_url: Mapped[str | None] = mapped_column(Text)

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
    opened_at: Mapped[datetime | None]  # first opening; is_read stays for reaching the sources
    # Its photo, kept on this PC (#18): the file in the images folder ("123.jpg") and the outlet
    # it came from. Both go back to None after 60 days.
    image: Mapped[str | None] = mapped_column(Text)
    image_credit: Mapped[str | None] = mapped_column(Text)

    edition: Mapped[Edition] = relationship(back_populates="notes")
    articles: Mapped[list[Article]] = relationship(
        secondary=note_articles, order_by=Article.published_at
    )


class PushSubscription(Base):
    """A browser that asked for the daily notice (Web Push, #17)."""

    __tablename__ = "push_subscriptions"

    id: Mapped[int] = mapped_column(primary_key=True)
    endpoint: Mapped[str] = mapped_column(String(2000), unique=True)
    p256dh: Mapped[str] = mapped_column(String(200))
    auth: Mapped[str] = mapped_column(String(100))
    mobile: Mapped[bool] = mapped_column(default=False)  # only the phone fills the funnel
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    gone_at: Mapped[datetime | None]  # the push service answered 404/410: renew, don't send


class Screening(Base):
    """One showtime of a configured cinema (#29). A cinema's rows are replaced on each good
    read, so the table holds only what each one publishes now."""

    __tablename__ = "screenings"

    id: Mapped[int] = mapped_column(primary_key=True)
    venue: Mapped[str] = mapped_column(String(60), index=True)  # the word in [cine] salas
    day: Mapped[date] = mapped_column(Date)  # local, as the cinema says it
    time: Mapped[str | None] = mapped_column(String(5))  # "HH:MM"; None: the day only
    title: Mapped[str] = mapped_column(String(300))
    note: Mapped[str | None] = mapped_column(String(300))  # presenter, cycle or category
    url: Mapped[str | None] = mapped_column(String(2000))  # tickets, else the event's page


class CinemaVenue(Base):
    """The last read of each cinema's listings (#29): a failure keeps its shows and says so."""

    __tablename__ = "cinema_venues"

    venue: Mapped[str] = mapped_column(String(60), primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    checked_at: Mapped[datetime]  # the last attempt
    ok_at: Mapped[datetime | None]  # the last good read
    error: Mapped[str | None] = mapped_column(Text)  # the last attempt's, None when it worked
