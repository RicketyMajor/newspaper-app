from datetime import date, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from newspaper import notify
from newspaper.storage.models import Edition, PushSubscription
from newspaper.web.app import create_app

from .test_notify import browser, compressed_point

FCM = "https://fcm.googleapis.com/fcm/send/phone"
SAME = {"sec-fetch-site": "same-origin"}


@pytest.fixture
def client(config):
    return TestClient(create_app(config), base_url="http://127.0.0.1:8765")


@pytest.fixture
def sent(monkeypatch):
    calls = []

    def fake(engine, vapid, message, log, only=None, http=None):
        calls.append((message, only))
        return notify.Sent(accepted=1, total=1)

    monkeypatch.setattr(notify, "send", fake)
    return calls


def body(endpoint=FCM, keys=None, **extra):
    """What app.js and the service worker POST: PushSubscription.toJSON() plus our fields."""
    keys = keys or browser()[2]
    return {"subscription": {"endpoint": endpoint, "expirationTime": None, "keys": keys}, **extra}


def rows(engine):
    with Session(engine) as s:
        return list(s.scalars(select(PushSubscription).order_by(PushSubscription.id)))


def funnel(engine, day=date(2026, 10, 10)):
    with Session(engine) as s:
        e = s.scalars(select(Edition).where(Edition.edition_date == day)).one()
        return e.delivered_at, e.clicked_at


def test_subscribing_saves_it_and_confirms_only_when_asked(client, engine, sent):
    keys = browser()[2]

    first = client.post(
        "/aviso/alta", json=body(keys=keys, mobile=True, confirm=True), headers=SAME
    )
    again = client.post("/aviso/alta", json=body(keys=keys), headers=SAME)  # the visit's check

    assert first.json() == {"new": True, "gone": False}
    assert again.json() == {"new": False, "gone": False}
    [row] = rows(engine)
    assert (row.endpoint, row.mobile) == (FCM, True)
    [(message, only)] = sent
    assert (message.kind, message.title, only) == ("confirmacion", "Listo", row.id)
    assert message.body == "Te aviso cuando salga Newspaper."


def test_the_minimal_bodies_of_the_page_and_the_worker_are_accepted(client, engine):
    assert client.post("/aviso/alta", json=body()).status_code == 200  # no mobile, no old
    assert rows(engine)[0].mobile is False


@pytest.mark.parametrize(
    "change",
    [
        {"endpoint": "http://fcm.googleapis.com/fcm/send/x"},
        {"endpoint": "https://127.0.0.1:8765/a/1"},
        {"p256dh": notify.b64(b"\x04" + bytes(64))},
        {"p256dh": notify.b64(compressed_point())},
        {"auth": notify.b64(bytes(15))},
    ],
)
def test_subscriptions_the_pc_could_not_serve_are_refused(client, engine, change):
    endpoint = change.pop("endpoint", FCM)
    response = client.post("/aviso/alta", json=body(endpoint, {**browser()[2], **change}))
    assert response.status_code == 422 and rows(engine) == []


def test_a_rotation_keeps_the_row_so_notices_in_the_tray_still_count(client, engine):
    client.post("/aviso/alta", json=body(mobile=True))
    [before] = rows(engine)
    with Session(engine) as s, s.begin():
        s.execute(update(PushSubscription).values(gone_at=datetime(2026, 10, 10)))
    rotated = "https://fcm.googleapis.com/fcm/send/rotated"

    response = client.post("/aviso/alta", json=body(rotated, old=FCM))

    [after] = rows(engine)
    assert response.json() == {"new": False, "gone": False}
    assert (after.id, after.endpoint, after.mobile) == (before.id, rotated, True)
    assert after.gone_at is None


def test_a_rotation_onto_a_known_endpoint_drops_the_old_row(client, engine):
    other = "https://fcm.googleapis.com/fcm/send/other"
    client.post("/aviso/alta", json=body())
    client.post("/aviso/alta", json=body(other))
    client.post("/aviso/alta", json=body(other, old=FCM))
    assert [r.endpoint for r in rows(engine)] == [other]


def test_a_gone_subscription_says_so_and_gets_no_confirmation(client, engine, sent):
    client.post("/aviso/alta", json=body())
    with Session(engine) as s, s.begin():
        s.execute(update(PushSubscription).values(gone_at=datetime(2026, 10, 10)))

    response = client.post("/aviso/alta", json=body(confirm=True))

    assert response.json() == {"new": False, "gone": True} and sent == []


def test_unsubscribing_is_idempotent(client, engine):
    client.post("/aviso/alta", json=body())
    for _ in range(2):
        assert client.post("/aviso/baja", json={"endpoint": FCM}).status_code == 200
    assert rows(engine) == []


@pytest.mark.parametrize(
    ("path", "payload"),
    [
        ("/aviso/alta", body()),
        ("/aviso/baja", {"endpoint": FCM}),
        ("/aviso/2026-10-10/entregado?s=1", None),
    ],
)
def test_notice_routes_refuse_other_sites(client, seeded, path, payload):
    response = client.post(path, json=payload, headers={"sec-fetch-site": "cross-site"})
    assert response.status_code == 403


def test_only_the_phone_fills_the_funnel_and_only_once(client, engine, seeded):
    client.post("/aviso/alta", json=body(mobile=True))
    client.post("/aviso/alta", json=body("https://fcm.googleapis.com/fcm/send/pc", mobile=False))
    phone, pc = rows(engine)

    assert client.post(f"/aviso/2026-10-10/entregado?s={pc.id}").status_code == 204
    assert funnel(engine) == (None, None)
    client.post(f"/aviso/2026-10-10/entregado?s={phone.id}")
    first = funnel(engine)[0]
    client.post(f"/aviso/2026-10-10/entregado?s={phone.id}")
    client.post(f"/aviso/2026-10-10/tocado?s={phone.id}")

    delivered, clicked = funnel(engine)
    assert first is not None and delivered == first and clicked is not None
    assert client.post("/aviso/2026-10-10/tocado?s=999").status_code == 204
    assert client.post(f"/aviso/2026-10-11/tocado?s={phone.id}").status_code == 404


def test_the_service_worker_is_javascript_with_its_images_inlined(client):
    response = client.get("/sw.js")
    assert response.headers["content-type"].startswith("text/javascript")
    assert response.headers["cache-control"] == "no-cache"
    assert response.text.count('"data:image/png;base64,') == 2
    assert "__ICON__" not in response.text and '"Newspaper"' in response.text


@pytest.mark.parametrize("damage", ["corrupt", "folder"])
def test_a_broken_key_turns_the_notices_off_not_the_reader(config, seeded, damage):
    path = notify.key_path(config)
    if damage == "corrupt":
        path.write_bytes(b"no es una clave")
    else:
        path.mkdir()
    client = TestClient(create_app(config), base_url="http://127.0.0.1:8765")

    assert client.get("/").status_code == 200
    assert client.post("/aviso/alta", json=body()).status_code == 404
    assert client.get("/sw.js").status_code == 404
