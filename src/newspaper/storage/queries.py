"""Read-side queries and reading-state changes shared by the TUI and the web app."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Literal

from sqlalchemy import bindparam, func, select, text
from sqlalchemy.orm import Session, selectinload

from .models import Article, Edition, EditionStatus, Note

Kind = Literal["note", "article"]
Flag = Literal["read", "favorite", "saved"]

_MODELS: dict[str, type[Note] | type[Article]] = {"note": Note, "article": Article}
_FLAGS = {"read": "is_read", "favorite": "is_favorite", "saved": "is_saved"}


@dataclass(frozen=True)
class SearchHit:
    kind: Kind
    id: int
    title: str
    snippet: str


_READY = select(Edition).where(Edition.status == EditionStatus.ready)


def latest_ready_edition(s: Session) -> Edition | None:
    return s.scalars(_READY.order_by(Edition.edition_date.desc()).limit(1)).first()


def ready_edition(s: Session, day: date) -> Edition | None:
    return s.scalars(_READY.where(Edition.edition_date == day)).first()


def today_edition(s: Session, day: date) -> Edition | None:
    """The edition row for `day` in any status (to show generation progress)."""
    return s.scalars(select(Edition).where(Edition.edition_date == day)).first()


def list_editions(s: Session) -> list[Edition]:
    return list(s.scalars(_READY.order_by(Edition.edition_date.desc())))


def front_page(s: Session, edition_id: int) -> list[Note]:
    stmt = select(Note).where(Note.edition_id == edition_id, Note.is_front).order_by(Note.rank)
    return list(s.scalars(stmt.options(selectinload(Note.articles))))


def section_notes(s: Session, edition_id: int, section: str) -> list[Note]:
    stmt = (
        select(Note)
        .where(Note.edition_id == edition_id, Note.section == section, ~Note.is_front)
        .order_by(Note.rank)
    )
    return list(s.scalars(stmt.options(selectinload(Note.articles))))


def section_counts(s: Session, edition_id: int) -> list[tuple[str, int]]:
    """(section, notes outside the front page), in edition order."""
    stmt = (
        select(Note.section, func.count())
        .where(Note.edition_id == edition_id, ~Note.is_front)
        .group_by(Note.section)
        .order_by(func.min(Note.rank))
    )
    return [(section, count) for section, count in s.execute(stmt)]


def reading_order(notes: list[Note]) -> tuple[list[Note], list[Note]]:
    """(core, rest) of an edition's notes in rank order. The core is what the day's reading
    can finish: the front page, then the first note of each section outside it. The reading
    path is core + rest."""
    firsts: dict[str, Note] = {}
    for note in notes:
        if not note.is_front:
            firsts.setdefault(note.section, note)
    core = [n for n in notes if n.is_front] + list(firsts.values())
    rest = [n for n in notes if n not in core]  # ponytail: O(n²) on ~40 notes
    return core, rest


def get_note(s: Session, note_id: int) -> Note | None:
    stmt = select(Note).where(Note.id == note_id)
    return s.scalars(stmt.options(selectinload(Note.articles), selectinload(Note.edition))).first()


def search(s: Session, query: str, limit: int = 50) -> list[SearchHit]:
    # Quote every word so user input can never become FTS5 syntax; trailing * = prefix match.
    terms = re.findall(r"\w+", query)
    if not terms:
        return []
    match = " ".join(f'"{t}"*' for t in terms)
    rows = s.execute(
        text(
            "SELECT kind, ref_id, title, snippet(search, 3, '', '', '…', 16) FROM search "
            "WHERE search MATCH :match ORDER BY rank LIMIT :limit"
        ),
        {"match": match, "limit": limit},
    )
    return [SearchHit(kind, int(ref_id), title, snippet) for kind, ref_id, title, snippet in rows]


def index_for_search(s: Session, kind: Kind, ref_id: int, title: str, body: str) -> None:
    s.execute(
        text("INSERT INTO search (kind, ref_id, title, body) VALUES (:k, :r, :t, :b)"),
        {"k": kind, "r": ref_id, "t": title, "b": body},
    )


def unindex(s: Session, kind: Kind, ref_ids: list[int]) -> None:
    stmt = text("DELETE FROM search WHERE kind = :k AND ref_id IN :ids")
    s.execute(stmt.bindparams(bindparam("ids", expanding=True)), {"k": kind, "ids": ref_ids})


def flagged(s: Session, flag: Flag) -> tuple[list[Note], list[Article]]:
    """Notes and articles with `flag` set, newest first."""
    attr = _FLAGS[flag]
    notes = s.scalars(
        select(Note)
        .where(getattr(Note, attr))
        .options(selectinload(Note.edition), selectinload(Note.articles))
        .order_by(Note.edition_id.desc(), Note.rank)
    )
    articles = s.scalars(
        select(Article).where(getattr(Article, attr)).order_by(Article.published_at.desc())
    )
    return list(notes), list(articles)


def set_flag(s: Session, kind: Kind, obj_id: int, flag: Flag, value: bool | None = None) -> bool:
    """Set a reading flag, or toggle it when `value` is None. Returns the new value."""
    obj = s.get(_MODELS[kind], obj_id)
    if obj is None:
        raise LookupError(f"No existe {kind} {obj_id}")
    attr = _FLAGS[flag]
    new = (not getattr(obj, attr)) if value is None else value
    setattr(obj, attr, new)
    return new
