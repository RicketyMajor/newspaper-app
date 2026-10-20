import re

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


def test_today_is_one_sky_with_the_core_and_the_sun(client, engine, seeded):
    page = client.get("/").text
    assert "Lo esencial: 0 de 2" in page  # seeded: 1 front + Mundo's first
    assert "2&nbsp;notas esenciales · 3&nbsp;min" in page
    assert 'class="rail"' in page and "--sol: 0.0" in page
    assert "Sigue aquí" in page  # the first unread core note is big
    with Session(engine) as s, s.begin():
        s.get(Note, seeded["front"]).is_read = True
    page = client.get("/").text
    assert "Lo esencial: 1 de 2" in page and "--sol: 0.5" in page


def test_section_bands_fold_the_rest_of_the_section(client, engine, seeded):
    with Session(engine) as s, s.begin():
        edition = s.get(Note, seeded["world"]).edition
        edition.notes.append(
            Note(section="Mundo", rank=5, is_front=False, headline="Otra de Mundo", dek="",
                 body="b", single_source=True, excerpt_only=False, drafted=True, read_minutes=1)
        )  # fmt: skip
    page = client.get("/").text
    assert "<details" in page and "y 1 más en Mundo" in page and "Otra de Mundo" in page


def test_cards_name_their_outlets_instead_of_stamping_single_source(client, seeded):
    page = client.get("/").text
    assert "CIPER y Cooperativa" in page
    assert "BBC Mundo" in page
    assert "Fuente única" not in page
    note = client.get(f"/n/{seeded['world']}").text
    assert "Fuente única:" in note  # explained where it matters


def test_note_page_has_one_h1_and_walks_the_reading_path_to_noon(client, seeded):
    first = client.get(f"/n/{seeded['front']}").text
    assert first.count("<h1") == 1
    assert "1 de 2" in first and f'rel="next" href="/n/{seeded["world"]}"' in first
    last = client.get(f"/n/{seeded['world']}").text
    assert "2 de 2" in last and 'rel="next" href="/fin/2026-10-10"' in last
    assert f'rel="prev" href="/n/{seeded["front"]}"' in last
    assert "Pleno día" in last


def test_notes_after_the_core_count_as_more_and_end_the_edition(client, engine, seeded):
    with Session(engine) as s, s.begin():
        edition = s.get(Note, seeded["world"]).edition
        edition.notes.append(
            Note(section="Mundo", rank=5, is_front=False, headline="Otra de Mundo", dek="",
                 body="b", single_source=True, excerpt_only=False, drafted=True, read_minutes=1)
        )  # fmt: skip
        s.flush()
        extra = edition.notes[-1].id
    page = client.get(f"/n/{extra}").text
    assert "Más · 1 de 1" in page and "Fin de la edición" in page
    assert 'rel="next"' not in page


def test_saved_merges_favorites_and_saved_and_old_urls_redirect(client, seeded):
    client.post(f"/n/{seeded['front']}/estado?flag=favorite")
    client.post(f"/n/{seeded['front']}/estado?flag=saved")
    everything = client.get("/guardado").text
    assert everything.count("Senado aprueba reforma") == 1  # favorite and saved: listed once
    assert "Senado aprueba reforma" in client.get("/guardado?f=favoritos").text
    client.post(f"/n/{seeded['front']}/estado?flag=saved")
    assert "Senado aprueba reforma" not in client.get("/guardado?f=guardados").text
    for old, new in (("/favoritos", "favoritos"), ("/guardados", "guardados")):
        r = client.get(old, follow_redirects=False)
        assert (r.status_code, r.headers["location"]) == (301, f"/guardado?f={new}")


def test_search_hits_name_the_day_and_section_of_notes(client, seeded):
    page = client.get("/buscar?q=senado").text
    assert "10 de octubre" in page and "Chile" in page


def test_archive_shows_how_far_the_sun_rose_each_day(client, engine, seeded):
    with Session(engine) as s, s.begin():
        s.get(Note, seeded["front"]).is_read = True
    assert "1 de 2 esenciales" in client.get("/archivo").text


