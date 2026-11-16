"""RSS 2.0 / Atom / RDF feeds -> RawArticle."""

from __future__ import annotations

import calendar
import html
import ipaddress
import re
import socket
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from time import monotonic

import feedparser
import httpx

from ..config.models import FeedConfig

USER_AGENT = (
    "Mozilla/5.0 (compatible; Newspaper/0.1; +https://github.com/RicketyMajor/newspaper-app)"
)
# The client's timeout is per read: a host sending a byte every 19 s beats it for ever.
READ_SECONDS, READ_BYTES = 30, 10_000_000


@dataclass
class RawArticle:
    title: str
    url: str
    summary: str
    source_name: str
    section: str
    language: str
    published_at: datetime | None  # naive UTC
    image_url: str | None = None  # the feed's photo for it (#18)


class BlockedURL(httpx.RequestError):
    """A request to a scheme other than http(s), or to a host that isn't on the internet."""


def _resolve(host: str) -> list[str]:
    return [info[4][0] for info in socket.getaddrinfo(host, None)]


def _public_only(request: httpx.Request) -> None:
    """Refuse a host with any non-global address: a feed, an article link or a redirect must
    not reach this PC's own services (the web reader, Ollama) or the LAN. httpx calls it before
    every request, each redirect included.
    ponytail: httpx resolves the name again to connect; DNS rebinding in between gets through."""
    if request.url.scheme not in ("http", "https"):
        raise BlockedURL(f"esquema no permitido: {request.url.scheme}", request=request)
    host = request.url.raw_host.decode("ascii")  # IDNA, no brackets: the name httpx connects to
    try:
        addresses = [ipaddress.ip_address(host)]
    except ValueError:
        try:
            addresses = [ipaddress.ip_address(a) for a in _resolve(host)]
        except OSError:  # it could answer 127.0.0.1 when httpx resolves it again to connect
            raise BlockedURL(f"{host} no se pudo resolver", request=request) from None
    if not addresses or not all(a.is_global for a in addresses):
        raise BlockedURL(f"{host} no es una dirección pública", request=request)


def make_client(timeout: float = 20, transport: httpx.BaseTransport | None = None) -> httpx.Client:
    return httpx.Client(
        timeout=timeout,
        follow_redirects=True,
        headers={"User-Agent": USER_AGENT},
        event_hooks={"request": [_public_only]},
        transport=transport,  # tests only
    )


def read_capped(
    client: httpx.Client, url: str, *, seconds: float = READ_SECONDS, max_bytes: int = READ_BYTES
) -> tuple[bytes, httpx.Response]:
    """(body, response) of a GET, within `seconds` and `max_bytes` of body, else TimeoutError or
    ValueError. The response gives the URL after redirects and the encoding. ponytail: the bytes
    counted are decoded ones (a gzip bomb inflates one chunk before it's seen, as in cine.py);
    the threat is a dripping host, not a known outlet's bomb. Headers dripping: within()."""
    deadline, body = monotonic() + seconds, bytearray()
    with client.stream("GET", url) as response:
        response.raise_for_status()
        for chunk in response.iter_bytes():
            body += chunk
            if len(body) > max_bytes:
                raise ValueError(f"pasa de {max_bytes // 1_000_000} MB")
            if monotonic() > deadline:
                raise TimeoutError(f"tardó más de {seconds} s")
    return bytes(body), response


def within[K, A, R](
    items: Mapping[K, A], fn: Callable[[A], R], *, workers: int, seconds: float
) -> dict[K, R]:
    """key -> fn(value), `workers` at a time, for those done within `seconds`. A host dripping
    its headers beats every per-read timeout, so the batch has its own clock; its threads are
    daemons, so one still stuck never holds the process at exit. `fn` mustn't raise: one that
    does counts as not done. One starting after the clock doesn't run."""
    deadline = monotonic() + seconds
    gate, lock, done = threading.Semaphore(workers), threading.Lock(), {}

    def run(key: K, value: A) -> None:
        with gate:
            if monotonic() < deadline:
                result = fn(value)
                with lock:
                    done[key] = result

    threads = [threading.Thread(target=run, args=item, daemon=True) for item in items.items()]
    for t in threads:
        t.start()
    for t in threads:
        t.join(max(0, deadline - monotonic()))
    with lock:
        return dict(done)


# Control characters: an ESC in a title would reach the terminal (the TUI, `status`).
CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def clean_html(value: str) -> str:
    """An HTML fragment as one line of plain text, without control characters."""
    return " ".join(CONTROL.sub(" ", html.unescape(re.sub(r"<[^>]+>", " ", value))).split())


def web_url(url: str | None) -> str | None:
    """`url` if it is an absolute http(s) URL, else None."""
    url = (url or "").strip()
    return url if url.lower().startswith(("http://", "https://")) else None


def _entry_image(entry) -> str | None:
    """The entry's photo: Media RSS (an image, else a thumbnail), else an image enclosure."""
    candidates = [
        m.get("url")
        for m in entry.get("media_content", [])
        if m.get("medium") == "image" or (m.get("type") or "").startswith("image/")
    ]
    candidates += [m.get("url") for m in entry.get("media_thumbnail", [])]
    candidates += [
        e.get("href")
        for e in entry.get("enclosures", [])
        if (e.get("type") or "").startswith("image/")
    ]
    return next(filter(None, map(web_url, candidates)), None)


def parse_feed(data: bytes, feed: FeedConfig) -> list[RawArticle]:
    parsed = feedparser.parse(data)
    if not parsed.entries:
        reason = parsed.get("bozo_exception") or "no entries"
        raise ValueError(f"{feed.name}: el feed no se puede leer ({reason})")

    articles = []
    for entry in parsed.entries:
        title, url = clean_html(entry.get("title", "")), entry.get("link")
        # Only web links: readers open them with webbrowser.open, which on Windows is
        # os.startfile and would run a file path or protocol handler from a hostile feed.
        if not title or not url or not url.lower().startswith(("http://", "https://")):
            continue
        # Rules run here, the only place that knows the article's feed (names can repeat).
        # ponytail: not retroactive; articles already stored keep their section.
        section = feed.section_for(title, url)
        if section is None:
            continue
        stamp = entry.get("published_parsed") or entry.get("updated_parsed")  # UTC struct_time
        published = (
            datetime.fromtimestamp(calendar.timegm(stamp), UTC).replace(tzinfo=None)
            if stamp
            else None
        )
        articles.append(
            RawArticle(
                title=title,
                url=url,
                summary=clean_html(entry.get("summary", "")),
                source_name=feed.name,
                section=section,
                language=feed.language,
                published_at=published,
                image_url=_entry_image(entry),
            )
        )
    return articles


def fetch_feed(client: httpx.Client, feed: FeedConfig) -> list[RawArticle]:
    body, _ = read_capped(client, feed.url)
    return parse_feed(body, feed)
