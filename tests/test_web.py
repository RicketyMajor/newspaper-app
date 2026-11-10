import os
import re
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from newspaper import schedule
from newspaper.config.models import CINEMA_NAMES, CineConfig
from newspaper.edition.images import images_folder
from newspaper.edition.today import local_today
from newspaper.storage.models import (
    Article,
    CinemaVenue,
    Edition,
    EditionStatus,
    Note,
    Screening,
    utcnow,
)
from newspaper.web.app import create_app

from .conftest import JPEG, give_photo


def test_front_page_shows_lead_note_and_sections(client, seeded):
    page = client.get("/").text
    assert "Senado aprueba reforma de pensiones" in page
    assert "Cumbre climática cierra sin acuerdo" in page
    assert "sábado 10 de octubre de 2026" in page


def test_today_has_the_name_between_the_core_and_the_minutes(client, engine, seeded):
    page = client.get("/").text
    assert "<h1>Newspaper</h1>" in page  # edition_name, in the gothic nameplate
    left = page.split('class="ear left"')[1].split("</a>")[0]
    assert "Esenciales" in left and "<b>0/2</b>" in left  # seeded: 1 front + Mundo's first
    assert "aria-hidden" not in left  # the name says what the ear shows (WCAG 2.5.3)
    # It leads to where the reading goes on, and its name says so (2.4.4).
    assert f'href="/n/{seeded["front"]}"' in left and "· seguir leyendo</span>" in left
    right = page.split('class="ear right"')[1].split("</p>")[0]
    assert "Lectura" in right and "<b>3</b>&nbsp;min" in right  # without [cine]: the minutes
    assert "Sigue aquí" in page  # the first unread core note says so
    assert "sábado 10 de octubre de 2026" in page
    with Session(engine) as s, s.begin():
        s.get(Note, seeded["front"]).is_read = True
    page = client.get("/").text
    assert "<b>1/2</b>" in page
    with Session(engine) as s, s.begin():
        s.get(Note, seeded["world"]).is_read = True
    left = client.get("/").text.split('class="ear left"')[1].split("</a>")[0]
    assert 'href="/fin/2026-10-10"' in left and "<b>2/2</b>" in left  # all read: Pleno día
    assert "· Pleno día</span>" in left


def test_the_right_ear_counts_todays_shows_with_cine(cine_client, engine, config, seeded):
    today = local_today(config)
    with Session(engine) as s, s.begin():
        s.add_all(
            [
                Screening(venue="normandie", day=today, time="15:00", title="Nine"),
                Screening(venue="ceina", day=today, time=None, title="VÍNCULO(S)"),
                Screening(venue="alameda", day=today + timedelta(days=1), time="16:30", title="X"),
            ]
        )
    page = cine_client.get("/").text
    right = page.split('class="ear right"')[1].split("</a>")[0]
    assert 'href="/cine"' in right and "Cine hoy" in right and "<b>2</b> funciones" in right
    with Session(engine) as s, s.begin():
        s.query(Screening).filter(Screening.day == today).delete()
    page = cine_client.get("/e/2026-10-10").text  # an older edition's page says today's too
    assert "sin funciones" in page.split('class="ear right"')[1].split("</a>")[0]


def test_the_sky_is_gone(client, seeded):
    page = client.get("/").text
    assert 'class="rail"' not in page and "amanecer" not in page and "alba:" not in page
    css = client.get("/static/style.css").text
    for gone in ("--sun", "--r0", "--tint", "sun-climbs", "#8a2c1d", "#8A2C1D"):
        assert gone not in css, gone
    assert "/static/fonts/unifraktur.woff2" in css


def test_kickers_are_stamps_and_photos_have_their_credit(client, engine, config, seeded):
    give_photo(engine, config, seeded["front"])  # credit: CIPER
    page = client.get("/").text
    assert '<p class="sello" data-seccion="Chile">Sigue aquí · ' in page
    img = f'<img src="/img/{seeded["front"]}" alt="" loading="lazy" decoding="async">'
    assert img in page and "Foto: CIPER" in page
    assert f'src="/img/{seeded["world"]}"' not in page  # Mundo's first has no photo: no box
    assert page.count("<figure") == 1 and page.count("Foto:") == 1


