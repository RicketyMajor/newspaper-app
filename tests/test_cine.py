import json
from datetime import date
from pathlib import Path

import httpx
import pytest

from newspaper.ingestion import cine

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
    names = [cine.reader(v)(http, TODAY)[0] for v in ("normandie", "alameda", "ceina")]
    assert names == ["Normandie", "Centro Arte Alameda · sala CEINA", "CEINA · escenario"]
    assert cine.reader("cineteca")(http, TODAY) == ("cineteca", [])  # not in the fixture
    with pytest.raises(httpx.HTTPStatusError):
        cine.reader("normandie")(site(fail={cine.NORMANDIE_URL}), TODAY)
