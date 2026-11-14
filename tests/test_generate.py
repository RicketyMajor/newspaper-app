import os
import subprocess
import sys
from array import array
from datetime import UTC, date, datetime, timedelta
from email.utils import format_datetime
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from newspaper import notify
from newspaper.ai.ollama import NEUTRAL_TOPICS, Draft, OllamaError, OllamaUnavailableError
from newspaper.config.models import InterestsConfig, NtfyConfig
from newspaper.edition.generate import _capitalize, _clean_text, _draft, generate, ingest
from newspaper.edition.images import images_folder
from newspaper.edition.today import pid_alive
from newspaper.storage import queries
from newspaper.storage.models import Article, Edition, EditionStatus, Note

from .conftest import JPEG

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
        "https://ciper.test/fotos/senado.jpg": JPEG,  # the page's og:image
    }

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith(("/feed", "/rss")) and not feeds_ok:
            return httpx.Response(500)
        if url not in pages:
            return httpx.Response(404)
        return httpx.Response(200, content=iter([pages[url]]))  # streamed, as on the wire

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


class MatterLLM(TopicLLM):
    """«Same matter» for the candidates that contain one of `same`; records every question.
    `errors`: what each question raises in turn (None: it answers), then `error` for the rest."""

    def __init__(self, same=(), error=None, errors=()):
        super().__init__()
        self.same, self.matter_error, self.asked = set(same), error, []
        self.errors = list(errors)

    def same_matter(self, rejected, candidate):
        self.asked.append((rejected, candidate))
        error = self.errors.pop(0) if self.errors else self.matter_error
        if error:
            raise error
        return any(t in candidate for t in self.same)


def dismiss_earlier(engine, headline, vector, dismissed_days_ago=1, edition_days_ago=10):
    """An earlier edition's note, dismissed, made of one article with that embedding. One per
    edition: another dismissal needs another `edition_days_ago`."""
    old_day = TODAY - timedelta(days=edition_days_ago)
    with Session(engine) as s, s.begin():
        article = Article(
            url=f"https://old.test/{edition_days_ago}",
            title=headline,
            source_name="CIPER",
            section="Chile",
            published_at=datetime(2026, 9, 30, 12, 0),  # far outside the 36 h window
            embedding=array("f", vector).tobytes(),
        )
        note = Note(
            section="Chile",
            rank=0,
            headline=headline,
            dek="Bajada vieja",
            body="Cuerpo.",
            articles=[article],
            dismissed_at=NOW.replace(tzinfo=None) - timedelta(days=dismissed_days_ago),
        )
        s.add(Edition(edition_date=old_day, status=EditionStatus.ready, notes=[note]))


def test_a_group_on_a_dismissed_matter_goes_down_labelled(config, engine):
    dismiss_earlier(engine, "Senado debate *pensiones*", [1.0, 0.0])
    llm = MatterLLM(same=[SENADO[0]])
    lines = []
    assert run(config, llm, log=lines.append).status == "ready"
    assert len(llm.asked) == 1  # Atacama ([0, 1]) is far: never asked
    assert llm.asked[0][0] == "Senado debate *pensiones*\nBajada vieja"
    with Session(engine) as s:
        senado = s.scalars(select(Note).where(Note.headline == f"Nota: {SENADO[0]}")).one()
        assert (senado.interest, senado.interest_liked) == (
            "parecida a «Senado debate pensiones»",
            False,
        )
        assert not senado.is_front
    assert "No me interesa: bajan 1 de 2 grupos (1 pregunta, 1 rechazada activa)" in lines


def test_a_no_from_gemma3_leaves_the_group_as_it_was(config, engine):
    dismiss_earlier(engine, "Senado debate pensiones", [1.0, 0.0])
    llm = MatterLLM(same=())
    assert run(config, llm).status == "ready"
    assert llm.asked  # it was asked, and its "no" stands
    with Session(engine) as s:
        interests = {n.interest for n in s.scalars(select(Note)) if n.dismissed_at is None}
        assert interests == {None}


def test_a_dismissal_older_than_30_days_asks_nothing(config, engine):
    dismiss_earlier(engine, "Senado debate pensiones", [1.0, 0.0], dismissed_days_ago=31)
    llm = MatterLLM(same=[SENADO[0]])
    assert run(config, llm).status == "ready"
    assert llm.asked == []