def test_under_sigue_aqui_the_meta_keeps_a_place_for_the_outlets(client, engine, seeded):
    # The stamp names the outlets; a copy without the PC that finds the note read moves them
    # back into the meta line, in place of this span (app.js applyMarks).
    page = client.get("/").text
    assert '<p class="meta"><span class="sigue">Chile · </span>2&nbsp;min' in page
    with Session(engine) as s, s.begin():
        s.get(Note, seeded["front"]).is_read = True
    page = client.get("/").text  # Mundo's first goes on, under its section's heading
    assert '<p class="meta"><span class="sigue"></span>1&nbsp;min' in page
    assert '<p class="meta"><span class="outlets multi">CIPER y Cooperativa</span> · 2' in page


def test_a_note_without_photo_has_no_img_nor_caption(client, seeded):
    page = client.get("/").text
    assert "<img" not in page and "<figure" not in page and "Foto:" not in page


def test_section_lines_fold_past_four(client, engine, seeded):
    with Session(engine) as s, s.begin():
        edition = s.get(Note, seeded["world"]).edition
        for n in range(5):
            edition.notes.append(
                Note(section="Mundo", rank=5 + n, is_front=False, headline=f"Otra de Mundo {n}",
                     dek="", body="b", single_source=True, excerpt_only=False, drafted=True,
                     read_minutes=1)
            )  # fmt: skip
    page = client.get("/").text
    assert '<h2 class="sec"' in page and "6&nbsp;notas" in page
    fold = page.index("<details")
    assert page.index("Otra de Mundo 3") < fold < page.index("Otra de Mundo 4")
    assert "y 1 más en Mundo" in page and page.count('<a class="row') == 5


def test_cards_name_their_outlets_instead_of_stamping_single_source(client, seeded):
    page = client.get("/").text
    assert "CIPER y Cooperativa" in page
    assert "BBC Mundo" in page
    assert "Fuente única" not in page
    note = client.get(f"/n/{seeded['world']}").text
    assert "Fuente única:" in note  # explained where it matters


def test_the_note_has_its_photo_across_with_the_credit(client, engine, config, seeded):
    give_photo(engine, config, seeded["front"])  # credit: CIPER
    page = client.get(f"/n/{seeded['front']}").text
    figure = page[page.index('<figure class="ph">') : page.index("</figure>")]
    assert f'<img src="/img/{seeded["front"]}" alt="" loading="lazy" decoding="async">' in figure
    assert '<figcaption class="cap">Foto: CIPER</figcaption>' in figure
    assert '<span class="sello">Chile</span>' in page  # the stamp, then the outlets
    assert "--tint" not in page and "var(--r" not in page  # no band tint any more
    without = client.get(f"/n/{seeded['world']}").text
    assert "<figure" not in without and "Foto:" not in without


def test_marks_are_drawn_and_the_words_stay(client, engine, seeded):
    with Session(engine) as s, s.begin():
        note = s.get(Note, seeded["front"])
        note.is_read = note.is_favorite = True
    page = client.get(f"/n/{seeded['front']}").text
    toggles = page[page.rindex('<div class="toggles">') :]
    buttons = re.findall(r"<button aria-pressed=\"\w+\">(.*?)</button>", toggles, re.S)
    assert [re.sub(r"<svg.*?</svg>", "", b) for b in buttons] == ["Leída", "Favorita", "Guardar"]
    assert all('aria-hidden="true"' in b for b in buttons)
    for html in (page, client.get("/").text):
        assert "★" not in html and "✓" not in html
    front = client.get("/").text
    assert '<svg class="ic check"' in front and "Leída: " in front  # read cards
    assert '<svg class="ic star"' in front and "Favorita" in front


def test_shortcuts_can_be_switched_off_and_the_unread_filter_is_there(client, seeded):
    page = client.get("/").text
    assert '<input type="checkbox" id="atajos-on" checked>' in page  # WCAG 2.1.4
    assert 'class="atajos-abrir"' in page  # the list opens without the ? key too
    button = '<button type="button" aria-pressed="false" class="solo-sin-leer">'
    assert f"{button}Solo sin leer</button>" in page
    assert "solo-sin-leer" not in client.get("/guardado").text  # Today only
    # The live region is there from the start, empty, so the news that today's edition is ready
    # is read out when app.js writes it.
    assert '<p class="banner ready" role="status"></p>' in page


def test_note_page_has_one_h1_and_walks_the_reading_path_to_noon(client, seeded):
    first = client.get(f"/n/{seeded['front']}").text
    assert first.count("<h1") == 1
    assert "Esencial 1 de 2" in first and f'rel="next" href="/n/{seeded["world"]}"' in first
    last = client.get(f"/n/{seeded['world']}").text
    assert "2 de 2" in last and 'rel="next" href="/fin/2026-10-10"' in last
    assert f'rel="prev" href="/n/{seeded["front"]}"' in last
    assert "Pleno día" in last
    # Back one note without ← or k (shortcuts may be off) nor a swipe (WCAG 2.1.1).
    assert f'<a class="prev" rel="prev" href="/n/{seeded["front"]}">' in last
    assert 'class="prev"' not in first  # the first note has none


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


