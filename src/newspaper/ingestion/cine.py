"""Today's and tomorrow's showtimes of the configured cinemas (#29): one reader per source."""

from __future__ import annotations

import contextlib
import html
import json
import re
import unicodedata
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta

import httpx
from sqlalchemy import Engine, delete, select
from sqlalchemy.orm import Session

from ..config.models import CINEMA_NAMES, NewspaperConfig
from ..dates import DAYS, MONTHS
from ..edition.today import local_today
from ..storage.models import CinemaVenue, Screening, utcnow

NORMANDIE_URL = "https://normandie.cl/wp-json/wp/v2/pages?slug=cartelera&_fields=content"
CEINA_URL = "https://ceina.cl/cartelera/"
ENCARTELERA_URL = "https://www.encartelera.cl/"


@dataclass(frozen=True)
class Show:
    day: date
    time: str | None  # "HH:MM"; None when the venue gives only the day
    title: str
    note: str | None = None  # a presenter, a cycle or a category
    url: str | None = None  # tickets, else the event's page


Reader = Callable[[httpx.Client, date], tuple[str, list[Show]]]


def plain(text: str) -> str:
    """Lowercase without accents: «Sábado» → «sabado»."""
    decomposed = unicodedata.normalize("NFD", text.lower())
    return "".join(c for c in decomposed if not unicodedata.combining(c))


WEEKDAY = {plain(d): i for i, d in enumerate(DAYS)}
MONTH = {plain(m): i + 1 for i, m in enumerate(MONTHS)} | {"setiembre": 9}


def web_url(url: str | None) -> str | None:
    return url if url and url.startswith(("https://", "http://")) else None


def text_of(fragment: str) -> str:
    """An HTML fragment as one line of plain text."""
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", fragment)).split())


def get(http: httpx.Client, url: str) -> httpx.Response:
    response = http.get(url)
    response.raise_for_status()
    return response


# --- Normandie: its WordPress page, written by hand ------------------------------------------

DAY_HEADING = re.compile(r"(lunes|martes|miercoles|jueves|viernes|sabado|domingo)\s+(\d{1,2})")
HOUR = re.compile(r"(\d{1,2}):(\d{2})\s*hrs?\.?")
LINK = re.compile(r"\x00([^\x01]*)\x01(.*)")


def day_near(weekday: int, number: int, today: date) -> date | None:
    """The date numbered `number` that falls on `weekday`, from a week ago to four weeks ahead:
    the page names the day («Jueves 29»), not the month."""
    for offset in range(-7, 29):
        d = today + timedelta(days=offset)
        if d.day == number and d.weekday() == weekday:
            return d
    return None


def parse_normandie(content: str, today: date) -> list[Show]:
    """A day heading («Jueves 29») opens a day, «15:00 hrs.» a show, the next link closes it;
    loose text in between is the note («Alex Doll presenta»). The rest is ignored."""
    content = re.sub(r"<script.*?</script>", "", content, flags=re.S | re.I)
    content = re.sub(  # each link on a line of its own, its href kept
        r'<a\s[^>]*?href="([^"]*)"[^>]*>(.*?)</a>',
        lambda m: f"\n\x00{m.group(1)}\x01{text_of(m.group(2))}\n",
        content,
        flags=re.S | re.I,
    )
    content = re.sub(r"<(br|hr|/h\d|/p|/section|/div)\b[^>]*>", "\n", content, flags=re.I)
    shows, day, hour, note, headings = [], None, None, [], 0
    for raw in re.sub(r"<[^>]+>", "", content).split("\n"):
        line = " ".join(html.unescape(raw).split())
        if not line:
            continue
        link = LINK.fullmatch(line)
        if heading := DAY_HEADING.fullmatch(plain(line)):
            headings += 1
            day = day_near(WEEKDAY[heading.group(1)], int(heading.group(2)), today)
            hour = None
        elif hour_match := HOUR.fullmatch(line):
            hour, note = f"{int(hour_match.group(1)):02d}:{hour_match.group(2)}", []
        elif link and hour and day:
            url, title = link.groups()
            shows.append(
                Show(day, hour, title, " ".join(note) or None, web_url(html.unescape(url)))
            )
            hour = None
        elif hour:
            note.append(line)
    if not headings:
        raise ValueError("Normandie: no encuentro los días de la cartelera")
    return shows


