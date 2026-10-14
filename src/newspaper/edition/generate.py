"""`newspaper generate`: fetch -> extract -> embed -> group -> select -> draft -> publish.

Notes are written to the database only in the final transaction, so readers never see a
half-built edition and a crash leaves at most a stale `generating` row to take over.
"""

from __future__ import annotations

import math
import os
import re
from array import array
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

import httpx
from sqlalchemy import Engine, func, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from ..ai.ollama import Ollama, OllamaError, OllamaUnavailableError
from ..config.models import NewspaperConfig
from ..ingestion.extract import extract_many
from ..ingestion.rss import fetch_feed, make_client
from ..processing.cluster import cluster
from ..processing.select import Selected, select_groups
from ..storage import queries
from ..storage.database import open_db
from ..storage.models import Article, Edition, EditionStatus, Note, note_articles, utcnow
from .today import local_today, running

MAX_CHARS_PER_ARTICLE = 6000
MAX_CHARS_PER_NOTE = 24000
EMBED_BATCH = 32
# ponytail: this many failed notes in a row means Ollama is hung (each waits timeout_seconds),
# not a bad note; fail the edition instead of publishing it undrafted.
FAILED_DRAFTS_IN_A_ROW = 3
WORDS_PER_MINUTE = 200
# The first letter after any leading punctuation (¿, «, quotes); a leading digit or a Markdown
# code span (`) means none.
_FIRST_LETTER = re.compile(r"^([^\w`]*)([^\W\d_])")


def _starts_lower(text: str) -> bool:
    m = _FIRST_LETTER.match(text)
    return m is not None and m[2].islower()


def _capitalize(text: str) -> str:
    # ponytail: a brand like "iPhone" would become "IPhone"; skip known ones if it shows up.
    return _FIRST_LETTER.sub(lambda m: m[1] + m[2].upper(), text, count=1)


@dataclass
class GenerateResult:
    status: str  # "ready" | "failed" | "skipped"
    message: str
    edition_date: date


@dataclass
class _NoteDraft:
    selected: Selected
    headline: str
    dek: str
    body: str
    differences: str | None
    drafted: bool


def generate(
    config: NewspaperConfig,
    *,
    force: bool = False,
    http: httpx.Client | None = None,
    llm: Ollama | None = None,
    now: datetime | None = None,
    log: Callable[[str], None] = lambda _msg: None,
) -> GenerateResult:
    now = now or datetime.now(UTC)
    day = local_today(config, now)
    engine = open_db(config)

    edition_id, reason = _claim(engine, day, force)
    if edition_id is None:
        return GenerateResult("skipped", reason, day)

    http = http or make_client()
    llm = llm or Ollama(config.llm)
    try:
        log("Leyendo fuentes…")
        _, errors = ingest(engine, config, http)
        with Session(engine) as s, s.begin():
            s.get(Edition, edition_id).feed_errors = "\n".join(errors) or None
        drafts = _build_notes(
            engine, edition_id, config, http, llm, now.astimezone(UTC).replace(tzinfo=None), log
        )
        _publish(engine, edition_id, drafts)
    except Exception as e:  # any failure: mark it, keep the previous edition readable
        message = str(e) or type(e).__name__
        _fail(engine, edition_id, message)
        return GenerateResult("failed", message, day)
    return GenerateResult("ready", f"Edición publicada con {len(drafts)} notas.", day)


def _claim(engine: Engine, day: date, force: bool) -> tuple[int | None, str]:
    """Own today's edition row, or explain why not."""
    busy = "Ya hay una generación en curso."
    try:
        with Session(engine) as s, s.begin():
            edition = queries.today_edition(s, day)
            if edition is None:
                edition = Edition(edition_date=day, pid=os.getpid())
                s.add(edition)
                s.flush()
                return edition.id, ""
            if edition.status == EditionStatus.ready and not force:
                return None, "La edición de hoy ya está lista (usa --force para regenerarla)."
            if running(edition):
                return None, busy
            # Compare-and-set on (status, started_at): only one process wins a takeover.
            claimed = s.execute(
                update(Edition)
                .where(
                    Edition.id == edition.id,
                    Edition.status == edition.status,
                    Edition.started_at == edition.started_at,
                )
                .values(
                    status=EditionStatus.generating,
                    pid=os.getpid(),
                    started_at=utcnow(),
                    finished_at=None,
                    error=None,
                    feed_errors=None,
                )
                .execution_options(synchronize_session=False)
            ).rowcount
            # The old notes stay until _publish replaces them, so a failed --force keeps them.
            return (edition.id, "") if claimed == 1 else (None, busy)
    except (IntegrityError, OperationalError):  # lost the insert race / database locked
        return None, busy


