import sqlite3
from datetime import date

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from newspaper.storage import database, queries
from newspaper.storage.database import open_db
from newspaper.storage.models import Edition, EditionStatus, Note, note_articles

from .conftest import add_article


def test_open_db_creates_schema_with_wal_and_fts(engine):
    with engine.connect() as c:
        assert c.exec_driver_sql("PRAGMA journal_mode").scalar() == "wal"
        tables = {r[0] for r in c.exec_driver_sql("SELECT name FROM sqlite_master")}
    assert {"editions", "articles", "notes", "note_articles", "search"} <= tables


def test_edition_date_and_article_url_are_unique(engine):
    with pytest.raises(IntegrityError), Session(engine) as s, s.begin():
        s.add_all([Edition(edition_date=date(2026, 1, 1)), Edition(edition_date=date(2026, 1, 1))])
    with pytest.raises(IntegrityError), Session(engine) as s, s.begin():
        add_article(s, 1)
        add_article(s, 1)


def test_latest_ready_edition_ignores_generating_and_failed(engine, seeded):
    with Session(engine) as s, s.begin():
        s.add(Edition(edition_date=date(2026, 10, 11), status=EditionStatus.generating))
        s.add(Edition(edition_date=date(2026, 10, 12), status=EditionStatus.failed))
    with Session(engine) as s:
        assert queries.latest_ready_edition(s).id == seeded["edition"]
        assert [e.id for e in queries.list_editions(s)] == [seeded["edition"]]
        assert queries.today_edition(s, date(2026, 10, 12)).status == EditionStatus.failed
        assert queries.ready_edition(s, date(2026, 10, 11)) is None


def test_front_page_sections_and_note(engine, seeded):
    with Session(engine) as s:
        assert [n.id for n in queries.front_page(s, seeded["edition"])] == [seeded["front"]]
        assert [n.id for n in queries.section_notes(s, seeded["edition"], "Mundo")] == [
            seeded["world"]
        ]
        assert queries.section_notes(s, seeded["edition"], "Chile") == []
        assert queries.section_counts(s, seeded["edition"]) == [("Mundo", 1)]
        note = queries.get_note(s, seeded["front"])
        assert [a.source_name for a in note.articles] == ["CIPER", "Cooperativa"]


def test_set_flag_toggles_or_sets_on_notes_and_articles(engine, seeded):
    with Session(engine) as s, s.begin():
        assert queries.set_flag(s, "note", seeded["front"], "favorite") is True
        assert queries.set_flag(s, "note", seeded["front"], "favorite") is False
        assert queries.set_flag(s, "article", seeded["articles"][0], "saved", True) is True
        assert queries.set_flag(s, "article", seeded["articles"][0], "saved", True) is True
        queries.set_flag(s, "note", seeded["world"], "favorite", True)
    with Session(engine) as s:
        notes, articles = queries.flagged(s, "favorite")
        assert [n.id for n in notes] == [seeded["world"]]
        assert articles == []
        notes, articles = queries.flagged(s, "saved")
        assert [a.id for a in articles] == [seeded["articles"][0]]
    with Session(engine) as s, pytest.raises(LookupError):
        queries.set_flag(s, "note", 999, "read")


def test_search_ignores_accents_supports_prefixes_and_is_injection_safe(engine, seeded):
    with Session(engine) as s:
        assert [h.id for h in queries.search(s, "pension")] == [seeded["front"]]
        assert [h.id for h in queries.search(s, "climat")] == [seeded["world"]]
        assert queries.search(s, "senado REFORMA")[0].kind == "note"
        assert queries.search(s, "") == []
        assert queries.search(s, '" OR * NEAR(') == []


def test_deleting_notes_cascades_links(engine, seeded):
    with Session(engine) as s, s.begin():
        s.delete(s.get(Note, seeded["front"]))
    with Session(engine) as s:
        assert s.scalar(select(func.count()).select_from(note_articles)) == 1


def test_open_db_adds_the_interest_columns_to_an_older_database(config):
    path = config.storage.database_path
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE notes (id INTEGER PRIMARY KEY, headline TEXT)")
    old.execute("INSERT INTO notes (headline) VALUES ('Antigua')")
    old.commit()
    old.close()

    open_db(config)
    engine = open_db(config)  # twice: nothing left to add

    with engine.connect() as c:
        columns = {row[1] for row in c.exec_driver_sql("PRAGMA table_info(notes)")}
        row = c.exec_driver_sql("SELECT headline, interest, interest_liked FROM notes").one()
    assert {"interest", "interest_liked"} <= columns
    assert tuple(row) == ("Antigua", None, None)


def test_open_db_tolerates_a_column_another_process_just_added(config, monkeypatch):
    open_db(config)
    # SQLite column names ignore case: "INTEREST" looks missing here but already exists, as
    # when fetch and generate both migrate at boot.
    monkeypatch.setitem(database.ADDED_COLUMNS, "notes", {"INTEREST": "TEXT"})
    open_db(config)
