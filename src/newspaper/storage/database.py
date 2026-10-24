"""SQLite engine setup shared by the generator and both readers."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.exc import OperationalError

from ..config.models import NewspaperConfig
from .models import Base

# One full-text index over notes and articles; diacritics removed so "pension" finds "pensión".
FTS_DDL = """
CREATE VIRTUAL TABLE IF NOT EXISTS search USING fts5(
    kind UNINDEXED, ref_id UNINDEXED, title, body,
    tokenize = 'unicode61 remove_diacritics 2'
)
"""
# ponytail: hand-written migration for columns added after the first release; move to a
# migration tool (Alembic) if a third change like this shows up.
ADDED_COLUMNS = {
    "notes": {"interest": "TEXT", "interest_liked": "BOOLEAN", "opened_at": "DATETIME"},
    "editions": {
        "notified_at": "DATETIME",
        "delivered_at": "DATETIME",
        "cached_at": "DATETIME",
        "clicked_at": "DATETIME",
        "opened_at": "DATETIME",
    },
}


def open_db(config: NewspaperConfig) -> Engine:
    db_path = Path(config.storage.database_path).expanduser()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(f"sqlite:///{db_path}")

    @event.listens_for(engine, "connect")
    def _pragmas(dbapi_conn, _record) -> None:
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")  # readers keep working while generate writes
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()

    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(text(FTS_DDL))
        for table, columns in ADDED_COLUMNS.items():
            have = {row[1] for row in conn.exec_driver_sql(f"PRAGMA table_info({table})")}
            for name, sql_type in columns.items():
                if name in have:
                    continue
                try:
                    conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {name} {sql_type}")
                except OperationalError as e:  # fetch and generate can start together at boot
                    if "duplicate column" not in str(e):
                        raise
    return engine
