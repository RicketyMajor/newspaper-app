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
    assert "sábado 10 de octubre de 2026" in page
    assert "El Senado aprobó la reforma" in page  # the lead card carries its lede
    assert "Ver dónde difieren" in page


def test_cards_name_their_outlets_instead_of_stamping_single_source(client, seeded):
    page = client.get("/").text
    assert "CIPER y Cooperativa" in page
    assert "BBC Mundo" in page
    assert "Fuente única" not in page
    note = client.get(f"/n/{seeded['world']}").text
    assert "Fuente única:" in note  # explained where it matters


def test_note_page_has_one_h1_and_leads_to_the_next_note(client, seeded):
    page = client.get(f"/n/{seeded['front']}").text
    assert page.count("<h1") == 1
    assert "Nota 1 de 2" in page
    assert f'href="/n/{seeded["world"]}"' in page
    assert "Fin de la edición" in client.get(f"/n/{seeded['world']}").text


def test_nav_marks_the_current_page_and_shows_edition_progress(client, seeded):
    page = client.get("/archivo").text
    assert '<a href="/archivo" aria-current="page">' in page
    assert 'href="/e/2026-10-10/Mundo"' in page  # sections stay outside edition pages
    assert "2&nbsp;notas · 3&nbsp;min · 0&nbsp;leídas" in page


def test_errors_are_pages_not_json(client, seeded):
    missing = client.get("/n/999")
    assert missing.status_code == 404
    assert "Nota no encontrada" in missing.text
    assert "Volver a la portada" in missing.text
    bad = client.get("/e/hoy")
    assert bad.status_code == 422
    assert "<html" in bad.text
    assert "POST" in client.get(f"/n/{seeded['front']}/estado").headers["allow"]


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


def test_toggle_answers_fetch_with_json_and_keeps_the_source_in_view(client, engine, seeded):
    note, article = seeded["front"], seeded["articles"][0]
    json = {"accept": "application/json"}
    response = client.post(f"/n/{note}/estado", params={"flag": "read"}, headers=json)
    assert response.json() == {"value": True}
    response = client.post(f"/n/{note}/estado", params={"flag": "read"}, headers=json)
    assert response.json() == {"value": False}  # the note can be marked unread again
    with Session(engine) as s:
        assert not s.get(Note, note).is_read

    back = {"referer": f"http://127.0.0.1:8765/n/{note}"}
    response = client.post(
        f"/a/{article}/estado", params={"flag": "saved"}, headers=back, follow_redirects=False
    )
    assert response.headers["location"] == f"/n/{note}#a-{article}"  # no-JS fallback


def test_section_archive_search_and_status(client, seeded):
    assert "Cumbre climática" in client.get("/e/2026-10-10/Mundo").text
    assert "2026-10-10" in client.get("/archivo").text
    assert "Senado aprueba reforma" in client.get("/buscar", params={"q": "pension"}).text
    assert client.get("/e/1999-01-01").status_code == 404
    status = client.get("/estado").json()
    assert status["latest"] == "2026-10-10"
    assert set(status) == {"today", "error", "latest"}


def test_section_nav_on_inner_pages_without_repeated_kicker(client, seeded):
    nav = 'href="/e/2026-10-10/Mundo"'
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


def test_interest_label_on_cards_and_explained_on_the_note(client, engine, seeded):
    with Session(engine) as s, s.begin():
        note = s.get(Note, seeded["world"])
        note.interest, note.interest_liked = "geopolítica de prueba", False
    assert "Menos interés: geopolítica de prueba" in client.get("/").text
    page = client.get(f"/n/{seeded['world']}").text
    assert "<strong>Menos interés:</strong>" in page
    assert "Te interesa" not in client.get(f"/n/{seeded['front']}").text


def test_accepts_the_tailnet_name_behind_tailscale_serve(client):
    # `tailscale serve` forwards the original Host: <pc>.<tailnet>.ts.net (ipn/ipnlocal/serve.go).
    assert client.get("/", headers={"host": "desktop-ca3f86f.tail1234.ts.net"}).status_code == 200
    assert client.get("/", headers={"host": "evil.example"}).status_code == 400


@pytest.mark.parametrize("site", ["cross-site", "same-site"])
def test_toggles_refuse_other_sites(client, engine, seeded, site):
    # same-site ignores the port: a page on another 127.0.0.1 port is same-site.
    note = seeded["front"]
    response = client.post(
        f"/n/{note}/estado", params={"flag": "favorite"}, headers={"sec-fetch-site": site}
    )
    assert response.status_code == 403
    with Session(engine) as s:
        assert not s.get(Note, note).is_favorite


@pytest.mark.parametrize("site", ["same-origin", "none"])
def test_toggles_accept_this_site(client, seeded, site):
    headers = {"accept": "application/json", "sec-fetch-site": site}
    response = client.post(
        f"/n/{seeded['front']}/estado", params={"flag": "favorite"}, headers=headers
    )
    assert response.json() == {"value": True}


def test_search_page_has_its_own_form_and_no_second_one(client, seeded):
    page = client.get("/buscar", params={"q": "pension"}).text
    assert page.count('role="search"') == 1 and page.count('id="q"') == 1
    assert 'value="pension"' in page
    assert 'role="search"' in client.get("/").text  # other pages keep the masthead form


def test_note_page_marks_its_section_in_the_nav(client, seeded):
    page = client.get(f"/n/{seeded['world']}").text
    assert '<a href="/e/2026-10-10/Mundo" aria-current="location">' in page
    assert 'aria-current="page"' not in page


def test_masthead_links_to_search_for_small_screens(client, seeded):
    assert '<a class="search-link" href="/buscar">Buscar</a>' in client.get("/").text


def test_manifest_lets_chrome_install_the_paper(client):
    response = client.get("/manifest.webmanifest")
    assert response.headers["content-type"].startswith("application/manifest+json")
    manifest = response.json()
    assert manifest["name"] == manifest["short_name"] == "Newspaper"
    assert (manifest["start_url"], manifest["display"]) == ("/", "standalone")
    sizes = {(icon["sizes"], icon["purpose"]) for icon in manifest["icons"]}
    assert sizes == {("192x192", "any"), ("512x512", "any"), ("512x512", "maskable")}
    for icon in manifest["icons"]:
        assert client.get(icon["src"]).status_code == 200
    page = client.get("/").text
    assert '<link rel="manifest" href="/manifest.webmanifest">' in page
    assert '<meta name="theme-color"' in page


def test_toggle_form_without_js_from_this_site_still_redirects(client, seeded):
    # What a real browser sends for the no-JS form: same-origin, then back to the page.
    note = seeded["front"]
    headers = {"sec-fetch-site": "same-origin", "referer": f"http://127.0.0.1:8765/n/{note}"}
    response = client.post(
        f"/n/{note}/estado", params={"flag": "saved"}, headers=headers, follow_redirects=False
    )
    assert (response.status_code, response.headers["location"]) == (303, f"/n/{note}")