def test_d_beats_a_yes_but_an_a_no_stays_and_is_not_asked(config, engine):
    dismiss_earlier(engine, "Senado debate pensiones", [1.0, 0.0])
    config.interests = InterestsConfig(like=["política"])  # TopicLLM: Senado is "política"
    llm = MatterLLM(same=[SENADO[0]])
    assert run(config, llm).status == "ready"
    with Session(engine) as s:
        senado = s.scalars(select(Note).where(Note.headline == f"Nota: {SENADO[0]}")).one()
        assert senado.interest_liked is False  # the "sí" of A lost to D
    config.interests = InterestsConfig(dislike=["política"])
    llm = MatterLLM(same=[SENADO[0]])
    assert run(config, llm, force=True).status == "ready"
    assert llm.asked == []  # already "no" by A: D never asks
    with Session(engine) as s:
        senado = s.scalars(select(Note).where(Note.headline == f"Nota: {SENADO[0]}")).one()
        assert senado.interest == "política"


def test_a_failed_comparison_leaves_the_group_and_is_logged(config, engine):
    dismiss_earlier(engine, "Senado debate pensiones", [1.0, 0.0])
    lines = []
    llm = MatterLLM(same=[SENADO[0]], error=OllamaError("same_matter: JSON inválido"))
    assert run(config, llm, log=lines.append).status == "ready"
    assert any(line.startswith("  Sin comparar") for line in lines)
    with Session(engine) as s:
        senado = s.scalars(select(Note).where(Note.headline == f"Nota: {SENADO[0]}")).one()
        assert senado.interest is None  # an error is no "yes": the group stays where it was


def test_ollama_down_while_comparing_fails_the_edition(config, engine):
    dismiss_earlier(engine, "Senado debate pensiones", [1.0, 0.0])
    llm = MatterLLM(error=OllamaUnavailableError("Ollama no responde"))
    assert run(config, llm).status == "failed"


def test_three_failed_comparisons_in_a_row_fail_the_edition(config, engine):
    # Ollama hung: each question waits out its timeout; hours of "no" would follow.
    for days in (10, 11, 12):
        dismiss_earlier(engine, f"Senado debate {days}", [1.0, 0.0], edition_days_ago=days)
    llm = MatterLLM(error=OllamaError("Ollama: ReadTimeout"))
    result = run(config, llm)
    assert result.status == "failed" and "dejó de responder" in result.message
    assert len(llm.asked) == 3


def test_an_answer_resets_the_failed_comparisons(config, engine):
    for days in range(10, 15):
        dismiss_earlier(engine, f"Senado debate {days}", [1.0, 0.0], edition_days_ago=days)
    timeout = OllamaError("Ollama: ReadTimeout")
    llm = MatterLLM(errors=[timeout, timeout, None, timeout, timeout])  # None: a "no"
    assert run(config, llm).status == "ready"
    assert len(llm.asked) == 5


def test_the_nearest_dismissal_is_asked_first_and_a_yes_ends_the_questions(config, engine):
    # Cosines to the Senado group: 0.90 and 0.50. The farther one is the newer dismissal, the
    # one active_dismissals gives first.
    dismiss_earlier(engine, "Cerca", [0.911, -0.413], dismissed_days_ago=2)
    dismiss_earlier(engine, "Lejos", [0.522, -0.853], edition_days_ago=11)
    llm = MatterLLM(same=())
    assert run(config, llm).status == "ready"
    assert [rejected for rejected, _ in llm.asked] == ["Cerca\nBajada vieja", "Lejos\nBajada vieja"]
    llm = MatterLLM(same=[SENADO[0]])
    assert run(config, llm, force=True).status == "ready"
    assert [rejected for rejected, _ in llm.asked] == ["Cerca\nBajada vieja"]


def test_a_dismissal_below_the_floor_is_never_asked(config, engine):
    dismiss_earlier(engine, "Casi", [0.463, -0.887])  # cosine 0.44 to the Senado group
    llm = MatterLLM(same=[SENADO[0]])
    assert run(config, llm).status == "ready"
    assert llm.asked == []


def test_a_dismissal_from_another_embedding_model_is_skipped_and_logged(config, engine):
    dismiss_earlier(engine, "Otro modelo", [1.0, 0.0, 0.0])  # 3 dimensions; today's have 2
    dismiss_earlier(engine, "Senado debate pensiones", [1.0, 0.0], edition_days_ago=11)
    lines = []
    llm = MatterLLM(same=[SENADO[0]])
    assert run(config, llm, log=lines.append).status == "ready"
    assert [rejected for rejected, _ in llm.asked] == ["Senado debate pensiones\nBajada vieja"]
    assert "  No me interesa: 1 rechazada con otro modelo de embeddings, ignorada" in lines


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


