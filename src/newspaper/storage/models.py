"""Database ORM models for Newspaper.

Uses SQLAlchemy 2.0 declarative style with mapped_column.
"""

from __future__ import annotations

import enum
from datetime import date, datetime

from sqlalchemy import (
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    mapped_column,
    relationship,
)


class Base(DeclarativeBase):
    """Base class for all ORM models."""

    pass


class EditionStatus(enum.Enum):
    """Status of an edition in the generation pipeline."""

    generating = "generating"
    ready = "ready"
    archived = "archived"


class Section(Base):
    """A thematic section defined by the user (e.g. Tech, World, Science)."""

    __tablename__ = "sections"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    slug: Mapped[str] = mapped_column(String(100), unique=True)
    display_order: Mapped[int] = mapped_column(default=0)

    # Relationships
    feeds: Mapped[list[Feed]] = relationship(back_populates="section")
    clusters: Mapped[list[Cluster]] = relationship(back_populates="section")

    def __repr__(self) -> str:
        return f"<Section(name={self.name!r})>"


class Feed(Base):
    """A configured news source (RSS feed, API endpoint, scraper target)."""

    __tablename__ = "feeds"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    url: Mapped[str] = mapped_column(String(500))
    type: Mapped[str] = mapped_column(String(20))  # rss|hackernews|scraper|newsapi|github
    section_id: Mapped[int] = mapped_column(ForeignKey("sections.id"))
    language: Mapped[str] = mapped_column(String(5), default="en")
    last_fetched: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    active: Mapped[bool] = mapped_column(default=True)

    # Relationships
    section: Mapped[Section] = relationship(back_populates="feeds")
    articles: Mapped[list[Article]] = relationship(back_populates="feed")

    def __repr__(self) -> str:
        return f"<Feed(name={self.name!r}, type={self.type!r})>"


class Edition(Base):
    """A complete newspaper edition generated for a specific date."""

    __tablename__ = "editions"

    id: Mapped[int] = mapped_column(primary_key=True)
    edition_date: Mapped[date] = mapped_column(Date, unique=True)
    ai_front_headline: Mapped[str | None] = mapped_column(Text, default=None)
    generated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    status: Mapped[EditionStatus] = mapped_column(
        Enum(EditionStatus), default=EditionStatus.generating
    )

    # Relationships
    clusters: Mapped[list[Cluster]] = relationship(back_populates="edition")

    __table_args__ = (Index("idx_edition_date", "edition_date"),)

    def __repr__(self) -> str:
        return f"<Edition(date={self.edition_date}, status={self.status.value})>"


class Cluster(Base):
    """A group of articles about the same event, identified by HDBSCAN."""

    __tablename__ = "clusters"

    id: Mapped[int] = mapped_column(primary_key=True)
    ai_headline: Mapped[str | None] = mapped_column(Text, default=None)
    ai_summary: Mapped[str | None] = mapped_column(Text, default=None)
    ai_bullets: Mapped[str | None] = mapped_column(Text, default=None)  # JSON array
    edition_id: Mapped[int] = mapped_column(ForeignKey("editions.id"))
    section_id: Mapped[int] = mapped_column(ForeignKey("sections.id"))
    representative_article_id: Mapped[int | None] = mapped_column(
        ForeignKey("articles.id", use_alter=True), default=None
    )

    # Relationships
    edition: Mapped[Edition] = relationship(back_populates="clusters")
    section: Mapped[Section] = relationship(back_populates="clusters")
    articles: Mapped[list[Article]] = relationship(
        back_populates="cluster", foreign_keys="Article.cluster_id"
    )
    representative_article: Mapped[Article | None] = relationship(
        foreign_keys=[representative_article_id], post_update=True
    )

    def __repr__(self) -> str:
        return f"<Cluster(id={self.id}, headline={self.ai_headline!r})>"


class Article(Base):
    """An individual piece of content fetched from a feed."""

    __tablename__ = "articles"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(500))
    summary: Mapped[str | None] = mapped_column(Text, default=None)
    url: Mapped[str] = mapped_column(String(1000))
    content_hash: Mapped[str] = mapped_column(String(64))  # SHA-256
    embedding: Mapped[bytes | None] = mapped_column(LargeBinary, default=None)
    feed_id: Mapped[int] = mapped_column(ForeignKey("feeds.id"))
    cluster_id: Mapped[int | None] = mapped_column(
        ForeignKey("clusters.id"), default=None
    )
    published_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    fetched_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    # Relationships
    feed: Mapped[Feed] = relationship(back_populates="articles")
    cluster: Mapped[Cluster | None] = relationship(
        back_populates="articles", foreign_keys=[cluster_id]
    )
    user_state: Mapped[UserState | None] = relationship(
        back_populates="article", uselist=False
    )

    __table_args__ = (
        Index("idx_content_hash", "content_hash"),
        Index("idx_published_at", "published_at"),
    )

    def __repr__(self) -> str:
        return f"<Article(title={self.title!r})>"


class UserState(Base):
    """User interaction state for an article (read, favorite, saved)."""

    __tablename__ = "user_states"

    id: Mapped[int] = mapped_column(primary_key=True)
    article_id: Mapped[int] = mapped_column(ForeignKey("articles.id"), unique=True)
    is_read: Mapped[bool] = mapped_column(default=False)
    is_favorite: Mapped[bool] = mapped_column(default=False)
    is_saved: Mapped[bool] = mapped_column(default=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    # Relationships
    article: Mapped[Article] = relationship(back_populates="user_state")

    def __repr__(self) -> str:
        return f"<UserState(article_id={self.article_id}, read={self.is_read})>"
