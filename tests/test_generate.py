import os
import subprocess
import sys
from datetime import UTC, date, datetime
from email.utils import format_datetime
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from newspaper.ai.ollama import NEUTRAL_TOPICS, Draft, OllamaError, OllamaUnavailableError
from newspaper.config.models import InterestsConfig
from newspaper.edition.generate import _capitalize, _draft, generate, ingest
from newspaper.edition.today import pid_alive
from newspaper.storage import queries
from newspaper.storage.models import Article, Edition, EditionStatus, Note

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

    def draft(self, materials, language, retry=False):
        if self.down:
            raise OllamaUnavailableError("Ollama no responde")
        titles = [title for _, title, _ in materials]
        if self.fail_titles & set(titles):
            raise OllamaError("draft: JSON inválido")
        outlets = ", ".join(outlet for outlet, _, _ in materials)
        return Draft(f"Nota: {titles[0]}", "Bajada", f"Cuerpo según {outlets}.", "Difieren.")


class FlakyLLM(FakeLLM):
    """Every note's first draft attempt fails (bad JSON); the second works."""

    def __init__(self):
        super().__init__()
        self.tried = set()

    def draft(self, materials, language, retry=False):
        title = materials[0][1]
        if title not in self.tried:
            self.tried.add(title)
            raise OllamaError("draft: JSON inválido")
        return super().draft(materials, language, retry)


def run(config, llm=None, **kwargs):
    return generate(config, http=http(), llm=llm or FakeLLM(), now=NOW, **kwargs)


class TopicLLM(FakeLLM):
    """Classifies by the titles it is given: Atacama is nature, everything else politics."""

    def __init__(self, error=None):
        super().__init__()
        self.error, self.topic_lists = error, []

    def classify(self, text, topics):
        self.topic_lists.append(topics)
        if self.error:
            raise self.error
        return "naturaleza" if ATACAMA[0] in text else "política"


def test_interests_order_the_edition_and_are_stored(config, engine):
    config.interests = InterestsConfig(like=["naturaleza"], dislike=["política"])
    llm = TopicLLM()
    assert run(config, llm).status == "ready"
    assert llm.topic_lists[0] == ["política", "naturaleza", *NEUTRAL_TOPICS]  # "no" wins
    with Session(engine) as s:
        notes = s.scalars(select(Note).order_by(Note.rank)).all()
        got = [(n.headline, n.is_front, n.interest, n.interest_liked) for n in notes]
    assert got == [
        (f"Nota: {ATACAMA[0]}", True, "naturaleza", True),
        (f"Nota: {SENADO[0]}", False, "política", False),  # 2 outlets, still off the front
    ]


def test_a_failed_classification_leaves_the_group_neutral(config, engine):
    config.interests = InterestsConfig(dislike=["política"])
    lines = []
    llm = TopicLLM(error=OllamaError("classify: JSON inválido"))
    assert run(config, llm, log=lines.append).status == "ready"
    with Session(engine) as s:
        assert {n.interest for n in s.scalars(select(Note))} == {None}
    assert any(line.startswith("  Sin clasificar") for line in lines)


def test_ollama_down_while_classifying_fails_the_edition(config):
    config.interests = InterestsConfig(like=["naturaleza"])
    llm = TopicLLM(error=OllamaUnavailableError("Ollama no responde"))
    assert run(config, llm).status == "failed"


def test_without_interests_nothing_is_classified(config):
    llm = TopicLLM()
    assert run(config, llm).status == "ready"
    assert llm.topic_lists == []


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


def test_feed_rules_exclude_and_route(config, engine):
    config.feeds[0].route = {"atacama": "Ciencia"}
    config.feeds[1].exclude = ["/SENADO"]  # case-insensitive, matches the URL
    assert run(config).status == "ready"
    with Session(engine) as s:
        notes = queries.front_page(s, queries.latest_ready_edition(s).id)
        assert {n.headline: n.section for n in notes} == {
            f"Nota: {SENADO[0]}": "Chile",
            f"Nota: {ATACAMA[0]}": "Ciencia",
        }
        assert s.scalar(select(Article).where(Article.url == SENADO_BBC[1])) is None  # not stored


def test_same_named_feeds_keep_their_own_rules(config, engine):
    # Two feeds of one outlet (counted as one source) with different sections and rules.
    config.feeds[1] = config.feeds[1].model_copy(update={"name": "CIPER", "exclude": ["/senado"]})
    assert run(config).status == "ready"
    with Session(engine) as s:
        notes = queries.front_page(s, queries.latest_ready_edition(s).id)
        assert {n.headline: n.section for n in notes} == {
            f"Nota: {SENADO[0]}": "Chile",
            f"Nota: {ATACAMA[0]}": "Chile",
        }


