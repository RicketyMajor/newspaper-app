import json
from datetime import date, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from newspaper.config.models import CineConfig
from newspaper.edition.generate import ingest
from newspaper.ingestion import cine
from newspaper.storage import queries
from newspaper.storage.models import CinemaVenue, Screening

FIXTURES = Path(__file__).parent / "fixtures"
TODAY = date(2026, 11, 1)  # the day the fixtures were saved
PAGES = {
    cine.NORMANDIE_URL: "cine_normandie.json",
    cine.CEINA_URL: "cine_ceina.html",
    cine.ENCARTELERA_URL: "cine_encartelera.html",
}


def site(fail=(), seen=None) -> httpx.Client:
    """The three sites as they were on 1 Nov; `fail` answers 500, `seen` collects the URLs."""

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if seen is not None:
            seen.append(url)
        if url in fail:
            return httpx.Response(500)
        if url in PAGES:
            return httpx.Response(200, content=(FIXTURES / PAGES[url]).read_bytes())
        return httpx.Response(404)

    return httpx.Client(transport=httpx.MockTransport(handler))


def normandie_content() -> str:
    pages = json.loads((FIXTURES / "cine_normandie.json").read_text("utf-8"))
    return pages[0]["content"]["rendered"]


def test_normandie_reads_the_week_and_the_other_shows():
    shows = cine.parse_normandie(normandie_content(), TODAY)
    assert len(shows) == 28  # 23 in the week block + 5 under «Otras funciones»
    assert shows[0] == cine.Show(
        date(2026, 10, 29), "15:00", "Isla de Perros", None,
        "https://www.flow.cl/btn.php?token=ug9bres",
    )  # fmt: skip
    nine = next(s for s in shows if s.title == "Nine")
    assert (nine.day, nine.time, nine.note) == (date(2026, 11, 2), "15:00", "Alex Doll presenta")
    # «Domingo 8» comes in an <h4> under class="sabado": the heading's text decides.
    last = shows[-1]
    assert (last.day, last.time, last.title) == (
        date(2026, 11, 8), "19:15", "Wildwood: El bosque salvaje",
    )  # fmt: skip
    assert not any("PRONTO" in s.title for s in shows)


def test_normandie_places_a_day_by_its_weekday_not_its_month():
    assert cine.day_near(3, 29, TODAY) == date(2026, 10, 29)  # «Jueves 29»
    assert cine.day_near(6, 8, TODAY) == date(2026, 11, 8)  # «Domingo 8»
    assert cine.day_near(0, 29, TODAY) is None  # no Monday 29 near 1 Nov 2026


def test_normandie_without_days_is_an_error():
    with pytest.raises(ValueError, match="Normandie"):
        cine.parse_normandie("<p>Estamos renovando la sala</p>", TODAY)


def test_ceina_reads_each_dated_event():
    shows = cine.parse_ceina((FIXTURES / "cine_ceina.html").read_text("utf-8"), TODAY)
    assert len(shows) == 18  # 11 events with a day; the two dated «2026» have none
    assert shows[0] == cine.Show(
        date(2026, 10, 31), "20:00", "ESTO ES HALLOWEEN \N{EN DASH} KOKORONE MUSIC", "Música",
        "https://ticketplus.cl/events/esto-es-halloween-vol-ii-el-concierto-que-estabas-esperando",
    )  # fmt: skip
    vinculos = [s for s in shows if s.title.startswith("VÍNCULO(S)")]
    assert [s.day.day for s in vinculos] == [5, 6, 7, 8]
    assert {(s.time, s.note) for s in vinculos} == {(None, "Danza - Performance")}
    assert not any("Candlelight" in s.title or "Viejas" in s.title for s in shows)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("31 de Octubre - 20:00", [(date(2026, 10, 31), "20:00")]),
        ("05 al 08 noviembre", [(date(2026, 11, d), None) for d in (5, 6, 7, 8)]),
        ("6 y 7 noviembre", [(date(2026, 11, 6), None), (date(2026, 11, 7), None)]),
        (
            "13 noviembre - 20:00h | 14 noviembre - 21:00 h.",
            [(date(2026, 11, 13), "20:00"), (date(2026, 11, 14), "21:00")],
        ),
        ("21 y 22 de noviembre", [(date(2026, 11, 21), None), (date(2026, 11, 22), None)]),
        ("8 enero 2027 - 18:00", [(date(2027, 1, 8), "18:00")]),
        (
            "18 y 19 enero 2027 - 19:00",
            [(date(2027, 1, 18), "19:00"), (date(2027, 1, 19), "19:00")],
        ),
        ("24 de enero - 20:00", [(date(2027, 1, 24), "20:00")]),  # no year: the next January
        ("2026", []),
        ("30 de octubre al 2 de noviembre", []),  # across two months: not understood, skipped
        ("31 de febrero", []),
    ],
)
def test_ceina_dates(text, expected):
    assert cine.parse_when(text, TODAY) == expected