def test_an_original_favorited_elsewhere_can_still_be_unfavorited(client, seeded):
    article = seeded["articles"][0]
    unfav = f'action="/a/{article}/estado?flag=favorite"'
    assert unfav not in client.get(f"/n/{seeded['front']}").text  # originals: Leído · Guardar
    client.post(f"/a/{article}/estado?flag=favorite")  # e.g. from the TUI
    assert unfav in client.get("/guardado?f=favoritos").text


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


def test_a_notes_photo_is_served_from_this_pc(client, engine, config, seeded):
    give_photo(engine, config, seeded["front"])
    response = client.get(f"/img/{seeded['front']}")
    assert response.status_code == 200 and response.content == JPEG
    assert response.headers["content-type"] == "image/jpeg"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "immutable" in response.headers["cache-control"]


def test_no_photo_no_file_or_no_note_is_a_404(client, engine, config, seeded):
    assert client.get(f"/img/{seeded['world']}").status_code == 404  # it has none
    give_photo(engine, config, seeded["front"], file=False)  # the file went missing
    assert client.get(f"/img/{seeded['front']}").status_code == 404
    assert client.get("/img/999").status_code == 404


@pytest.mark.parametrize("name", ["../newspaper.db", "1.svg", "x.jpg", "1.jpg\n", "1.JPG"])
def test_a_photo_name_out_of_shape_is_never_served(client, engine, config, seeded, name):
    # The file name never comes from the request, and only one shape of it is trusted.
    if "/" not in name:
        give_photo(engine, config, seeded["front"], "1.jpg")  # the folder exists, with a file
        (images_folder(config) / name.strip()).write_bytes(JPEG)
    give_photo(engine, config, seeded["front"], name, file=False)
    assert client.get(f"/img/{seeded['front']}").status_code == 404


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


def test_the_pc_stays_awake_only_while_needed(config, engine, monkeypatch):
    # 2026-10-22: the PC stayed awake from the 06:02 wake until noon with nobody reading.
    app = create_app(config)
    rechecks = []
    app.state.recheck = lambda: rechecks.append(1)
    client = TestClient(app, base_url="http://127.0.0.1:8765")
    assert not app.state.awake()  # nothing to do: Windows may sleep again
    client.get("/estado")  # a tab left open polls it: nobody is reading
    assert not app.state.awake() and not rechecks
    client.get("/")
    assert app.state.awake() and rechecks  # someone is reading: ask again now, not in a minute
    app.state.visited -= schedule.AWAKE_AFTER_VISIT
    assert not app.state.awake()

    with Session(engine) as s, s.begin():
        edition = Edition(edition_date=local_today(config), pid=os.getpid())
        s.add(edition)
        s.flush()
        edition_id = edition.id
    assert app.state.awake()  # being written: held before `generate` lets go
    with Session(engine) as s, s.begin():
        edition = s.get(Edition, edition_id)
        edition.status = EditionStatus.ready
        edition.finished_at = utcnow() - schedule.AWAY + timedelta(minutes=1)
    # ready, no notice yet: still held, so #22b decides away from the generation's phantoms
    assert app.state.awake()
    with Session(engine) as s, s.begin():
        edition = s.get(Edition, edition_id)
        edition.status, edition.finished_at = EditionStatus.failed, utcnow()
    assert app.state.awake()  # a failed run too: its hold doesn't end inside the task's window
    with Session(engine) as s, s.begin():
        edition = s.get(Edition, edition_id)
        edition.status, edition.finished_at = EditionStatus.ready, utcnow() - schedule.AWAY
    assert not app.state.awake()  # AWAY, not the notice's 30 min

    with Session(engine) as s, s.begin():
        s.get(Edition, edition_id).notified_at = utcnow() - timedelta(minutes=25)
    assert app.state.awake()  # the notice went out (or `newspaper notify`): room for the phone
    with Session(engine) as s, s.begin():
        s.get(Edition, edition_id).notified_at = utcnow() - schedule.AWAKE_AFTER_NOTICE
    assert not app.state.awake()

    config.general.notice_time = "07:30"

    def notice(at):  # today's only: awake() must ask for the local date
        return lambda config_, day: at if day == local_today(config) else None

    soon = datetime.now(UTC) - timedelta(minutes=25)
    monkeypatch.setattr("newspaper.web.app.notice_at", notice(soon))
    assert app.state.awake()  # the notice task's wake: a window even if its notice fails
    over = datetime.now(UTC) - schedule.AWAKE_AFTER_NOTICE
    monkeypatch.setattr("newspaper.web.app.notice_at", notice(over))
    assert not app.state.awake()
    later = datetime.now(UTC) + timedelta(minutes=1)
    monkeypatch.setattr("newspaper.web.app.notice_at", notice(later))
    assert not app.state.awake()  # not before it: the 06:00 wake isn't held until 08:00
    monkeypatch.setattr("newspaper.web.app.notice_at", notice(soon))
    with Session(engine) as s, s.begin():
        s.get(Edition, edition_id).edition_date = local_today(config) - timedelta(days=1)
    assert not app.state.awake()  # no edition today: nothing for the phone to save


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
    assert (manifest["background_color"], manifest["theme_color"]) == ("#F3EEE3", "#F3EEE3")
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


