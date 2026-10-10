"""SQLite engine setup shared by the generator and both readers."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import Engine, create_engine, event, text

from ..config.models import NewspaperConfig
from .models import Base

# One full-text index over notes and articles; diacritics removed so "pension" finds "pensión".
FTS_DDL = """
CREATE VIRTUAL TABLE IF NOT EXISTS search USING fts5(
    kind UNINDEXED, ref_id UNINDEXED, title, body,
    tokenize = 'unicode61 remove_diacritics 2'
)
"""


def get_engine(db_path: str | Path) -> Engine:
    db_path = Path(db_path).expanduser()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(f"sqlite:///{db_path}")

    @event.listens_for(engine, "connect")
    def _pragmas(dbapi_conn, _record) -> None:
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")  # readers keep working while generate writes
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()

    return engine


def init_db(engine: Engine) -> None:
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(text(FTS_DDL))


def open_db(config: NewspaperConfig) -> Engine:
    engine = get_engine(config.storage.database_path)
    init_db(engine)
    return engine
