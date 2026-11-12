"""A photo per note (#18): after publishing, each note's photo from its outlets is downloaded
through the shared client (its SSRF guard and timeout) and kept on this PC, so a page never
loads an image from another site."""

from __future__ import annotations

import re
import threading
from collections import defaultdict
from collections.abc import Callable, Set
from contextlib import suppress
from datetime import date, timedelta
from pathlib import Path
from time import monotonic

import httpx
from sqlalchemy import Engine, select, update
from sqlalchemy.orm import Session

from ..config.models import NewspaperConfig
from ..storage.models import Article, Edition, Note, note_articles

TYPES = {"jpg": "image/jpeg", "png": "image/png", "webp": "image/webp", "avif": "image/avif"}
# Below the floor: tracking pixels, icons and most logos.
MAX_BYTES, MIN_BYTES, KEEP_DAYS = 3_000_000, 10_000, 60
WORKERS, DOWNLOAD_SECONDS, BATCH_SECONDS = 8, 30, 120
NAME = re.compile(r"[0-9]+\.(jpg|png|webp|avif)")  # the only files this module writes or deletes


def images_folder(config: NewspaperConfig) -> Path:
    """`<database stem>-images` next to the database: a folder of the app's own, since prune
    deletes the photos no note names (a plain `images` could be the reader's)."""
    db = Path(config.storage.database_path).expanduser()
    return db.parent / f"{db.stem}-images"


def sniff(data: bytes) -> str | None:
    """The format by the first bytes, never by the URL or the Content-Type: SVG (it can carry
    scripts) and GIF never pass."""
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if data[4:12] in (b"ftypavif", b"ftypavis"):
        return "avif"
    return None


def download(client: httpx.Client, url: str) -> tuple[bytes, str] | None:
    """(bytes, format) of a photo between the floor and the cap, else None. Never raises.
    The client's timeout is per read: DOWNLOAD_SECONDS bounds a server dripping bytes. The bytes
    counted are the wire's: a compressed body would inflate past the cap before it is seen, so
    none is asked for, and one sent anyway doesn't sniff as a photo."""
    deadline = monotonic() + DOWNLOAD_SECONDS
    try:
        with client.stream("GET", url, headers={"Accept-Encoding": "identity"}) as response:
            if response.status_code != 200:
                return None
            data = bytearray()
            for chunk in response.iter_raw():
                data += chunk
                if len(data) > MAX_BYTES or monotonic() > deadline:
                    return None
    except Exception:  # a refused host (BlockedURL), a timeout, a reset: no photo
        return None
    ext = sniff(data)
    return (bytes(data), ext) if ext and len(data) >= MIN_BYTES else None


def pick(notes: list[Note], used: Set[str] = frozenset()) -> dict[int, tuple[str, str]]:
    """note id -> (url, outlet): its first article's photo that no other note has, nor an
    earlier edition (`used`). The same URL on two or more notes is usually the outlet's logo or
    placeholder; one seen on another day, a series banner (CIPER's daily «Radar»)."""
    notes_with = defaultdict(set)
    for note in notes:
        for a in note.articles:
            if a.image_url:
                notes_with[a.image_url].add(note.id)
    chosen = {}
    for note in notes:
        for a in note.articles:
            if a.image_url and a.image_url not in used and len(notes_with[a.image_url]) == 1:
                chosen[note.id] = (a.image_url, a.source_name)
                break
    return chosen


def attach_images(
    engine: Engine,
    edition_id: int,
    client: httpx.Client,
    folder: Path,
    log: Callable[[str], None],
) -> None:
    """Download the edition's photos as folder/<note id>.<format>, within BATCH_SECONDS."""
    with Session(engine) as s:
        notes = list(s.scalars(select(Note).where(Note.edition_id == edition_id)))
        urls = {a.image_url for n in notes for a in n.articles if a.image_url}
        used = s.scalars(
            select(Article.image_url)
            .distinct()
            .join(note_articles, note_articles.c.article_id == Article.id)
            .join(Note, Note.id == note_articles.c.note_id)
            .where(Note.edition_id != edition_id, Article.image_url.in_(urls))
        )
        chosen = pick(notes, set(used))
    folder.mkdir(parents=True, exist_ok=True)
    got = _download_all(client, {note_id: url for note_id, (url, _) in chosen.items()})
    saved = {}
    for note_id, photo in got.items():
        if photo:
            data, ext = photo
            saved[note_id] = f"{note_id}.{ext}"
            (folder / saved[note_id]).write_bytes(data)
    with Session(engine) as s, s.begin():  # after the files: a page never names a missing one
        for note_id, name in saved.items():
            note = s.get(Note, note_id)
            note.image, note.image_credit = name, chosen[note_id][1]
    late = len(chosen) - len(got)
    log(f"Fotos: {len(saved)} de {len(notes)}" + (f" ({late} sin terminar)" if late else ""))


def _download_all(client: httpx.Client, wanted: dict[int, str]) -> dict[int, object]:
    """note id -> download(), WORKERS at a time, for those done within BATCH_SECONDS. A host
    dripping its headers beats every per-read timeout, so the batch has its own clock; its
    threads are daemons, so one still stuck never holds `generate` at exit."""
    deadline = monotonic() + BATCH_SECONDS
    gate, lock, done = threading.Semaphore(WORKERS), threading.Lock(), {}

    def fetch(note_id: int, url: str) -> None:
        with gate:
            if monotonic() < deadline:  # a late start: the batch is over
                photo = download(client, url)
                with lock:
                    done[note_id] = photo

    threads = [threading.Thread(target=fetch, args=item, daemon=True) for item in wanted.items()]
    for t in threads:
        t.start()
    for t in threads:
        t.join(max(0, deadline - monotonic()))
    with lock:
        return dict(done)


def prune_images(engine: Engine, folder: Path, today: date) -> None:
    """Photos last KEEP_DAYS: older editions' notes lose theirs. Then every photo file no note
    names goes (a forced run's old notes, a write cut short)."""
    old = select(Edition.id).where(Edition.edition_date < today - timedelta(days=KEEP_DAYS))
    with Session(engine) as s, s.begin():
        s.execute(
            update(Note)
            .where(Note.image.is_not(None), Note.edition_id.in_(old))
            .values(image=None, image_credit=None)
            .execution_options(synchronize_session=False)
        )
        kept = set(s.scalars(select(Note.image).where(Note.image.is_not(None))))
    if not folder.is_dir():
        return
    for path in folder.iterdir():  # only photo names: the folder may hold the reader's files
        if NAME.fullmatch(path.name) and path.name not in kept and path.is_file():
            with suppress(OSError):  # the web is sending it (Windows): next time
                path.unlink()
