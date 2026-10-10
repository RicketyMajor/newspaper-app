import os
import subprocess
import sys
from datetime import UTC, date, datetime
from email.utils import format_datetime
from pathlib import Path

import httpx
import pytest
from sqlalchemy.orm import Session

from newspaper.ai.ollama import Draft, OllamaError, OllamaUnavailableError
from newspaper.edition.generate import generate, pid_alive
from newspaper.storage import queries
from newspaper.storage.models import Edition, EditionStatus

FIXTURES = Path(__file__).parent / "fixtures"
NOW = datetime(2026, 10, 10, 15, 0, tzinfo=UTC)  # 12:00 in Santiago
TODAY = date(2026, 10, 10)

SENADO = ("Senado aprueba reforma de pensiones", "https://ciper.test/senado", "Votación cerrada.")
ATACAMA = ("Hallan nueva especie en Atacama", "https://ciper.test/atacama", "Un lagarto.")
SENADO_BBC = ("Senado chileno aprueba la reforma", "https://bbc.test/senado", "Tras meses.")
VECTORS = {SENADO[0]: [1.0, 0.0], SENADO_BBC[0]: [0.99, 0.05], ATACAMA[0]: [0.0, 1.0]}


def rss(*items) -> bytes:
    stamp = format_datetime(datetime(2026, 10, 10, 11, 0, tzinfo=UTC))
    body = "".join(
        f"<item><title>{t}</title><link>{u}</link><description>{d}</description>"
        f"<pubDate>{stamp}</pubDate></item>"
        for t, u, d in items
    )
    return f'<?xml version="1.0"?><rss version="2.0"><channel>{body}</channel></rss>'.encode()


def http(feeds_ok: bool = True) -> httpx.Client:
    pages = {
        "https://ciper.test/feed": rss(SENADO, ATACAMA),
        "https://bbc.test/rss": rss(SENADO_BBC),
        SENADO[1]: (FIXTURES / "article.html").read_bytes(),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith(("/feed", "/rss")) and not feeds_ok:
            return httpx.Response(500)
        return httpx.Response(200, content=pages[url]) if url in pages else httpx.Response(404)

    return httpx.Client(transport=httpx.MockTransport(handler))


class FakeLLM:
    def __init__(self, fail_titles=(), down=False):
        self.fail_titles, self.down = set(fail_titles), down

    def embed(self, texts):
        if self.down:
            raise OllamaUnavailableError("Ollama no responde")
        return [VECTORS[t.split("\n")[0]] for t in texts]

    def draft(self, materials, language):
        titles = [title for _, title, _ in materials]
        if self.fail_titles & set(titles):
            raise OllamaError("draft: JSON inválido")
        outlets = ", ".join(outlet for outlet, _, _ in materials)
        return Draft(f"Nota: {titles[0]}", "Bajada", f"Cuerpo según {outlets}.", "Difieren.")


def run(config, llm=None, **kwargs):
    return generate(config, http=http(), llm=llm or FakeLLM(), now=NOW, **kwargs)


def test_generate_publishes_a_ready_edition(config, engine):
    result = run(config)

    assert (result.status, result.edition_date) == ("ready", TODAY)
    with Session(engine) as s:
        edition = queries.latest_ready_edition(s)
        assert edition.edition_date == TODAY
        senado, atacama = queries.front_page(s, edition.id)
        assert senado.headline == f"Nota: {SENADO[0]}"
        assert {a.source_name for a in senado.articles} == {"CIPER", "BBC Mundo"}
        assert (senado.single_source, senado.differences, senado.drafted) == (
            False,
            "Difieren.",
            True,
        )
        assert senado.excerpt_only is False  # the CIPER page was extracted in full
        assert (atacama.single_source, atacama.differences, atacama.excerpt_only) == (
            True,
            None,
            True,
        )
        assert senado.read_minutes >= 1
        assert [h.id for h in queries.search(s, "lagarto")] == [atacama.articles[0].id]
        assert queries.search(s, "Nota Senado")[0].id == senado.id


def test_failed_draft_publishes_excerpts(config, engine):
    assert run(config, FakeLLM(fail_titles={ATACAMA[0]})).status == "ready"
    with Session(engine) as s:
        atacama = queries.front_page(s, queries.latest_ready_edition(s).id)[1]
        assert (atacama.drafted, atacama.headline) == (False, ATACAMA[0])
        assert "Un lagarto." in atacama.body


def test_ollama_down_fails_and_keeps_previous_edition(config, engine, seeded):
    result = generate(
        config, http=http(), llm=FakeLLM(down=True), now=datetime(2026, 10, 11, 15, tzinfo=UTC)
    )

    assert result.status == "failed"
    assert "Ollama" in result.message
    with Session(engine) as s:
        assert queries.latest_ready_edition(s).id == seeded["edition"]
        failed = queries.today_edition(s, date(2026, 10, 11))
        assert (failed.status, failed.error) == (EditionStatus.failed, result.message)


def test_all_feeds_down_fails(config):
    result = generate(config, http=http(feeds_ok=False), llm=FakeLLM(), now=NOW)
    assert result.status == "failed"
    assert "CIPER" in result.message and "BBC Mundo" in result.message


def add_today(engine, **fields):
    with Session(engine) as s, s.begin():
        s.add(Edition(edition_date=TODAY, **fields))


def test_running_generation_is_not_duplicated(config, engine):
    add_today(engine, status=EditionStatus.generating, pid=os.getpid())
    assert run(config).status == "skipped"


def dead_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def test_pid_alive():
    assert pid_alive(os.getpid())
    assert not pid_alive(dead_pid())


def test_crashed_or_failed_generation_is_taken_over(config, engine):
    add_today(engine, status=EditionStatus.generating, pid=dead_pid())
    assert run(config).status == "ready"


def test_ready_edition_is_kept_unless_forced(config, engine):
    assert run(config).status == "ready"
    assert run(config).status == "skipped"
    assert run(config, force=True).status == "ready"
    with Session(engine) as s:
        [edition] = queries.list_editions(s)
        assert len(edition.notes) == 2
        assert len(queries.search(s, "lagarto")) == 1


def test_published_articles_are_not_repeated_next_day(config, engine):
    assert run(config).status == "ready"
    next_day = generate(
        config, http=http(), llm=FakeLLM(), now=datetime(2026, 10, 11, 15, tzinfo=UTC)
    )
    assert next_day.status == "failed"
    assert "Sin artículos nuevos" in next_day.message


@pytest.mark.parametrize("hour", [2, 3])
def test_edition_date_uses_configured_timezone(config, hour):
    # Santiago is UTC-3 in October: 02:00 UTC on the 11th is still the 10th there.
    now = datetime(2026, 10, 11, hour, tzinfo=UTC)
    expected = date(2026, 10, 10) if hour == 2 else date(2026, 10, 11)
    assert generate(config, http=http(), llm=FakeLLM(), now=now).edition_date == expected
