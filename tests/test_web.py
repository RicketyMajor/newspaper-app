import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from newspaper.edition.today import local_today
from newspaper.storage.models import Article, Edition, EditionStatus, Note
from newspaper.web.app import create_app


@pytest.fixture
def client(config):
    return TestClient(create_app(config), base_url="http://127.0.0.1:8765")


def test_front_page_shows_lead_note_and_sections(client, seeded):
    page = client.get("/").text
    assert "Senado aprueba reforma de pensiones" in page
    assert "Cumbre climática cierra sin acuerdo" in page
    assert "2 fuentes" in page
    assert "sábado 10 de octubre de 2026" in page


def test_empty_state_without_editions(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "Todavía no hay ediciones" in response.text


def test_note_page_shows_sources_and_marks_read(client, engine, seeded):
    page = client.get(f"/n/{seeded['front']}").text
    assert "Dónde difieren" in page
    assert "CIPER destaca el costo fiscal" in page
    assert f"/a/{seeded['articles'][0]}" in page
    with Session(engine) as s:
        assert s.get(Note, seeded["front"]).is_read


def test_note_markdown_is_rendered_without_raw_html(client, engine, seeded):
    with Session(engine) as s, s.begin():
        s.get(Note, seeded["world"]).body = "**Clave**\n\n<script>alert(1)</script>"
    page = client.get(f"/n/{seeded['world']}").text
    assert "<strong>Clave</strong>" in page
    assert "<script>alert(1)</script>" not in page


def test_note_markdown_never_loads_external_images(client, engine, seeded):
    # The body is written by the LLM from third-party text: no tracking pixels.
    with Session(engine) as s, s.begin():
        s.get(Note, seeded["world"]).body = "Texto ![x](https://evil.test/t.png)"
    assert "<img" not in client.get(f"/n/{seeded['world']}").text


def test_rejects_foreign_host_header(client):
    # DNS rebinding: a page on another domain that resolves to 127.0.0.1 must not read us.
    assert client.get("/", headers={"host": "evil.test"}).status_code == 400
    assert client.get("/", headers={"host": "localhost:8765"}).status_code == 200


def test_original_link_redirects_and_marks_article_read(client, engine, seeded):
    article_id = seeded["articles"][2]
    response = client.get(f"/a/{article_id}", follow_redirects=False)
    assert response.status_code == 307
    assert response.headers["location"] == "https://news.test/3"
    with Session(engine) as s:
        assert s.get(Article, article_id).is_read


def test_toggle_state_and_flag_lists(client, seeded):
    note, article = seeded["front"], seeded["articles"][1]
    back = {"referer": f"http://testserver/n/{note}"}
    response = client.post(
        f"/n/{note}/estado", params={"flag": "favorite"}, headers=back, follow_redirects=False
    )
    assert response.status_code == 303
    assert response.headers["location"] == f"/n/{note}"
    client.post(f"/a/{article}/estado", params={"flag": "saved"})

    assert "Senado aprueba reforma" in client.get("/favoritos").text
    assert "Artículo 2" in client.get("/guardados").text
    assert client.post(f"/n/{note}/estado", params={"flag": "bogus"}).status_code == 422
    assert client.post("/n/999/estado", params={"flag": "read"}).status_code == 404


def test_section_archive_search_and_status(client, seeded):
    assert "Cumbre climática" in client.get("/e/2026-10-10/Mundo").text
    assert "2026-10-10" in client.get("/archivo").text
    assert "Senado aprueba reforma" in client.get("/buscar", params={"q": "pension"}).text
    assert client.get("/e/1999-01-01").status_code == 404
    status = client.get("/estado").json()
    assert status["latest"] == "2026-10-10"
    assert set(status) == {"today", "error", "latest"}


def test_section_nav_on_inner_pages_without_repeated_kicker(client, seeded):
    nav = '<a href="/e/2026-10-10/Mundo">Mundo</a>'
    assert nav in client.get(f"/n/{seeded['front']}").text
    section = client.get("/e/2026-10-10/Mundo").text
    assert nav in section
    assert '<p class="kicker">Mundo' not in section
    front = client.get("/").text
    assert '<p class="kicker">Chile</p>' in front  # front-page cards keep their section
    assert '<p class="kicker">Mundo' not in front


def test_failed_today_suggests_generate(client, engine, config, seeded):
    with Session(engine) as s, s.begin():
        s.add(Edition(edition_date=local_today(config), status=EditionStatus.failed, error="X"))
    page = client.get("/").text
    assert "La edición de hoy falló: X" in page
    assert "newspaper generate" in page