def test_everything_excluded_is_no_news(config):
    for feed in config.feeds:
        feed.exclude = [r"\.test/"]
    result = run(config)
    assert result.status == "failed"
    assert "Sin artículos nuevos" in result.message


def test_generate_logs_how_many_articles_fell_back_to_the_excerpt(config):
    lines = []
    assert run(config, log=lines.append).status == "ready"
    # Only the CIPER senado page exists in the fixtures; the other two keep their RSS excerpt.
    assert "  2 de 3 sin texto completo (se usa el extracto)" in lines


def test_ingest_skips_urls_already_stored(config, engine):
    assert ingest(engine, config, http()) == (3, [])
    assert ingest(engine, config, http()) == (0, [])  # e.g. `fetch` and `generate` at 06:00


def test_ingest_stores_an_article_listed_by_two_feeds_once(config, engine):
    config.feeds[1].url = config.feeds[0].url
    assert ingest(engine, config, http()) == (2, [])


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


def test_a_failed_draft_is_retried_once(config, engine):
    assert run(config, llm=FlakyLLM()).status == "ready"
    with Session(engine) as s:
        assert all(note.drafted for note in queries.latest_ready_edition(s).notes)


def one_article_selection():
    article = SimpleNamespace(
        source_name="Cooperativa", title="Lluvias en Santiago", content="Texto.", summary="Res."
    )
    return SimpleNamespace(items=[article])