def test_ceina_without_events_is_an_error():
    with pytest.raises(ValueError, match="CEINA"):
        cine.parse_ceina("<html><body>Mantención</body></html>", TODAY)


def test_encartelera_keeps_one_cinema():
    page = (FIXTURES / "cine_encartelera.html").read_text("utf-8")
    name, shows = cine.parse_encartelera(page, "alameda")
    assert name == "Centro Arte Alameda"
    monday = sorted((s.time, s.title, s.note) for s in shows if s.day == date(2026, 11, 2))
    assert monday == [
        ("16:30", "Hangar Rojo", None),
        ("18:30", "Post Mortem", "Sanfic 22"),
        ("20:30", "Narciso", "Sanfic 22"),
    ]
    assert all(s.url and s.url.startswith("https://") for s in shows)
    assert cine.parse_encartelera(page, "nadie") == (None, [])


def test_encartelera_without_movies_is_an_error():
    with pytest.raises(ValueError, match="encartelera"):
        cine.parse_encartelera("<html><body>Vercel</body></html>", "alameda")


def test_readers_fetch_their_page_and_name_the_venue():
    http = site()
    names = [cine.read(http, v, TODAY)[0] for v in ("normandie", "alameda", "ceina")]
    assert names == ["Normandie", "Centro Arte Alameda · sala CEINA", "CEINA · escenario"]
    assert cine.read(http, "cineteca", TODAY) == (None, [])  # not in the fixture: no name
    with pytest.raises(httpx.HTTPStatusError):
        cine.read(site(fail={cine.NORMANDIE_URL}), "normandie", TODAY)


def test_normandie_a_heading_that_is_no_day_closes_the_day():
    # Review: «Otras funciones» or «Jueves 5 de noviembre» left B and C on Wednesday the 4th.
    content = (
        '<h5>Miércoles 4</h5>15:00 hrs.<br><a href="https://x.test/a">A</a>'
        '<h5>Jueves 5 de noviembre</h5>18:00 hrs.<br><a href="https://x.test/b">B</a>'
        '<h4>Otras funciones</h4>20:00 hrs.<br><a href="https://x.test/c">C</a>'
    )
    shows = cine.parse_normandie(content, TODAY)
    assert [(s.day, s.title) for s in shows] == [(date(2026, 11, 4), "A")]


def test_normandie_days_without_a_single_show_is_an_error():
    # «15.00 hrs.»: a new way of writing the hours must keep the old shows, not empty the board.
    with pytest.raises(ValueError, match="ninguna función"):
        cine.parse_normandie(
            '<h5>Lunes 2</h5>15.00 hrs.<br><a href="https://x.test/a">A</a>', TODAY
        )


def test_links_are_web_addresses_and_nothing_else():
    assert cine.web_url("https://x.test/a?b=1") == "https://x.test/a?b=1"
    for bad in ("javascript:alert(1)", "https://x\x00y", "https://x y", "ftp://x.test", None):
        assert cine.web_url(bad) is None  # a NUL made os.startfile crash the TUI


def test_a_page_too_big_or_too_slow_is_refused(monkeypatch):
    big = httpx.Response(200, content=b"<article>" + b"x" * (513 * 1024))
    http = httpx.Client(transport=httpx.MockTransport(lambda request: big))
    with pytest.raises(ValueError, match="512 KB"):
        cine.read(http, "ceina", TODAY)
    monkeypatch.setattr(cine, "READ_FOR", -1)  # the deadline already passed
    with pytest.raises(TimeoutError):
        cine.read(site(), "ceina", TODAY)


