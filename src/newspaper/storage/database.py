"""Database engine and session management.

Provides both sync and async SQLAlchemy engines for SQLite via aiosqlite.
"""

from __future__ import annotations

from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import AsyncGenerator, Generator

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.orm import Session, sessionmaker

from .models import Base


def _enable_sqlite_fks(dbapi_conn, connection_record) -> None:  # noqa: ANN001
    """Enable foreign key support in SQLite (off by default)."""
    cursor = dbapi_conn.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


def get_engine(db_path: str | Path) -> Engine:
    """Create a synchronous SQLAlchemy engine for the given SQLite database path.

    Args:
        db_path: Path to the SQLite database file. Will be created if it doesn't exist.

    Returns:
        A configured SQLAlchemy Engine.
    """
    db_path = Path(db_path).expanduser().resolve()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(f"sqlite:///{db_path}", echo=False)
    event.listen(engine, "connect", _enable_sqlite_fks)
    return engine


def get_async_engine(db_path: str | Path) -> AsyncEngine:
    """Create an async SQLAlchemy engine using aiosqlite.

    Args:
        db_path: Path to the SQLite database file. Will be created if it doesn't exist.

    Returns:
        A configured async SQLAlchemy Engine.
    """
    db_path = Path(db_path).expanduser().resolve()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}", echo=False)
    return engine


def init_db(engine: Engine) -> None:
    """Create all tables in the database.

    Args:
        engine: The SQLAlchemy engine to use.
    """
    Base.metadata.create_all(engine)


async def async_init_db(engine: AsyncEngine) -> None:
    """Create all tables in the database (async version).

    Args:
        engine: The async SQLAlchemy engine to use.
    """
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


@contextmanager
def get_session(engine: Engine) -> Generator[Session, None, None]:
    """Provide a transactional session scope.

    Usage:
        with get_session(engine) as session:
            session.add(...)

    Args:
        engine: The SQLAlchemy engine to use.

    Yields:
        A SQLAlchemy Session.
    """
    session_factory = sessionmaker(bind=engine)
    session = session_factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@asynccontextmanager
async def get_async_session(engine: AsyncEngine) -> AsyncGenerator[AsyncSession, None]:
    """Provide a transactional async session scope.

    Usage:
        async with get_async_session(engine) as session:
            await session.execute(...)

    Args:
        engine: The async SQLAlchemy engine to use.

    Yields:
        An async SQLAlchemy Session.
    """
    session = AsyncSession(engine, expire_on_commit=False)
    try:
        yield session
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()