@pytest.fixture
def cine_client(config):
    salas = ["normandie", "alameda", "ceina"]
    with_cine = config.model_copy(update={"cine": CineConfig(salas=salas)})
    return TestClient(create_app(with_cine), base_url="http://127.0.0.1:8765")


def read_venues(s, *venues, error=None, ok=True):
    now = utcnow()
    for v in venues:
        s.add(
            CinemaVenue(
                venue=v,
                name=CINEMA_NAMES[v],
                checked_at=now,
                ok_at=now if ok else None,
                error=error,
            )
        )


def test_cine_shows_today_and_tomorrow_by_venue(cine_client, engine, config):
    today = local_today(config)
    with Session(engine) as s, s.begin():
        s.add_all(
            [
                Screening(venue="normandie", day=today, time="20:00", title="La haine (El odio)"),
                Screening(
                    venue="normandie",
                    day=today,
                    time="15:00",
                    title="Nine",
                    note="Alex Doll presenta",
                    url="https://www.flow.cl/btn.php?token=x",
                ),
                Screening(venue="ceina", day=today, time=None, title="VÍNCULO(S)", note="Danza"),
                Screening(
                    venue="alameda",
                    day=today + timedelta(days=1),
                    time="16:30",
                    title="Hangar Rojo",
                ),
            ]
        )
        read_venues(s, "normandie", "alameda", "ceina")
    page = cine_client.get("/cine")
    assert page.status_code == 200
    html = page.text
    order = [
        ">Hoy <span",
        "Normandie",
        "15:00",
        "Nine",
        "Alex Doll presenta",
        "20:00",
        "La haine (El odio)",
        "Centro Arte Alameda · sala CEINA",
        "Sin funciones publicadas",
        "CEINA · escenario",
        "VÍNCULO(S)",
        "Danza · sin hora publicada",  # no time and no link: nowhere to look it up
        ">Mañana <span",
        "Hangar Rojo",
    ]
    at = [html.index(x) for x in order]
    assert at == sorted(at)
    # Instants with the cinema's offset: a device in another zone dims and reloads on time.
    zone = ZoneInfo(config.general.timezone)
    start = datetime.combine(today, time(15), zone).isoformat()
    assert f'data-start="{start}"' in html
    midnight = datetime.combine(today + timedelta(days=1), time(0), zone).isoformat()
    assert f'data-hasta="{midnight}"' in html  # app.js reloads a board left open overnight
    assert 'href="https://www.flow.cl/btn.php?token=x"' in html
    assert 'href="/cine" aria-current="page"' in html
    assert '<ul class="shows" role="list">' in html  # Safari drops a styled list's role
    assert "Sin hora" in html  # read out instead of «—»
    assert 'class="date"' not in html  # the latest edition's date would contradict «Hoy»
    assert '<meta name="referrer" content="same-origin">' in html  # no *.ts.net to box offices


def test_cine_says_when_a_venue_could_not_be_read(cine_client, engine):
    with Session(engine) as s, s.begin():
        read_venues(s, "normandie", error="Server error '500 Internal Server Error'")
        read_venues(s, "ceina", error="sin red", ok=False)
    html = cine_client.get("/cine").text
    assert "No pude actualizar la cartelera desde las " in html  # read today: the hour only
    assert "Todavía no he podido leer esta cartelera." in html  # ceina: never a good read
    assert "Todavía no la he leído" in html  # alameda: no attempt yet


def test_without_cine_no_tab_and_no_page(client, seeded):
    assert client.get("/cine").status_code == 404
    assert 'href="/cine"' not in client.get("/").text
