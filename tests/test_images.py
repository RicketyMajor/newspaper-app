import gzip
import threading
import time
from collections.abc import Iterator
from datetime import date, timedelta
from pathlib import Path

import httpx
import pytest
from sqlalchemy.orm import Session

from newspaper.edition import images
from newspaper.edition.images import (
    MAX_BYTES,
    attach_images,
    download,
    images_folder,
    pick,
    prune_images,
    sniff,
)
from newspaper.ingestion import rss
from newspaper.storage.models import Article, Edition, EditionStatus, Note

from .conftest import JPEG

URL = "https://img.test/foto.jpg"
PNG = b"\x89PNG\r\n\x1a\n" + bytes(20_000)


@pytest.mark.parametrize(
    ("data", "ext"),
    [
        (JPEG[:16], "jpg"),
        (PNG[:16], "png"),
        (b"RIFF\x24\x00\x00\x00WEBPVP8 ", "webp"),
        (b"\x00\x00\x00\x1cftypavif\x00\x00", "avif"),
        (b"\x00\x00\x00\x1cftypavis\x00\x00", "avif"),  # an animated AVIF's brand
        (b"GIF89a\x01\x00\x01\x00", None),
        (b'<svg xmlns="http://www.w3.org/2000/svg"><script/></svg>', None),
        (b"<!doctype html><html><body>404</body></html>", None),
    ],
)
def test_sniff_names_a_photo_by_its_bytes_only(data, ext):
    assert sniff(data) == ext


def wire(content: bytes) -> Iterator[bytes]:
    """A body as the network sends it: download reads the raw stream, and httpx counts a
    response built from bytes as already read."""
    yield content


def serving(status: int, content: bytes | Iterator[bytes] = b"") -> httpx.Client:
    def handler(request):
        body = wire(content) if isinstance(content, bytes) else content
        return httpx.Response(status, content=body)

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_download_keeps_a_photo_and_names_its_format():
    assert download(serving(200, content=JPEG), URL) == (JPEG, "jpg")


@pytest.mark.parametrize(
    ("status", "content"),
    [
        pytest.param(200, b"\xff\xd8\xff" + bytes(4_000_000), id="past the 3 MB cap"),
        pytest.param(200, b"\xff\xd8\xff" + bytes(5_000), id="a pixel or a logo"),
        pytest.param(200, b"<svg>" + b" " * 20_000 + b"</svg>", id="svg"),
        pytest.param(404, JPEG, id="404"),
    ],
)
def test_download_drops_what_is_not_a_photo_worth_keeping(status, content):
    assert download(serving(status, content=content), URL) is None


def test_download_stops_reading_past_the_cap():
    sent = []

    def endless():
        while True:
            sent.append(1)
            yield b"\xff\xd8\xff" + bytes(1_000_000)

    assert download(serving(200, content=endless()), URL) is None
    assert len(sent) <= MAX_BYTES // 1_000_000 + 1


def test_download_counts_the_bytes_on_the_wire_and_never_inflates_them():
    # A gzip body of a few KB can inflate to gigabytes before the cap sees it: ask for none,
    # and a body that still comes compressed doesn't sniff as a photo.
    asked = []

    def handler(request):
        asked.append(request.headers.get("accept-encoding"))
        squeezed = gzip.compress(JPEG)  # a photo once inflated, a few dozen bytes on the wire
        return httpx.Response(200, content=wire(squeezed), headers={"Content-Encoding": "gzip"})

    assert download(httpx.Client(transport=httpx.MockTransport(handler)), URL) is None
    assert asked == ["identity"]


def test_download_gives_up_on_a_photo_that_drips_past_its_deadline(monkeypatch):
    # Each read beats the client's timeout, but the whole photo would take 200 s.
    clock = [0.0]
    monkeypatch.setattr(images, "monotonic", lambda: clock[0])

    def dripping():
        for i in range(0, len(JPEG), 1000):
            clock[0] += 10
            yield JPEG[i : i + 1000]

    assert download(serving(200, content=dripping()), URL) is None
    assert clock[0] <= images.DOWNLOAD_SECONDS + 10


def test_download_goes_through_the_guard_of_the_shared_client():
    photo = httpx.MockTransport(lambda r: httpx.Response(200, content=JPEG))
    assert download(rss.make_client(transport=photo), "http://127.0.0.1:8765/foto.jpg") is None


def note_with(note_id: int, *photos: str | None) -> Note:
    articles = [
        Article(url=f"https://n.test/{note_id}/{i}", source_name=f"Medio {i}", image_url=photo)
        for i, photo in enumerate(photos)
    ]
    return Note(id=note_id, articles=articles)


def test_pick_takes_each_notes_first_photo_and_drops_one_shared_by_two_notes():
    notes = [
        note_with(1, None, "https://img.test/a.jpg", "https://img.test/b.jpg"),
        note_with(2, "https://img.test/logo.png"),  # the outlet's logo, on two notes
        note_with(3, "https://img.test/logo.png", "https://img.test/c.jpg"),
        note_with(4, None),
    ]
    assert pick(notes) == {
        1: ("https://img.test/a.jpg", "Medio 1"),
        3: ("https://img.test/c.jpg", "Medio 1"),  # the next outlet's photo, not the logo
    }


def edition_on(s: Session, day: date, *headlines: str) -> list[Note]:
    notes = [Note(section="Chile", rank=i, headline=h, body=".") for i, h in enumerate(headlines)]
    s.add(Edition(edition_date=day, status=EditionStatus.ready, notes=notes))
    s.flush()
    return notes


