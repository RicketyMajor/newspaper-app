"""`newspaper generate`: fetch -> extract -> embed -> group -> select -> draft -> publish.

Notes are written to the database only in the final transaction, so readers never see a
half-built edition and a crash leaves at most a stale `generating` row to take over.
"""

from __future__ import annotations

import math
import os
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
import numpy as np
from sqlalchemy import Engine, func, select, update
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
from ..storage.models import Article, Edition, EditionStatus, Note, note_articles

STALE_AFTER = timedelta(hours=6)  # a `generating` row older than this is considered dead
MAX_CHARS_PER_ARTICLE = 6000
MAX_CHARS_PER_NOTE = 24000
EMBED_BATCH = 32
WORDS_PER_MINUTE = 200


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


def local_today(config: NewspaperConfig, now: datetime | None = None) -> date:
    return (now or datetime.now(UTC)).astimezone(ZoneInfo(config.general.timezone)).date()


def pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    if sys.platform == "win32":
        # os.kill(pid, 0) would *terminate* the process on Windows; ask the kernel instead.
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        code = ctypes.c_ulong()
        ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        kernel32.CloseHandle(handle)
        return bool(ok) and code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


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
        _ingest(engine, edition_id, config, http)
        drafts = _build_notes(
            engine, config, http, llm, now.astimezone(UTC).replace(tzinfo=None), log
        )
        _publish(engine, edition_id, drafts)
    except Exception as e:  # any failure: mark it, keep the previous edition readable
        message = str(e) or type(e).__name__
        _finish(engine, edition_id, EditionStatus.failed, message)
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
            fresh = datetime.now(UTC).replace(tzinfo=None) - edition.started_at < STALE_AFTER
            if edition.status == EditionStatus.generating and fresh and pid_alive(edition.pid):
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
                    started_at=datetime.now(UTC).replace(tzinfo=None),
                    finished_at=None,
                    error=None,
                    feed_errors=None,
                )
                .execution_options(synchronize_session=False)
            ).rowcount
            if claimed != 1:
                return None, busy
            _drop_notes(s, edition)
            return edition.id, ""
    except (IntegrityError, OperationalError):  # lost the insert race / database locked
        return None, busy


def _drop_notes(s: Session, edition: Edition) -> None:
    """Remove a previous attempt's notes (forced regeneration) and their search rows."""
    for note in edition.notes:
        queries.unindex(s, "note", [note.id])
        queries.unindex(s, "article", [a.id for a in note.articles])
    edition.notes.clear()


def _ingest(engine: Engine, edition_id: int, config: NewspaperConfig, http: httpx.Client) -> None:
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

    with Session(engine) as s, s.begin():
        s.get(Edition, edition_id).feed_errors = "\n".join(errors) or None
        urls = [raw.url for raw in fetched]
        known = set(s.scalars(select(Article.url).where(Article.url.in_(urls))))
        for raw in fetched:
            if raw.url not in known:
                known.add(raw.url)
                s.add(Article(**vars(raw)))


def _build_notes(
    engine: Engine,
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
                    Article.id.not_in(select(note_articles.c.article_id)),
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
                a.embedding = np.asarray(vector, dtype=np.float32).tobytes()
        s.commit()  # keep extracted text and embeddings even if drafting fails later

    vectors = [np.frombuffer(a.embedding, dtype=np.float32) for a in pending]
    groups = [
        sorted((pending[i] for i in idx), key=lambda a: a.when)
        for idx in cluster(vectors, config.edition.cluster_threshold)
    ]
    selected = select_groups(
        groups,
        config.sections,
        config.edition.front_page_size,
        config.edition.max_notes_per_section,
    )

    drafts = []
    for n, sel in enumerate(selected, 1):
        log(f"Redactando nota {n}/{len(selected)}…")
        drafts.append(_draft(llm, sel, config.general.language))
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


def _draft(llm: Ollama, sel: Selected, language: str) -> _NoteDraft:
    single = len({a.source_name for a in sel.items}) == 1
    try:
        d = llm.draft(_materials(sel.items), language)
        if d.headline and d.body:
            differences = None if single or not d.differences else d.differences
            return _NoteDraft(sel, d.headline, d.dek, d.body, differences, True)
    except OllamaUnavailableError:
        raise
    except OllamaError:
        pass
    # Fallback: the original headlines and excerpts, clearly labelled as not drafted.
    body = "\n\n".join(f"**{a.source_name}**: {a.summary or a.content}" for a in sel.items)
    return _NoteDraft(sel, sel.items[0].title, "", body, None, False)


def _publish(engine: Engine, edition_id: int, drafts: list[_NoteDraft]) -> None:
    with Session(engine) as s, s.begin():
        edition = s.get(Edition, edition_id)
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
        edition.finished_at = datetime.now(UTC).replace(tzinfo=None)


def _finish(engine: Engine, edition_id: int, status: EditionStatus, error: str | None) -> None:
    with Session(engine) as s, s.begin():
        edition = s.get(Edition, edition_id)
        edition.status, edition.error = status, error
        edition.finished_at = datetime.now(UTC).replace(tzinfo=None)
