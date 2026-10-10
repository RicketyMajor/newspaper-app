"""Full public text of an article page, when the page allows it."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import httpx
import trafilatura

MIN_CHARS = 200  # shorter results are usually paywall teasers or cookie walls


def extract_text(client: httpx.Client, url: str) -> str | None:
    try:
        response = client.get(url)
        response.raise_for_status()
        text = trafilatura.extract(
            response.text, url=url, include_comments=False, include_tables=False
        )
    except Exception:  # one bad page must never stop the edition; caller falls back to RSS
        return None
    return text if text and len(text) >= MIN_CHARS else None


def extract_many(client: httpx.Client, urls: list[str], workers: int = 8) -> dict[str, str | None]:
    with ThreadPoolExecutor(workers) as pool:
        return dict(zip(urls, pool.map(lambda url: extract_text(client, url), urls), strict=True))
