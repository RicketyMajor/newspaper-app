"""Full public text of an article page, when the page allows it, and the page's photo."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import NamedTuple
from urllib.parse import urljoin

import httpx
import trafilatura

from .rss import web_url

MIN_CHARS = 200  # shorter results are usually paywall teasers or cookie walls


class Page(NamedTuple):
    text: str | None  # None: too short or unreadable; the caller falls back to the RSS
    image: str | None  # its og:image (#18), absolute http(s)


def extract_page(client: httpx.Client, url: str) -> Page:
    # One bad page must never stop the edition: the caller falls back to the RSS.
    try:
        response = client.get(url)
        response.raise_for_status()
        html = response.text
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
    with ThreadPoolExecutor(workers) as pool:
        pages = dict(zip(urls, pool.map(lambda url: extract_page(client, url), urls), strict=True))
    # A busy site drops some parallel requests (CIPER lost 3 of 5 with 144 at once, 2026-10-14):
    # retry those one at a time. ponytail: a paywalled page fails twice, a second or two each.
    # A paywall's first read may have had the photo: a retry without one keeps it.
    for url in [u for u, page in pages.items() if page.text is None]:
        retry = extract_page(client, url)
        pages[url] = Page(retry.text, retry.image or pages[url].image)
    return pages