def article(n: int, outlet: str, photo: str) -> Article:
    return Article(url=f"https://n.test/{n}", title="T", source_name=outlet, section="Chile",
                   image_url=photo)  # fmt: skip


def test_attach_images_saves_each_photo_under_its_notes_id(config, engine):
    folder = images_folder(config)
    with Session(engine) as s, s.begin():
        [with_photo, broken, without] = edition_on(s, date(2026, 11, 4), "A", "B", "C")
        with_photo.articles = [article(1, "CIPER", URL)]
        broken.articles = [article(2, "BBC Mundo", "https://img.test/404.jpg")]
        ids, edition_id = (with_photo.id, broken.id, without.id), with_photo.edition_id

    def handler(request):
        ok = str(request.url) == URL
        return httpx.Response(200, content=wire(JPEG)) if ok else httpx.Response(404)

    lines = []
    client = httpx.Client(transport=httpx.MockTransport(handler))
    attach_images(engine, edition_id, client, folder, lines.append)

    with Session(engine) as s:
        got = [(s.get(Note, i).image, s.get(Note, i).image_credit) for i in ids]
    assert got == [(f"{ids[0]}.jpg", "CIPER"), (None, None), (None, None)]
    assert (folder / f"{ids[0]}.jpg").read_bytes() == JPEG
    assert lines == ["Fotos: 1 de 3"]


def test_a_photo_an_earlier_edition_used_is_a_banner_not_news(config, engine):
    # CIPER's daily «Radar» carries the same banner: one note a day, never two in an edition.
    banner = "https://img.test/banner-radar.png"
    with Session(engine) as s, s.begin():
        [yesterday] = edition_on(s, date(2026, 11, 3), "Radar del 3")
        yesterday.articles = [article(1, "CIPER", banner)]
        [radar, news] = edition_on(s, date(2026, 11, 4), "Radar del 4", "Noticia")
        radar.articles = [article(2, "CIPER", banner)]
        news.articles = [article(3, "CIPER", URL)]
        ids, edition_id = (radar.id, news.id), radar.edition_id

    lines, folder = [], images_folder(config)
    attach_images(engine, edition_id, serving(200, content=JPEG), folder, lines.append)

    with Session(engine) as s:
        assert [s.get(Note, i).image for i in ids] == [None, f"{ids[1]}.jpg"]
    assert lines == ["Fotos: 1 de 2"]


def test_a_stuck_download_never_holds_the_edition(config, engine, monkeypatch):
    # A host dripping its headers beats every per-read timeout: the batch has its own clock,
    # else generate would never return (no notice, the PC kept awake).
    stuck, never = "https://img.test/goteo.jpg", threading.Event()

    def download(client, url):
        if url == stuck:
            never.wait(5)  # bounded, so the old code fails the test instead of hanging it
            return None
        return JPEG, "jpg"

    monkeypatch.setattr(images, "download", download)
    monkeypatch.setattr(images, "BATCH_SECONDS", 0.3, raising=False)
    with Session(engine) as s, s.begin():
        [slow, news] = edition_on(s, date(2026, 11, 4), "Lenta", "Noticia")
        slow.articles = [article(1, "CIPER", stuck)]
        news.articles = [article(2, "CIPER", URL)]
        ids, edition_id = (slow.id, news.id), slow.edition_id

    lines, started = [], time.monotonic()
    try:
        attach_images(engine, edition_id, serving(200), images_folder(config), lines.append)
    finally:
        never.set()
    assert time.monotonic() - started < 2
    with Session(engine) as s:
        assert [s.get(Note, i).image for i in ids] == [None, f"{ids[1]}.jpg"]
    assert lines == ["Fotos: 1 de 2 (1 sin terminar)"]


def test_prune_drops_photos_past_60_days_and_files_no_note_uses(config, engine):
    today = date(2026, 11, 4)
    folder = images_folder(config)
    folder.mkdir()
    with Session(engine) as s, s.begin():
        [old] = edition_on(s, today - timedelta(days=61), "Vieja")
        [recent] = edition_on(s, today - timedelta(days=1), "Reciente")
        for note in (old, recent):
            note.image, note.image_credit = f"{note.id}.jpg", "CIPER"
            (folder / note.image).write_bytes(JPEG)
        old_id, recent_id = old.id, recent.id
    (folder / "999.png").write_bytes(PNG)  # a note dropped by a forced run

    prune_images(engine, folder, today)

    with Session(engine) as s:
        assert (s.get(Note, old_id).image, s.get(Note, old_id).image_credit) == (None, None)
        assert s.get(Note, recent_id).image == f"{recent_id}.jpg"
    assert sorted(p.name for p in folder.iterdir()) == [f"{recent_id}.jpg"]


def test_the_photos_get_a_folder_named_after_the_database(config):
    # With database_path = "~/Documents/newspaper.db", a plain ~/Documents/images could be the
    # reader's own, and prune deletes the numbered photos no note names.
    db = Path(config.storage.database_path).expanduser()
    assert images_folder(config) == db.parent / f"{db.stem}-images"


def test_prune_only_ever_deletes_photo_names(config, engine):
    # Even in its own folder, prune touches only the names this module writes.
    folder = images_folder(config)
    folder.mkdir()
    for name in ("notes.txt", "holiday.jpg", "12.jpg.bak", "7.jpg"):
        (folder / name).write_bytes(JPEG)
    prune_images(engine, folder, date(2026, 11, 4))
    assert sorted(p.name for p in folder.iterdir()) == ["12.jpg.bak", "holiday.jpg", "notes.txt"]