def normandie(http: httpx.Client, today: date) -> tuple[str, list[Show]]:
    pages = get(http, NORMANDIE_URL).json()
    if not pages:
        raise ValueError("Normandie: no encuentro la página de la cartelera")
    return CINEMA_NAMES["normandie"], parse_normandie(pages[0]["content"]["rendered"], today)


# --- CEINA: the stage listings (theatre, music, dance) ----------------------------------------

CARD = re.compile(r'<article class="bde-loop-item[^"]*">(.*?)</article>', re.S)
ANCHOR = re.compile(r"<a\s([^>]*)>(.*?)</a>", re.S)
WHEN = re.compile(
    r"(\d{1,2})(?:\s*(al|y)\s*(\d{1,2}))?\s+(?:de\s+)?([a-z]+)(?:\s+(?:de\s+)?(\d{4}))?"
    r"(?:\s*-\s*(\d{1,2}):(\d{2})\s*h?\.?)?"
)


def parse_when(text: str, today: date) -> list[tuple[date, str | None]]:
    """«05 al 08 noviembre», «13 noviembre - 20:00h | 14 noviembre - 21:00 h.»… as (day, time).
    Without a year, the nearest one ahead. What doesn't fit («2026») gives nothing."""
    found = []
    for part in plain(text).split("|"):
        m = WHEN.fullmatch(part.strip())
        if not m or m.group(4) not in MONTH:
            continue
        first, kind, second, month, year, hh, mm = m.groups()
        first, last = int(first), int(second or first)
        days = list(range(first, last + 1)) if kind == "al" else sorted({first, last})
        hour = f"{int(hh):02d}:{mm}" if hh else None
        try:
            start = date(int(year or today.year), MONTH[month], first)
            if not year and start < today - timedelta(days=90):
                start = start.replace(year=start.year + 1)
            found += [(start.replace(day=d), hour) for d in days]
        except ValueError:  # «31 de febrero», «30 al 2»
            continue
    return found


def parse_ceina(page: str, today: date) -> list[Show]:
    """Each card of the list: its heading, its category (the note), its date and its links
    (tickets, else «Quiero saber más»)."""
    cards = CARD.findall(page)
    if not cards:
        raise ValueError("CEINA: no encuentro los eventos de la cartelera")
    shows = []
    for card in cards:
        heading = re.search(r"<h\d[^>]*>(.*?)</h\d>", card, re.S)
        if heading is None:
            continue
        title, note, more, tickets = text_of(heading.group(1)), None, None, None
        for attrs, label in ANCHOR.findall(card):
            href = re.search(r'href="([^"]*)"', attrs)
            url = web_url(html.unescape(href.group(1))) if href else None
            if 'rel="tag"' in attrs:  # the category
                note = text_of(label) or None
            elif url and plain(text_of(label)).startswith("quiero saber"):
                more = url
            elif url:
                tickets = tickets or url
        # The date: the first line after the title that starts with a digit.
        lines = (text_of(t) for t in re.split(r"<(?:/div|br)\b[^>]*>", card[heading.end() :]))
        when = next((t for t in lines if t[:1].isdigit()), "")
        shows += [
            Show(day, hour, title, note, tickets or more) for day, hour in parse_when(when, today)
        ]
    return shows


def ceina(http: httpx.Client, today: date) -> tuple[str, list[Show]]:
    return CINEMA_NAMES["ceina"], parse_ceina(get(http, CEINA_URL).text, today)


