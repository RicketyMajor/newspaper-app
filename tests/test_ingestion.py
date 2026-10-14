from datetime import datetime
from pathlib import Path

import httpx
import pytest

from newspaper.config.models import FeedConfig
from newspaper.ingestion.extract import extract_many, extract_text
from newspaper.ingestion.rss import fetch_feed, parse_feed

FIXTURES = Path(__file__).parent / "fixtures"
CIPER = FeedConfig(name="CIPER", url="https://ciper.test/feed", section="Chile")
DW = FeedConfig(name="DW Español", url="https://dw.test/rdf", section="Mundo")


def client_for(routes: dict[str, httpx.Response]) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        return routes.get(str(request.url), httpx.Response(404))

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_parse_rss2_cleans_html_converts_dates_and_skips_untitled():
    articles = parse_feed((FIXTURES / "rss2.xml").read_bytes(), CIPER)

    assert [a.url for a in articles] == [
        "https://ciper.test/pensiones",
        "https://ciper.test/atacama",
    ]
    first = articles[0]
    assert first.summary == "La votación cerró anoche & sin sorpresas."
    assert first.published_at == datetime(2026, 10, 10, 14, 30)  # naive UTC
    assert (first.source_name, first.section, first.language) == ("CIPER", "Chile", "es")
    assert articles[1].published_at is None


def test_parse_rdf():
    [article] = parse_feed((FIXTURES / "rdf.xml").read_bytes(), DW)
    assert article.title == "Cumbre climática cierra sin acuerdo"
    assert article.published_at == datetime(2026, 10, 10, 9, 0)


def test_parse_garbage_raises():
    with pytest.raises(ValueError):
        parse_feed(b"<html>not a feed</html>", CIPER)


def test_fetch_feed_raises_on_http_error():
    client = client_for({CIPER.url: httpx.Response(500)})
    with pytest.raises(httpx.HTTPStatusError):
        fetch_feed(client, CIPER)


def test_fetch_feed_parses_response():
    client = client_for(
        {CIPER.url: httpx.Response(200, content=(FIXTURES / "rss2.xml").read_bytes())}
    )
    assert len(fetch_feed(client, CIPER)) == 2


def test_extract_text_gets_article_body_and_drops_chrome():
    url = "https://ciper.test/pensiones"
    html = (FIXTURES / "article.html").read_text(encoding="utf-8")
    client = client_for({url: httpx.Response(200, html=html)})

    text = extract_text(client, url)

    assert text is not None
    assert "30 votos a favor" in text
    assert "boletín" not in text


def test_extract_returns_none_on_errors_and_short_pages():
    client = client_for({"https://x.test/short": httpx.Response(200, html="<p>Hola</p>")})
    assert extract_many(client, ["https://x.test/missing", "https://x.test/short"]) == {
        "https://x.test/missing": None,
        "https://x.test/short": None,
    }


def test_extract_many_retries_failed_pages_one_at_a_time():
    # A busy site drops some of the parallel requests (CIPER lost 3 of 5 with 144 at once).
    html = (FIXTURES / "article.html").read_text(encoding="utf-8")
    calls = []

    def handler(request):
        calls.append(str(request.url))
        first_try = calls.count(str(request.url)) == 1
        return httpx.Response(503) if first_try else httpx.Response(200, html=html)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    texts = extract_many(client, ["https://ciper.test/a", "https://ciper.test/b"])

    assert all(texts.values())
    assert len(calls) == 4


def test_parse_keeps_only_web_links():
    # os.startfile (behind webbrowser.open on Windows) would run a file path or protocol handler.
    links = ["file:///C:/x.exe", r"\\evil\share\x.exe", "ms-msdt:id", "https://ok.test/a"]
    links.append("http://[")  # malformed: must not abort the whole feed
    items = "".join(
        f"<item><title>T{i}</title><link>{link}</link></item>" for i, link in enumerate(links)
    )
    feed = f'<?xml version="1.0"?><rss version="2.0"><channel>{items}</channel></rss>'
    urls = [a.url for a in parse_feed(feed.encode(), CIPER)]
    assert urls[0] == "https://ok.test/a"
    assert all(u.startswith(("http://", "https://")) for u in urls)