def test_noon_page_closes_the_core(client, engine, seeded):
    with Session(engine) as s, s.begin():
        for note in s.get(Note, seeded["front"]).edition.notes:
            note.is_read = True
    page = client.get("/fin/2026-10-10").text
    assert "Pleno día." in page and "Terminaste lo esencial" in page
    assert "de 2&nbsp;secciones" in page  # Chile is only on the front page, and still counts
    assert "Te faltan" not in page
    assert "cerca de las 06:15" in page  # edition_hour 6
    assert client.get("/fin/2099-01-01").status_code == 404


def test_noon_reached_early_says_what_is_left_instead_of_claiming_it_is_done(client, seeded):
    page = client.get("/fin/2026-10-10").text
    assert "Te faltan 2 de las 2&nbsp;notas esenciales" in page
    assert "Terminaste lo esencial" not in page


def test_tabbar_marks_the_current_page(client, seeded):
    page = client.get("/archivo").text
    tabbar = page[page.index('<nav class="tabbar"') :]
    assert '<a href="/archivo" aria-current="page">' in tabbar
    for href in ("/", "/buscar", "/guardado"):
        assert f'<a href="{href}">' in tabbar


def test_pages_load_nothing_from_other_origins(client, seeded):
    for path in (
        "/",
        f"/n/{seeded['front']}",
        "/archivo",
        "/buscar",
        "/guardado",
        "/fin/2026-10-10",
    ):
        html = client.get(path).text
        assert not re.search(r'(src|href)="(https?:)?//', html), path  # originals use /a/{id}
    css = client.get("/static/style.css").text
    assert "url(http" not in css and "@import" not in css
    for font in re.findall(r"url\((/static/fonts/[\w.-]+)\)", css):
        assert client.get(font).status_code == 200, font
    assert "/static/fonts/newsreader-roman.woff2" in css


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


def test_opening_a_note_shows_sources_and_does_not_mark_it_read(client, engine, seeded):
    page = client.get(f"/n/{seeded['front']}").text
    assert "Dónde difieren" in page
    assert "CIPER destaca el costo fiscal" in page
    assert f"/a/{seeded['articles'][0]}" in page
    with Session(engine) as s:
        assert not s.get(Note, seeded["front"]).is_read  # it counts when finished (app.js)


def test_value_sets_a_flag_instead_of_toggling(client, seeded):
    url = f"/n/{seeded['front']}/estado?flag=read&value=true"
    for _ in range(2):  # reaching the end twice (a reload) must not unmark it
        assert client.post(url, headers={"Accept": "application/json"}).json() == {"value": True}
    off = f"/n/{seeded['front']}/estado?flag=read&value=false"
    assert client.post(off, headers={"Accept": "application/json"}).json() == {"value": False}


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

    assert "Senado aprueba reforma" in client.get("/guardado?f=favoritos").text
    assert "Artículo 2" in client.get("/guardado?f=guardados").text
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


def test_search_page_has_one_form(client, seeded):
    page = client.get("/buscar", params={"q": "pension"}).text
    assert page.count('role="search"') == 1 and page.count('id="q"') == 1
    assert 'value="pension"' in page


def test_manifest_lets_chrome_install_the_paper(client):
    response = client.get("/manifest.webmanifest")
    assert response.headers["content-type"].startswith("application/manifest+json")
    manifest = response.json()
    assert manifest["name"] == manifest["short_name"] == "Newspaper"
    assert (manifest["start_url"], manifest["display"]) == ("/", "standalone")
    assert (manifest["background_color"], manifest["theme_color"]) == ("#F2F4F5", "#F2F4F5")
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


def test_work_titles_in_headline_and_dek_render_in_italics_and_html_stays_escaped(
    client, engine, seeded
):
    with Session(engine) as s, s.begin():
        note = s.get(Note, seeded["world"])
        note.headline = "Película chilena *Hijas Únikas* triunfa"
        note.dek = "Se estrena con <b>éxito</b>."
    front, page = client.get("/").text, client.get(f"/n/{seeded['world']}").text
    for html in (front, page):
        assert "Película chilena <em>Hijas Únikas</em> triunfa" in html
        assert "&lt;b&gt;éxito&lt;/b&gt;" in html
    assert "<title>Película chilena Hijas Únikas triunfa ·" in page  # no asterisks in the tab
