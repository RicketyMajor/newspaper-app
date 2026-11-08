from datetime import datetime
from pathlib import Path

import feedparser
import httpx
import pytest

from newspaper.config.models import FeedConfig
from newspaper.ingestion import rss
from newspaper.ingestion.extract import Page, extract_many, extract_page
from newspaper.ingestion.rss import clean_html, fetch_feed, parse_feed

FIXTURES = Path(__file__).parent / "fixtures"
CIPER = FeedConfig(name="CIPER", url="https://ciper.test/feed", section="Chile")
DW = FeedConfig(name="DW Español", url="https://dw.test/rdf", section="Mundo")


def client_for(routes: dict[str, httpx.Response]) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        return routes.get(str(request.url), httpx.Response(404))

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_clean_html_drops_control_characters():
    # An ESC in a feed's title reached the terminal reader and `status` (OSC 52 to the
    # clipboard, clearing the screen): the review of #29 found it in the cinema data too.
    # Raw ones: html.unescape already drops «&#27;» (HTML5 rules) but leaves a raw ESC or CSI.
    assert clean_html("Nine\x1b[2J <b>de hoy</b>\x07\x9bfin") == "Nine [2J de hoy fin"
    assert clean_html("Línea 1<br>línea 2\t«ñandú»") == "Línea 1 línea 2 «ñandú»"


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


def test_extract_page_gets_article_body_and_its_photo_and_drops_chrome():
    url = "https://ciper.test/pensiones"
    html = (FIXTURES / "article.html").read_text(encoding="utf-8")
    client = client_for({url: httpx.Response(200, html=html)})

    page = extract_page(client, url)

    assert page.text is not None
    assert "30 votos a favor" in page.text
    assert "boletín" not in page.text
    assert page.image == "https://ciper.test/fotos/senado.jpg"  # og:image, made absolute


@pytest.mark.parametrize("image", ["data:image/png;base64,iVBORw0KGgo=", "javascript:alert(1)"])
def test_extract_page_keeps_only_web_photos(image):
    url = "https://ciper.test/pensiones"
    html = (FIXTURES / "article.html").read_text(encoding="utf-8")
    html = html.replace("/fotos/senado.jpg", image)
    assert extract_page(client_for({url: httpx.Response(200, html=html)}), url).image is None


def test_extract_returns_none_on_errors_and_short_pages():
    client = client_for({"https://x.test/short": httpx.Response(200, html="<p>Hola</p>")})
    assert extract_many(client, ["https://x.test/missing", "https://x.test/short"]) == {
        "https://x.test/missing": Page(None, None),
        "https://x.test/short": Page(None, None),
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
    pages = extract_many(client, ["https://ciper.test/a", "https://ciper.test/b"])

    assert all(page.text for page in pages.values())
    assert len(calls) == 4


def test_a_retry_that_fails_keeps_the_photo_of_the_first_try():
    # A paywall: the first read has the og:image but no text, and the retry gets a 503.
    teaser = '<head><meta property="og:image" content="https://img.test/a.jpg"></head><p>Hola</p>'
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(200, html=teaser) if len(calls) == 1 else httpx.Response(503)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    url = "https://ciper.test/a"
    assert extract_many(client, [url]) == {url: Page(None, "https://img.test/a.jpg")}


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


def test_entry_image_takes_the_feeds_photo_and_only_web_images():
    tags = [
        '<media:content url="https://img.test/a.jpg" medium="image"/>',
        '<media:thumbnail url="https://img.test/b.jpg"/>',
        '<enclosure url="https://img.test/c.jpg" type="image/jpeg" length="1"/>',
        '<enclosure url="https://audio.test/d.mp3" type="audio/mpeg" length="1"/>',
        '<media:content url="javascript:alert(1)" medium="image"/>',
        "",
    ]
    items = "".join(
        f"<item><title>T{i}</title><link>https://ok.test/{i}</link>{tag}</item>"
        for i, tag in enumerate(tags)
    )
    feed = (
        '<?xml version="1.0"?><rss version="2.0" xmlns:media="http://search.yahoo.com/mrss/">'
        f"<channel>{items}</channel></rss>"
    ).encode()
    expected = ["https://img.test/a.jpg", "https://img.test/b.jpg", "https://img.test/c.jpg"]
    expected += [None, None, None]
    assert [rss._entry_image(e) for e in feedparser.parse(feed).entries] == expected
    assert [a.image_url for a in parse_feed(feed, CIPER)] == expected


def test_client_refuses_private_hosts_and_redirects_to_them(monkeypatch):
    # A feed or an article link must not reach this PC's own services (the web reader,
    # Ollama) or the LAN, not even through a redirect from a public page.
    hosts = {"feed.example": ["93.184.216.34"], "evil.example": ["127.0.0.1"]}
    monkeypatch.setattr(rss, "_resolve", lambda host: hosts[host])

    def handler(request):
        if request.url.host == "feed.example":
            return httpx.Response(302, headers={"Location": "http://evil.example/x"})
        return httpx.Response(200, text="secret")

    client = rss.make_client(transport=httpx.MockTransport(handler))
    with pytest.raises(rss.BlockedURL):
        client.get("http://feed.example/rss")
    with pytest.raises(rss.BlockedURL):
        client.get("http://127.0.0.1:8765/")
    with pytest.raises(rss.BlockedURL):
        client.get("http://[::1]/")
    with pytest.raises(rss.BlockedURL):
        client.get("ftp://feed.example/rss")

    hosts["evil.example"] = ["93.184.216.34", "2606:4700::1"]  # now public: it goes through
    assert client.get("http://feed.example/rss").text == "secret"


def test_client_refuses_a_host_that_does_not_resolve(monkeypatch):
    # A name that fails at the check could answer 127.0.0.1 when httpx resolves it to connect.
    def no_such_host(host):
        raise OSError("getaddrinfo failed")

    monkeypatch.setattr(rss, "_resolve", no_such_host)
    client = rss.make_client(transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    with pytest.raises(rss.BlockedURL):
        client.get("https://nowhere.example/")
