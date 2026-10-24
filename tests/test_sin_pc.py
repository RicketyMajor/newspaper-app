import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from newspaper.edition.today import local_today
from newspaper.storage.models import Edition, EditionStatus, Note
from newspaper.web.app import create_app

PRELOAD = {"service-worker-navigation-preload": "true"}


@pytest.fixture
def client(config):
    return TestClient(create_app(config), base_url="http://127.0.0.1:8765")


def test_the_copy_lists_the_edition_guardado_and_their_sources(client, engine, seeded):
    with Session(engine) as s, s.begin():
        s.get(Note, seeded["front"]).is_read = True
        s.get(Note, seeded["world"]).is_favorite = True
    m = client.get("/sin-pc").json()
    front, world = seeded["front"], seeded["world"]
    assert m["day"] == "2026-10-10"
    assert m["pages"][:3] == ["/e/2026-10-10", f"/n/{front}", f"/n/{world}"]
    guardado = {"/guardado", "/guardado?f=favoritos", "/guardado?f=guardados"}
    assert {"/fin/2026-10-10", *guardado} <= set(m["pages"])
    assert any(u.startswith("/static/style.css?v=") for u in m["static"])
    assert "/static/fonts/newsreader-roman.woff2" in m["static"]
    assert set(m["originals"]) == {str(a) for a in seeded["articles"]}
    assert m["base"]["n"] == {str(front): "r", str(world): "f"}


def test_a_read_mark_keeps_the_revision_and_guardado_changes_it(client, engine, seeded):
    first = client.get("/sin-pc").json()["revision"]
    with Session(engine) as s, s.begin():
        s.get(Note, seeded["front"]).is_read = True
    assert client.get("/sin-pc").json()["revision"] == first
    with Session(engine) as s, s.begin():
        s.get(Note, seeded["front"]).is_saved = True
    assert client.get("/sin-pc").json()["revision"] != first


def test_no_edition_no_copy(client):
    response = client.get("/sin-pc")
    assert response.status_code == 404 and "Todavía no hay ediciones listas" in response.text


def test_saved_copies_never_freeze_the_days_banner(client, engine, config, seeded):
    with Session(engine) as s, s.begin():
        s.add(Edition(edition_date=local_today(config), status=EditionStatus.generating))
    live = client.get("/e/2026-10-10").text
    copy = client.get("/e/2026-10-10", headers={"x-sin-pc": "1"}).text
    assert "Preparando la edición" in live and 'data-today="generating"' in live
    assert "Preparando la edición" not in copy and 'data-today="ready"' in copy


def test_today_exposes_its_core_and_the_offline_line(client, config, seeded):
    config.general.notice_time = "07:30"
    core = f'data-nucleo="{seeded["front"]} {seeded["world"]}"'
    page = client.get("/").text
    assert core in page
    assert '<p class="sin-pc" role="status" data-aviso="07:30" hidden></p>' in page
    assert core in client.get(f"/n/{seeded['front']}").text


def test_a_preloaded_navigation_counts_as_an_opening(client, engine, seeded):
    client.get(f"/n/{seeded['front']}", headers=PRELOAD)
    with Session(engine) as s:
        assert s.get(Note, seeded["front"]).opened_at is not None


def test_a_saved_copy_is_no_opening(client, engine, seeded):
    client.get(f"/n/{seeded['front']}", headers={"x-sin-pc": "1"})
    with Session(engine) as s:
        assert s.get(Note, seeded["front"]).opened_at is None


def test_the_worker_is_served_even_without_notices(config, monkeypatch):
    monkeypatch.setattr("newspaper.web.app._push", lambda config: None)
    client = TestClient(create_app(config), base_url="http://127.0.0.1:8765")
    response = client.get("/sw.js")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/javascript")
