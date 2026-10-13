"""RSS 2.0 / Atom / RDF feeds -> RawArticle."""

from __future__ import annotations

import calendar
import html
import re
from dataclasses import dataclass
from datetime import UTC, datetime

import feedparser
import httpx

from ..config.models import FeedConfig

USER_AGENT = (
    "Mozilla/5.0 (compatible; Newspaper/0.1; +https://github.com/RicketyMajor/newspaper-app)"
)


@dataclass
class RawArticle:
    title: str
    url: str
    summary: str
    source_name: str
    section: str
    language: str
    published_at: datetime | None  # naive UTC


def make_client(timeout: float = 20) -> httpx.Client:
    return httpx.Client(timeout=timeout, follow_redirects=True, headers={"User-Agent": USER_AGENT})


def clean_html(value: str) -> str:
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", value)).split())


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
            )
        )
    return articles


def fetch_feed(client: httpx.Client, feed: FeedConfig) -> list[RawArticle]:
    response = client.get(feed.url)
    response.raise_for_status()
    return parse_feed(response.content, feed)