NOW = datetime(2026, 11, 1, 12, 0)  # naive UTC: 09:00 of 1 Nov in Santiago, like TODAY


@pytest.fixture
def cine_config(config):
    return config.model_copy(update={"cine": CineConfig(salas=["normandie", "ceina"])})


def count(s: Session, venue: str) -> int:
    return s.scalar(select(func.count()).select_from(Screening).where(Screening.venue == venue))


def test_refresh_stores_each_venue(engine, cine_config):
    cine.refresh_cine(engine, cine_config, site(), NOW)
    with Session(engine) as s:
        assert (count(s, "normandie"), count(s, "ceina")) == (28, 18)
        normandie = s.get(CinemaVenue, "normandie")
        assert (normandie.name, normandie.ok_at, normandie.error) == ("Normandie", NOW, None)
        assert s.get(CinemaVenue, "ceina").name == "CEINA · escenario"


def test_a_working_reader_replaces_its_shows(engine, cine_config):
    cine.refresh_cine(engine, cine_config, site(), NOW)
    with Session(engine) as s, s.begin():
        s.add(Screening(venue="normandie", day=TODAY, time="23:00", title="Ya no está"))
    cine.refresh_cine(engine, cine_config, site(), NOW + timedelta(hours=4))
    with Session(engine) as s:
        assert count(s, "normandie") == 28
        assert s.scalar(select(Screening).where(Screening.title == "Ya no está")) is None


def test_a_failing_reader_keeps_its_shows_and_records_why(engine, cine_config):
    cine.refresh_cine(engine, cine_config, site(), NOW)
    later = NOW + timedelta(hours=4)
    cine.refresh_cine(engine, cine_config, site(fail={cine.CEINA_URL}), later)
    with Session(engine) as s:
        assert count(s, "ceina") == 18
        ceina = s.get(CinemaVenue, "ceina")
        assert (ceina.ok_at, ceina.checked_at) == (NOW, later)
        assert "500" in ceina.error
        assert s.get(CinemaVenue, "normandie").ok_at == later


def test_refresh_waits_three_hours_unless_a_venue_is_new(engine, cine_config):
    seen = []
    cine.refresh_cine(engine, cine_config, site(seen=seen), NOW)
    assert len(seen) == 2
    cine.refresh_cine(engine, cine_config, site(seen=seen), NOW + timedelta(hours=2, minutes=59))
    assert len(seen) == 2
    more = cine_config.model_copy(
        update={"cine": CineConfig(salas=["normandie", "ceina", "alameda"])}
    )
    cine.refresh_cine(engine, more, site(seen=seen), NOW + timedelta(hours=1))  # alameda is new
    assert len(seen) == 5
    cine.refresh_cine(engine, more, site(seen=seen), NOW + timedelta(hours=4, minutes=1))
    assert len(seen) == 8


def test_refresh_never_raises_and_does_nothing_without_cine(engine, config, cine_config):
    seen = []
    cine.refresh_cine(engine, config, site(seen=seen), NOW)
    assert seen == []

    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("sin red", request=request)

    cine.refresh_cine(engine, cine_config, httpx.Client(transport=httpx.MockTransport(down)), NOW)
    with Session(engine) as s:
        assert "sin red" in s.get(CinemaVenue, "normandie").error
        assert s.get(CinemaVenue, "normandie").ok_at is None


def test_a_time_that_is_not_one_is_stored_without_it(engine, cine_config):
    # A hand-written «24:00 hrs.» would break /cine and the whole copy without the PC.
    content = (
        '<h5>Lunes 2</h5>24:00 hrs.<br><a href="https://x.test/a">Medianoche</a>'
        '<hr>15:00 hrs.<br><a href="https://x.test/b">Nine</a>'
    )
    page = json.dumps([{"content": {"rendered": content}}]).encode()

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == cine.NORMANDIE_URL:
            return httpx.Response(200, content=page)
        return httpx.Response(200, content=(FIXTURES / "cine_ceina.html").read_bytes())

    http = httpx.Client(transport=httpx.MockTransport(handler))
    cine.refresh_cine(engine, cine_config, http, NOW)
    with Session(engine) as s:
        shows = s.scalars(select(Screening).where(Screening.venue == "normandie")).all()
        assert sorted((x.title, x.time) for x in shows) == [("Medianoche", None), ("Nine", "15:00")]