# --- encartelera.cl: ~20 Santiago cinemas, the Arte Alameda among them -------------------------

PUSH = re.compile(r'self\.__next_f\.push\(\[1,"((?:[^"\\]|\\.)*)"\]\)')


def parse_encartelera(page: str, slug: str) -> tuple[str | None, list[Show]]:
    """The movies the home page carries in its Next.js payload, kept for one cinema; the name
    is the site's (None when that cinema has no shows)."""
    payload = "".join(json.loads(f'"{chunk}"') for chunk in PUSH.findall(page))
    at = payload.find('"movies":')
    if at < 0:
        raise ValueError("encartelera: no encuentro la lista de películas")
    movies, _ = json.JSONDecoder().raw_decode(payload, at + len('"movies":'))
    name, shows = None, []
    for movie in movies:
        for s in movie.get("screenings") or []:
            cinema = s.get("cinemas") or {}
            if not isinstance(cinema, dict) or cinema.get("slug") != slug:
                continue
            name = cinema.get("name") or name
            url = web_url(s.get("ticket_url")) or web_url(s.get("source_url"))
            day = date.fromisoformat(s["date"])
            hour = s["time"][:5] if s.get("time") else None
            shows.append(Show(day, hour, movie["title"], s.get("cycle") or None, url))
    return name, shows


def encartelera(slug: str) -> Reader:
    def read(http: httpx.Client, today: date) -> tuple[str, list[Show]]:
        name, shows = parse_encartelera(get(http, ENCARTELERA_URL).text, slug)
        return CINEMA_NAMES.get(slug) or name or slug, shows

    return read


def reader(venue: str) -> Reader:
    return {"normandie": normandie, "ceina": ceina}.get(venue) or encartelera(venue)


# --- Storing them ----------------------------------------------------------------------------

REFRESH_EVERY = timedelta(hours=3)  # polite with encartelera (~1.4 MB a read)


def _due(engine: Engine, venues: list[str], now: datetime) -> bool:
    with Session(engine) as s:
        checked = dict(
            s.execute(
                select(CinemaVenue.venue, CinemaVenue.checked_at).where(
                    CinemaVenue.venue.in_(venues)
                )
            ).all()
        )
    return len(checked) < len(venues) or now - min(checked.values()) >= REFRESH_EVERY


def _replace(engine: Engine, venue: str, name: str, shows: list[Show], now: datetime) -> None:
    with Session(engine) as s, s.begin():
        s.execute(delete(Screening).where(Screening.venue == venue))
        s.add_all(Screening(venue=venue, **asdict(show)) for show in shows)
        s.merge(CinemaVenue(venue=venue, name=name, checked_at=now, ok_at=now, error=None))


def _failed(engine: Engine, venue: str, error: str, now: datetime) -> None:
    with Session(engine) as s, s.begin():
        row = s.get(CinemaVenue, venue) or CinemaVenue(
            venue=venue, name=CINEMA_NAMES.get(venue, venue)
        )
        row.checked_at, row.error = now, error[:500]
        s.add(row)


def refresh_cine(
    engine: Engine, config: NewspaperConfig, http: httpx.Client, now: datetime | None = None
) -> None:
    """Read every configured cinema again when the oldest read is 3 h old, or one was never
    read. Never raises (it runs inside `ingest`): a failure keeps that cinema's shows and
    records why in cinema_venues."""
    venues = config.cine.salas
    now = now or utcnow()
    try:
        if not venues or not _due(engine, venues, now):
            return
    except Exception:  # a locked database: the next fetch tries again
        return
    today = local_today(config, now.replace(tzinfo=UTC))
    for venue in venues:
        try:
            name, shows = reader(venue)(http, today)
            _replace(engine, venue, name, shows, now)
        except Exception as e:
            with contextlib.suppress(Exception):  # the database itself may be what failed
                _failed(engine, venue, str(e) or type(e).__name__, now)