class ScriptedLLM:
    """Answers each draft call with the next scripted Draft, or raises it if it's an error."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.retries = []

    def draft(self, materials, language, retry=False):
        self.retries.append(retry)
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


LOWER = Draft("suspenden clases en santiago", "las lluvias siguen.", "tras la lluvia…", "")
UPPER = Draft("Suspenden clases en Santiago", "Las lluvias siguen.", "Tras la lluvia…", "")


def test_lowercase_headline_is_redrafted():
    llm = ScriptedLLM(LOWER, UPPER)
    note = _draft(llm, one_article_selection(), "es", log=lambda _msg: None)
    assert (note.headline, note.drafted, llm.answers) == (UPPER.headline, True, [])
    assert llm.retries == [False, True]  # only the retry samples with a repeat penalty


@pytest.mark.parametrize("retry", [LOWER, OllamaError("draft: JSON inválido")])
def test_lowercase_draft_is_published_capitalized_if_the_retry_does_not_help(retry):
    note = _draft(ScriptedLLM(LOWER, retry), one_article_selection(), "es", log=lambda _m: None)
    assert (note.headline, note.dek, note.body, note.drafted) == (
        "Suspenden clases en santiago",
        "Las lluvias siguen.",
        "Tras la lluvia…",
        True,
    )


CUT = Draft("Juzgan a los creadores del bulo del", "Un juicio.", "Cuerpo.", "")
CUT_DEK = Draft("Zelenski advierte a EE. UU.", "Dice que el apoyo a Rusia sería un", "Cuerpo.", "")


@pytest.mark.parametrize("cut", [CUT, CUT_DEK])
def test_a_headline_or_dek_cut_by_a_quote_is_redrafted(cut):
    logged = []
    llm = ScriptedLLM(cut, UPPER)
    note = _draft(llm, one_article_selection(), "es", log=logged.append)
    assert (note.headline, llm.retries) == (UPPER.headline, [False, True])
    assert logged == ["  Intento 1: titular o bajada cortados (Lluvias en Santiago)"]


def test_a_cut_draft_is_published_if_the_retry_is_cut_too():
    note = _draft(ScriptedLLM(CUT, CUT_DEK), one_article_selection(), "es", log=lambda _m: None)
    assert (note.headline, note.drafted) == (CUT.headline, True)  # the first, as with lowercase


def test_a_lowercase_retry_beats_a_cut_first_draft():
    # Capitalizing fixes a lowercase headline; nothing brings back the words a quote cut off.
    note = _draft(ScriptedLLM(CUT, LOWER), one_article_selection(), "es", log=lambda _m: None)
    assert note.headline == "Suspenden clases en santiago"


@pytest.mark.parametrize(
    "headline, dek",
    [
        ("Película chilena *Hijas Únikas* triunfa en Valdivia", "Se estrena en *Guerra de verano*"),
        ("Plan de vivienda llega a 30 comunas", "Es la mayor ampliación desde 2019…"),
        ("El gobierno responde", ""),
        ("Gobierno anuncia seguimiento a detenidos tras las protestas del 18-O", "Lo dijo."),
        ("Nuevo estudio sobre la vitamina A", "Es un ensayo."),
    ],
)
def test_complete_headlines_and_deks_are_not_cut(headline, dek):
    llm = ScriptedLLM(Draft(headline, dek, "Cuerpo.", ""))
    _draft(llm, one_article_selection(), "es", log=lambda _m: None)
    assert llm.retries == [False]


def test_published_body_drops_stray_tags_and_braces():
    body = (
        "Primer párrafo.\n\nDice que x < 5 y y > 2. <ul><li>Según *El País*, sí.</li></ul>"
        " } </div>\n} \xa0\n{"
    )
    two_outlets = SimpleNamespace(
        items=[*one_article_selection().items, SimpleNamespace(
            source_name="CIPER", title="Lluvias", content="Texto.", summary="Res."
        )]
    )  # fmt: skip
    note = _draft(
        ScriptedLLM(Draft("Titular", "Bajada.", body, "<p>Difieren.</p>\n}")),
        two_outlets, "es", log=lambda _m: None,
    )  # fmt: skip
    assert note.body == "Primer párrafo.\n\nDice que x < 5 y y > 2.\n\nSegún *El País*, sí."
    assert note.differences == "Difieren."


def test_cleanup_keeps_blocks_apart():
    body = "Balance: <ul><li>10 muertos</li><li>20 heridos</li></ul>Fin.<br>Otra línea."
    note = _draft(
        ScriptedLLM(Draft("Titular", "Bajada.", body, "")),
        one_article_selection(), "es", log=lambda _m: None,
    )  # fmt: skip
    assert note.body == "Balance:\n\n10 muertos\n\n20 heridos\n\nFin.\n\nOtra línea."


def test_cleanup_keeps_autolinks_and_angle_brackets_that_are_not_html():
    body = (
        "Informe en <https://example.org/informe>. Usa `vector<int>` y a<b>c</b>."
        " Escribe a <a.vera@example.org> o <p.ramos@ciper.cl>."
    )
    note = _draft(
        ScriptedLLM(Draft("Titular", "Bajada.", body, "")),
        one_article_selection(), "es", log=lambda _m: None,
    )  # fmt: skip
    assert note.body == (
        "Informe en <https://example.org/informe>. Usa `vector<int>` y ac."
        " Escribe a <a.vera@example.org> o <p.ramos@ciper.cl>."
    )


def test_a_body_of_only_leftovers_counts_as_empty_and_is_retried():
    logged = []
    llm = ScriptedLLM(Draft("Titular", "Bajada.", "<p></p>\n}", ""), UPPER)
    note = _draft(llm, one_article_selection(), "es", log=logged.append)
    assert (note.headline, note.drafted) == (UPPER.headline, True)
    assert logged == ["  Intento 1 fallido (Lluvias en Santiago): titular o cuerpo vacío"]


def test_capitalize_skips_leading_punctuation_and_leaves_numbers_alone():
    assert _capitalize("¿qué pasó?") == "¿Qué pasó?"
    assert _capitalize("«la nota»") == "«La nota»"
    assert _capitalize("30 premios en 125 años") == "30 premios en 125 años"
    assert _capitalize("`print()` falla en Python 4") == "`print()` falla en Python 4"  # code span
    assert _capitalize("") == ""


def test_each_failed_attempt_is_logged_with_its_cause():
    lines = []
    llm = ScriptedLLM(OllamaError("Ollama 500: token repeat limit reached"), Draft("", "", "", ""))
    note = _draft(llm, one_article_selection(), "es", log=lines.append)
    assert note.drafted is False
    assert lines == [
        "  Intento 1 fallido (Lluvias en Santiago): Ollama 500: token repeat limit reached",
        "  Intento 2 fallido (Lluvias en Santiago): titular o cuerpo vacío",
        "  Publicada sin redactar (Lluvias en Santiago)",
    ]


def test_ollama_stuck_on_every_note_fails_the_edition(config, monkeypatch):
    monkeypatch.setattr("newspaper.edition.generate.FAILED_DRAFTS_IN_A_ROW", 2)
    result = run(config, llm=FakeLLM(fail_titles=[SENADO[0], SENADO_BBC[0], ATACAMA[0]]))
    assert result.status == "failed"
    assert "dejó de responder" in result.message


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


def test_failed_forced_regeneration_keeps_todays_edition(config, engine):
    assert run(config).status == "ready"
    assert run(config, llm=FakeLLM(down=True), force=True).status == "failed"
    with Session(engine) as s:
        edition = queries.latest_ready_edition(s)
        assert edition.edition_date == TODAY
        assert len(edition.notes) == 2


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