def test_the_attempt_is_recorded_before_reading(engine, cine_config):
    # Killed mid-read (the task's time limit), a run must wait its 3 hours, and say so.
    during = []

    def handler(request: httpx.Request) -> httpx.Response:
        with Session(engine) as s:
            row = s.get(CinemaVenue, "normandie")
            during.append((row.checked_at, row.error))
        return httpx.Response(200, content=(FIXTURES / PAGES[str(request.url)]).read_bytes())

    cine.refresh_cine(
        engine, cine_config, httpx.Client(transport=httpx.MockTransport(handler)), NOW
    )
    assert during[0] == (NOW, "La lectura no terminó.")
    with Session(engine) as s:
        assert s.get(CinemaVenue, "normandie").error is None


def test_encartelera_is_read_once_for_all_its_cinemas(engine, config):
    seen = []
    both = config.model_copy(update={"cine": CineConfig(salas=["alameda", "biografo"])})
    cine.refresh_cine(engine, both, site(seen=seen), NOW)
    assert seen == [cine.ENCARTELERA_URL]


def test_a_read_without_shows_keeps_the_name_it_had(engine, config):
    cineuc = config.model_copy(update={"cine": CineConfig(salas=["cineuc"])})
    with Session(engine) as s, s.begin():
        s.add(CinemaVenue(venue="cineuc", name="Cine UC", checked_at=NOW))
    cine.refresh_cine(engine, cineuc, site(), NOW + timedelta(hours=4))  # none in the fixture
    with Session(engine) as s:
        assert s.get(CinemaVenue, "cineuc").name == "Cine UC"


def test_control_characters_never_reach_the_base(engine, cine_config):
    # An ESC in a title or an error would reach the terminal (TUI, status): OSC 52, clear screen.
    content = '<h5>Lunes 2</h5>15:00 hrs.<br><a href="https://x.test/a">Nine&#27;[2J</a>'
    page = json.dumps([{"content": {"rendered": content}}]).encode()

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == cine.NORMANDIE_URL:
            return httpx.Response(200, content=page)
        return httpx.Response(404)  # httpx's message has a second line, a help link

    cine.refresh_cine(
        engine, cine_config, httpx.Client(transport=httpx.MockTransport(handler)), NOW
    )
    with Session(engine) as s:
        assert s.scalar(select(Screening.title)) == "Nine[2J"
        error = s.get(CinemaVenue, "ceina").error
        assert "404" in error and "\n" not in error


def test_ingest_reads_the_cinemas_even_when_the_feeds_fail(engine, cine_config):
    # The feeds are down (404 in `site`): ingest raises, but the cinemas were read.
    with pytest.raises(RuntimeError, match="Ninguna fuente"):
        ingest(engine, cine_config, site(fail={cine.CEINA_URL}))
    with Session(engine) as s:
        assert s.get(CinemaVenue, "normandie").error is None
        assert "500" in s.get(CinemaVenue, "ceina").error


def test_the_board_lists_each_venue_in_order_timed_shows_first(engine):
    day = date(2026, 11, 2)
    with Session(engine) as s, s.begin():
        s.add_all(
            [
                Screening(venue="ceina", day=day, time=None, title="Vínculo(s)"),
                Screening(venue="normandie", day=day, time="20:00", title="El odio"),
                Screening(venue="normandie", day=day, time="15:00", title="Nine"),
                Screening(
                    venue="normandie", day=day + timedelta(days=1), time="15:00", title="Otro día"
                ),
                CinemaVenue(venue="normandie", name="Normandie", checked_at=NOW, ok_at=NOW),
            ]
        )
    with Session(engine) as s:
        board = queries.cinema_board(s, ["normandie", "alameda", "ceina"], day)
        assert [(b.venue, b.name) for b in board] == [
            ("normandie", "Normandie"),
            ("alameda", "Centro Arte Alameda · sala CEINA"),  # never read: the fixed name
            ("ceina", "CEINA · escenario"),
        ]
        assert [x.title for x in board[0].shows] == ["Nine", "El odio"]
        assert (board[1].shows, board[1].status) == ([], None)
        assert [x.title for x in board[2].shows] == ["Vínculo(s)"]
