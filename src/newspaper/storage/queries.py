"""Common database queries for the Newspaper application.

All query functions take an AsyncSession and return model instances or sequences.
"""

from __future__ import annotations

from datetime import date

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from .models import Article, Cluster, Edition, EditionStatus, Section, UserState


async def get_latest_edition(session: AsyncSession) -> Edition | None:
    """Get the most recent ready edition.

    Args:
        session: An async database session.

    Returns:
        The latest Edition with status 'ready', or None if no editions exist.
    """
    stmt = (
        select(Edition)
        .where(Edition.status == EditionStatus.ready)
        .order_by(Edition.edition_date.desc())
        .limit(1)
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def get_edition_by_date(session: AsyncSession, target_date: date) -> Edition | None:
    """Get an edition for a specific date.

    Args:
        session: An async database session.
        target_date: The date to look up.

    Returns:
        The Edition for that date, or None.
    """
    stmt = select(Edition).where(Edition.edition_date == target_date)
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def get_sections(session: AsyncSession) -> list[Section]:
    """Get all sections ordered by display_order.

    Args:
        session: An async database session.

    Returns:
        List of all sections.
    """
    stmt = select(Section).order_by(Section.display_order)
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def get_clusters_by_section(
    session: AsyncSession,
    section_id: int,
    edition_id: int,
) -> list[Cluster]:
    """Get all clusters in a section for a specific edition.

    Args:
        session: An async database session.
        section_id: The section to filter by.
        edition_id: The edition to filter by.

    Returns:
        List of clusters with their articles eagerly loaded.
    """
    stmt = (
        select(Cluster)
        .where(Cluster.section_id == section_id, Cluster.edition_id == edition_id)
        .options(selectinload(Cluster.articles))
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def get_articles_by_section(
    session: AsyncSession,
    section_id: int,
    edition_id: int,
) -> list[Article]:
    """Get all articles in a section for a given edition, via their clusters.

    Args:
        session: An async database session.
        section_id: The section to filter by.
        edition_id: The edition to filter by.

    Returns:
        List of articles with user state eagerly loaded.
    """
    stmt = (
        select(Article)
        .join(Cluster, Article.cluster_id == Cluster.id)
        .where(Cluster.section_id == section_id, Cluster.edition_id == edition_id)
        .options(selectinload(Article.user_state))
        .order_by(Article.published_at.desc())
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def get_article_by_id(session: AsyncSession, article_id: int) -> Article | None:
    """Get a single article by its ID.

    Args:
        session: An async database session.
        article_id: The article's primary key.

    Returns:
        The Article, or None.
    """
    stmt = (
        select(Article)
        .where(Article.id == article_id)
        .options(
            selectinload(Article.user_state),
            selectinload(Article.feed),
            selectinload(Article.cluster),
        )
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def search_articles(session: AsyncSession, query: str) -> list[Article]:
    """Search articles by title or summary.

    This is a basic LIKE search. Will be upgraded to FTS5 once the
    virtual table is set up.

    Args:
        session: An async database session.
        query: The search string.

    Returns:
        Matching articles ordered by relevance (published_at desc for now).
    """
    pattern = f"%{query}%"
    stmt = (
        select(Article)
        .where(Article.title.ilike(pattern) | Article.summary.ilike(pattern))
        .options(selectinload(Article.user_state))
        .order_by(Article.published_at.desc())
        .limit(50)
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def update_user_state(
    session: AsyncSession,
    article_id: int,
    *,
    is_read: bool | None = None,
    is_favorite: bool | None = None,
    is_saved: bool | None = None,
) -> UserState:
    """Create or update the user interaction state for an article.

    Args:
        session: An async database session.
        article_id: The article to update state for.
        is_read: Set read status (None = don't change).
        is_favorite: Set favorite status (None = don't change).
        is_saved: Set saved status (None = don't change).

    Returns:
        The updated or created UserState.
    """
    stmt = select(UserState).where(UserState.article_id == article_id)
    result = await session.execute(stmt)
    state = result.scalar_one_or_none()

    if state is None:
        state = UserState(
            article_id=article_id,
            is_read=is_read if is_read is not None else False,
            is_favorite=is_favorite if is_favorite is not None else False,
            is_saved=is_saved if is_saved is not None else False,
        )
        session.add(state)
    else:
        if is_read is not None:
            state.is_read = is_read
        if is_favorite is not None:
            state.is_favorite = is_favorite
        if is_saved is not None:
            state.is_saved = is_saved

    return state


async def get_user_favorites(session: AsyncSession) -> list[Article]:
    """Get all articles marked as favorite.

    Args:
        session: An async database session.

    Returns:
        List of favorite articles.
    """
    stmt = (
        select(Article)
        .join(UserState, Article.id == UserState.article_id)
        .where(UserState.is_favorite.is_(True))
        .options(selectinload(Article.user_state), selectinload(Article.feed))
        .order_by(Article.published_at.desc())
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def get_saved_articles(session: AsyncSession) -> list[Article]:
    """Get all articles saved for later.

    Args:
        session: An async database session.

    Returns:
        List of saved articles.
    """
    stmt = (
        select(Article)
        .join(UserState, Article.id == UserState.article_id)
        .where(UserState.is_saved.is_(True))
        .options(selectinload(Article.user_state), selectinload(Article.feed))
        .order_by(Article.published_at.desc())
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())
