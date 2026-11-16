"""Full public text of an article page, when the page allows it, and the page's photo."""

from __future__ import annotations

from time import monotonic
from typing import NamedTuple
from urllib.parse import urljoin

import httpx
import trafilatura

from .rss import read_capped, web_url, within

MIN_CHARS = 200  # shorter results are usually paywall teasers or cookie walls
EXTRACT_SECONDS = 300  # every page and its retries: ~6 s for 60 on a normal day


class Page(NamedTuple):
    text: str | None  # None: too short or unreadable; the caller falls back to the RSS
    image: str | None  # its og:image (#18), absolute http(s)


def extract_page(client: httpx.Client, url: str) -> Page:
    # One bad page must never stop the edition: the caller falls back to the RSS.
    try:
        body, response = read_capped(client, url)
        html = body.decode(response.encoding or "utf-8", errors="replace")  # = response.text
        text = trafilatura.extract(html, url=url, include_comments=False, include_tables=False)
    except Exception:
        return Page(None, None)
    try:  # relative to where the page ended up, after any redirect
        image = trafilatura.extract_metadata(html, default_url=url).image
        image = web_url(urljoin(str(response.url), image)) if image else None
    except Exception:
        image = None
    return Page(text if text and len(text) >= MIN_CHARS else None, image)


def extract_many(client: httpx.Client, urls: list[str], workers: int = 8) -> dict[str, Page]:
    """url -> Page within EXTRACT_SECONDS; one not done by then falls back to the RSS too."""
    deadline = monotonic() + EXTRACT_SECONDS

    def read(url: str) -> Page:
        return extract_page(client, url)

    done = within({u: u for u in urls}, read, workers=workers, seconds=EXTRACT_SECONDS)
    pages = {u: done.get(u, Page(None, None)) for u in urls}
    if len(done) < len(urls):  # one held the clock to its end: no time is left for retries
        return pages
    # A busy site drops some parallel requests (CIPER lost 3 of 5 with 144 at once, 2026-10-14):
    # retry those one at a time, in what's left of the clock.
    # ponytail: a paywalled page fails twice, a second or two each.
    # A paywall's first read may have had the photo: a retry without one keeps it.
    failed = {u: u for u, page in pages.items() if page.text is None}
    for url, retry in within(failed, read, workers=1, seconds=deadline - monotonic()).items():
        pages[url] = Page(retry.text, retry.image or pages[url].image)
    return pages