def _drop_notes(s: Session, edition: Edition) -> None:
    """Remove a previous attempt's notes (forced regeneration) and their search rows."""
    queries.unindex(s, "note", [n.id for n in edition.notes])
    queries.unindex(s, "article", [a.id for n in edition.notes for a in n.articles])
    edition.notes.clear()


def ingest(engine: Engine, config: NewspaperConfig, http: httpx.Client) -> tuple[int, list[str]]:
    """Fetch every active feed and store its new articles: (how many were new, feed errors).
    Safe while another process ingests (`fetch` and `generate` may overlap): a URL already
    stored, or listed twice, is skipped."""
    feeds = config.active_feeds
    if not feeds:
        raise RuntimeError("No hay fuentes activas en newspaper.toml.")
    fetched, errors = [], []
    for feed in feeds:
        try:
            fetched += fetch_feed(http, feed)
        except Exception as e:
            errors.append(f"{feed.name}: {e}")
    if len(errors) == len(feeds):
        raise RuntimeError("Ninguna fuente respondió.\n" + "\n".join(errors))
    if not fetched:
        return 0, errors
    insert = sqlite_insert(Article.__table__).on_conflict_do_nothing(index_elements=["url"])
    with engine.begin() as conn:
        new = conn.execute(insert, [vars(raw) for raw in fetched]).rowcount
    return new, errors


def _build_notes(
    engine: Engine,
    edition_id: int,
    config: NewspaperConfig,
    http: httpx.Client,
    llm: Ollama,
    now_utc: datetime,
    log: Callable[[str], None],
) -> list[_NoteDraft]:
    # expire_on_commit=False: articles stay usable after the session closes (for _publish).
    with Session(engine, expire_on_commit=False) as s:
        since = now_utc - timedelta(hours=config.edition.window_hours)
        pending = list(
            s.scalars(
                select(Article)
                .where(
                    func.coalesce(Article.published_at, Article.fetched_at) >= since,
                    Article.source_name.in_([f.name for f in config.active_feeds]),
                    Article.id.not_in(  # published in another edition (a rerun reuses its own)
                        select(note_articles.c.article_id)
                        .join(Note, Note.id == note_articles.c.note_id)
                        .where(Note.edition_id != edition_id)
                    ),
                )
                .order_by(Article.id)
            )
        )
        if not pending:
            raise RuntimeError("Sin artículos nuevos desde la última edición.")

        missing = [a for a in pending if a.content is None]
        log(f"Extrayendo texto de {len(missing)} artículos…")
        texts = extract_many(http, [a.url for a in missing])
        for a in missing:
            text = texts[a.url]
            a.content, a.content_source = (
                (text, "full") if text else (a.summary or a.title, "excerpt")
            )

        need = [a for a in pending if a.embedding is None]
        log(f"Calculando similitud de {len(need)} artículos…")
        for i in range(0, len(need), EMBED_BATCH):
            batch = need[i : i + EMBED_BATCH]
            vectors = llm.embed([f"{a.title}\n\n{(a.content or '')[:1000]}" for a in batch])
            for a, vector in zip(batch, vectors, strict=True):
                a.embedding = array("f", vector).tobytes()  # float32
        s.commit()  # keep extracted text and embeddings even if drafting fails later

    vectors = [array("f", a.embedding) for a in pending]
    groups = [
        sorted((pending[i] for i in idx), key=lambda a: a.when)
        for idx in cluster(vectors, config.edition.cluster_threshold)
    ]
    selected = select_groups(
        groups,
        config.sections,
        config.edition.front_page_size,
        config.edition.max_notes_per_section,
        config.edition.section_limits,
    )

    drafts, failed_in_a_row = [], 0
    for n, sel in enumerate(selected, 1):
        log(f"Redactando nota {n}/{len(selected)}…")
        drafts.append(_draft(llm, sel, config.general.language, log))
        failed_in_a_row = 0 if drafts[-1].drafted else failed_in_a_row + 1
        if failed_in_a_row == FAILED_DRAFTS_IN_A_ROW:
            raise OllamaUnavailableError(
                f"Ollama dejó de responder: {failed_in_a_row} notas seguidas sin redactar."
            )
    return drafts


