from datetime import date, datetime

import pytest
from sqlalchemy.orm import Session

from newspaper import schedule
from newspaper.config.models import NewspaperConfig
from newspaper.storage import queries
from newspaper.storage.database import open_db
from newspaper.storage.models import Article, Edition, EditionStatus, Note


@pytest.fixture(autouse=True)
def never_sleep_the_pc(monkeypatch):
    """The tests run on the user's PC: none of them may put it to sleep (#22)."""

    def suspend():
        raise AssertionError("a test tried to sleep the PC")

    monkeypatch.setattr(schedule, "suspend", suspend)


@pytest.fixture
def config(tmp_path):
    return NewspaperConfig(
        storage={"database_path": tmp_path / "newspaper.db"},
        feeds=[
            {"name": "CIPER", "url": "https://ciper.test/feed", "section": "Chile"},
            {"name": "BBC Mundo", "url": "https://bbc.test/rss", "section": "Mundo"},
        ],
    )


@pytest.fixture
def engine(config):
    return open_db(config)


def add_article(s: Session, n: int, *, source: str = "CIPER", section: str = "Chile") -> Article:
    article = Article(
        url=f"https://news.test/{n}",
        title=f"Artículo {n}",
        summary=f"Resumen {n}",
        content=f"Contenido completo {n}",
        content_source="full",
        source_name=source,
        section=section,
        language="es",
        published_at=datetime(2026, 10, 10, 8, n),
    )
    s.add(article)
    return article


@pytest.fixture
def seeded(engine):
    """A ready edition with one front note (2 sources) and one Mundo note (1 source)."""
    with Session(engine) as s, s.begin():
        edition = Edition(edition_date=date(2026, 10, 10), status=EditionStatus.ready)
        a1, a2 = add_article(s, 1), add_article(s, 2, source="Cooperativa")
        a3 = add_article(s, 3, source="BBC Mundo", section="Mundo")
        front = Note(
            section="Chile",
            rank=0,
            is_front=True,
            headline="Senado aprueba reforma de pensiones",
            dek="La votación cerró anoche.",
            body="El Senado aprobó la reforma de pensiones tras meses de debate.",
            differences="CIPER destaca el costo fiscal; Cooperativa, el calendario.",
            single_source=False,
            excerpt_only=False,
            read_minutes=2,
            articles=[a1, a2],
        )
        world = Note(
            section="Mundo",
            rank=1,
            is_front=False,
            headline="Cumbre climática cierra sin acuerdo",
            body="Los países no lograron un texto común.",
            single_source=True,
            excerpt_only=False,
            read_minutes=1,
            articles=[a3],
        )
        edition.notes = [front, world]
        s.add(edition)
        s.flush()
        for note in (front, world):
            queries.index_for_search(s, "note", note.id, note.headline, note.body)
        return {
            "edition": edition.id,
            "front": front.id,
            "world": world.id,
            "articles": [a1.id, a2.id, a3.id],
        }