def test_published_notes_keep_a_photo_from_their_pages(config, engine, tmp_path):
    lines = []
    assert run(config, log=lines.append).status == "ready"
    with Session(engine) as s:
        senado, atacama = queries.front_page(s, queries.latest_ready_edition(s).id)
        assert (senado.image, senado.image_credit) == (f"{senado.id}.jpg", "CIPER")
        assert (atacama.image, atacama.image_credit) == (None, None)  # its page has none
        assert senado.articles[0].image_url == "https://ciper.test/fotos/senado.jpg"
    assert (images_folder(config) / senado.image).read_bytes() == JPEG
    assert "Fotos: 1 de 2" in lines


def test_a_photo_that_breaks_never_fails_the_edition(config, engine, monkeypatch):
    def broken(client, wanted):
        raise OSError("disco lleno")

    monkeypatch.setattr("newspaper.edition.images._download_all", broken)
    lines = []
    assert run(config, log=lines.append).status == "ready"
    assert "Fotos: disco lleno" in lines
    with Session(engine) as s:
        edition = queries.latest_ready_edition(s)
        assert edition.edition_date == TODAY
        assert [n.image for n in edition.notes] == [None, None]


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


@pytest.mark.parametrize(
    "credit",
    [
        "Foto | Juan Pérez vía Getty Images",
        "Foto: Agencia Uno",
        "FOTOGRAFÍA: EFE",
        "Imagen | Reuters",
    ],
)
def test_clean_text_drops_photo_credit_lines(credit):
    # #19: the extracted page keeps the caption's credit, and the model copies it as a paragraph.
    text = f"Primer párrafo.\n\n{credit}\n\nSegundo párrafo."
    assert _clean_text(text) == "Primer párrafo.\n\nSegundo párrafo."


def test_clean_text_keeps_a_sentence_that_starts_with_foto():
    text = "Fotografía y memoria se cruzan en la muestra."
    assert _clean_text(text) == text


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
    monkeypatch.setattr("newspaper.edition.generate.FAILED_CALLS_IN_A_ROW", 2)
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


def test_a_retried_failed_edition_owes_its_notice_again(config, engine, monkeypatch):
    # 13 Nov (#30): the first try failed (Ollama still starting after a power cut) and its
    # notice, «Hoy no hubo edición nueva», used up the edition's only one; the retry that
    # worked never told anyone.
    sent = datetime(2026, 11, 13, 12, 25)
    add_today(
        engine,
        status=EditionStatus.failed,
        notified_at=sent,
        delivered_at=sent,
        clicked_at=sent,
        cached_at=sent,
    )
    assert run(config).status == "ready"
    with Session(engine) as s:
        edition = queries.today_edition(s, TODAY)
        assert (edition.delivered_at, edition.clicked_at, edition.cached_at) == (None,) * 3
    told = []

    def fake_ntfy(_ntfy, message, _log):
        told.append(message)
        return notify.Sent(accepted=1, total=1)

    monkeypatch.setattr(notify, "send_ntfy", fake_ntfy)
    topic = NtfyConfig(url="https://ntfy.sh/t", click="https://pc.ts.net/")
    assert notify.edition_notice(config.model_copy(update={"ntfy": topic}), TODAY, print, now=NOW)
    [message] = told  # the ready edition's notice, not a second failure one
    assert "Hoy no hubo" not in message.title and message.lines


def test_a_forced_run_keeps_the_notice_already_sent(config, engine):
    assert run(config).status == "ready"
    sent = datetime(2026, 11, 13, 10, 30)
    with Session(engine) as s, s.begin():
        queries.today_edition(s, TODAY).notified_at = sent
    assert run(config, force=True).status == "ready"
    with Session(engine) as s:
        assert queries.today_edition(s, TODAY).notified_at == sent


def test_ready_edition_is_kept_unless_forced(config, engine):
    assert run(config).status == "ready"
    assert run(config).status == "skipped"
    assert run(config, force=True).status == "ready"
    with Session(engine) as s:
        [edition] = queries.list_editions(s)
        assert len(edition.notes) == 2
        assert len(queries.search(s, "lagarto")) == 1


def test_a_forced_run_gives_its_notes_new_ids(config, engine, tmp_path):
    # /img/{id} is cached as immutable: an id the forced run reused would show the old photo.
    def notes():
        with Session(engine) as s:
            return {n.id: n.image for n in s.scalars(select(Note))}

    assert run(config).status == "ready"
    before = notes()
    assert run(config, force=True).status == "ready"
    after = notes()
    assert len(after) == 2 and not set(before) & set(after)
    assert sorted(p.name for p in images_folder(config).iterdir()) == sorted(
        name for name in after.values() if name
    )  # the old notes' photos are gone


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