def _materials(articles: list[Article]) -> list[tuple[str, str, str]]:
    budget, out = MAX_CHARS_PER_NOTE, []
    for a in articles:
        if budget <= 0:
            break
        text = (a.content or a.summary)[: min(MAX_CHARS_PER_ARTICLE, budget)]
        budget -= len(text)
        out.append((a.source_name, a.title, text))
    return out


def _draft(llm: Ollama, sel: Selected, language: str, log: Callable[[str], None]) -> _NoteDraft:
    single = len({a.source_name for a in sel.items}) == 1
    title = sel.items[0].title
    best = None
    # Sampling sometimes yields bad JSON, an empty body or a lowercase headline: retry once.
    for attempt in (1, 2):
        try:
            d = llm.draft(_materials(sel.items), language, retry=attempt > 1)
        except OllamaUnavailableError:
            raise
        except OllamaError as e:
            log(f"  Intento {attempt} fallido ({title}): {e}")
            continue
        if not (d.headline and d.body):
            log(f"  Intento {attempt} fallido ({title}): titular o cuerpo vacío")
            continue
        if not _starts_lower(d.headline):
            best = d
            break
        log(f"  Intento {attempt}: titular en minúscula ({title})")
        best = best or d  # keep the first one in case the retry does no better
    if best is None:
        log(f"  Publicada sin redactar ({title})")
        # Fallback: the original headlines and excerpts, clearly labelled as not drafted.
        body = "\n\n".join(f"**{a.source_name}**: {a.summary or a.content}" for a in sel.items)
        return _NoteDraft(sel, sel.items[0].title, "", body, None, False)
    differences = None if single or not best.differences else best.differences
    return _NoteDraft(
        sel,
        _capitalize(best.headline),
        _capitalize(best.dek),
        _capitalize(best.body),
        differences,
        True,
    )


def _publish(engine: Engine, edition_id: int, drafts: list[_NoteDraft]) -> None:
    with Session(engine) as s, s.begin():
        edition = s.get(Edition, edition_id)
        _drop_notes(s, edition)
        s.flush()
        for rank, d in enumerate(drafts):
            articles = [s.get(Article, a.id) for a in d.selected.items]
            text = " ".join(filter(None, [d.headline, d.dek, d.body, d.differences]))
            note = Note(
                section=d.selected.section,
                rank=rank,
                is_front=d.selected.is_front,
                headline=d.headline,
                dek=d.dek,
                body=d.body,
                differences=d.differences,
                single_source=len({a.source_name for a in articles}) == 1,
                excerpt_only=all(a.content_source == "excerpt" for a in articles),
                drafted=d.drafted,
                read_minutes=max(1, math.ceil(len(text.split()) / WORDS_PER_MINUTE)),
                articles=articles,
            )
            edition.notes.append(note)
            s.flush()
            queries.index_for_search(s, "note", note.id, d.headline, f"{d.dek} {d.body}")
            for a in articles:
                queries.index_for_search(s, "article", a.id, a.title, a.content or a.summary)
        edition.status = EditionStatus.ready
        edition.finished_at = utcnow()


def _fail(engine: Engine, edition_id: int, error: str) -> None:
    with Session(engine) as s, s.begin():
        edition = s.get(Edition, edition_id)
        # A failed regeneration keeps the notes it was going to replace, so stay readable.
        edition.status = EditionStatus.ready if edition.notes else EditionStatus.failed
        edition.error, edition.finished_at = error, utcnow()
