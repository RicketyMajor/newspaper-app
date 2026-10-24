import json
import os
import time
from datetime import UTC, date, datetime
from types import SimpleNamespace

import http_ece
import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from py_vapid import Vapid02
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from newspaper import notify
from newspaper.edition.today import notice_at
from newspaper.storage.models import Edition, EditionStatus, Note, PushSubscription, utcnow


def browser():
    """A fake browser's push keys, as PushSubscription.toJSON() sends them: base64url, no '='."""
    key = ec.generate_private_key(ec.SECP256R1())
    raw = key.public_key().public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)
    auth = os.urandom(16)
    return key, auth, {"p256dh": notify.b64(raw), "auth": notify.b64(auth)}


def compressed_point():
    key = ec.generate_private_key(ec.SECP256R1())
    return key.public_key().public_bytes(Encoding.X962, PublicFormat.CompressedPoint)


def test_the_key_is_created_once_and_reused(tmp_path):
    path = tmp_path / "vapid.pem"
    first = notify.ensure_key(path)
    again = notify.ensure_key(path)
    assert notify.public_key(first) == notify.public_key(again)
    assert list(tmp_path.iterdir()) == [path]  # no temporary file left behind


def test_a_key_created_meanwhile_by_another_process_wins(tmp_path, monkeypatch):
    path = tmp_path / "vapid.pem"
    theirs = Vapid02()
    theirs.generate_keys()
    real_link = os.link

    def racing_link(src, dst):  # the other server writes its key just before our link
        path.write_bytes(theirs.private_pem())
        real_link(src, dst)

    monkeypatch.setattr(os, "link", racing_link)
    mine = notify.ensure_key(path)
    assert notify.public_key(mine) == notify.public_key(theirs)
    assert list(tmp_path.iterdir()) == [path]


def test_the_cli_never_creates_the_key(tmp_path):
    assert notify.load_key(tmp_path / "vapid.pem") is None
    assert not (tmp_path / "vapid.pem").exists()


def test_the_public_key_is_the_uncompressed_point(tmp_path):
    raw = notify.unb64(notify.public_key(notify.ensure_key(tmp_path / "vapid.pem")))
    assert len(raw) == 65 and raw[0] == 4


def test_p256dh_accepts_a_browser_key_without_padding():
    _, _, keys = browser()
    assert len(keys["p256dh"]) == 87  # base64url without "="
    assert len(notify.valid_p256dh(keys["p256dh"])) == 65


def test_p256dh_rejects_a_point_off_the_curve():
    with pytest.raises(ValueError):
        notify.valid_p256dh(notify.b64(b"\x04" + bytes(64)))


def test_p256dh_rejects_a_compressed_point_the_browser_could_not_decrypt_for():
    with pytest.raises(ValueError):
        notify.valid_p256dh(notify.b64(compressed_point()))


@pytest.mark.parametrize(
    ("url", "ok"),
    [
        ("https://fcm.googleapis.com/fcm/send/abc", True),
        ("https://wns2-par02p.notify.windows.com/w/?token=x", True),
        ("https://updates.push.services.mozilla.com/wpush/v2/x", True),
        ("http://fcm.googleapis.com/fcm/send/abc", False),
        ("https://fcm.googleapis.com.evil.test/x", False),
        ("https://127.0.0.1:8765/a/1", False),
    ],
)
def test_only_known_push_services_are_allowed(url, ok):
    assert notify.allowed_endpoint(url) is ok


def test_encrypt_roundtrips_with_a_fresh_key_that_is_never_the_vapid_one(tmp_path):
    vapid = notify.ensure_key(tmp_path / "vapid.pem")
    key, auth, keys = browser()
    payload = json.dumps({"title": "Alba · 10 esenciales, 23 min"}, ensure_ascii=False).encode()

    first = notify.encrypt(payload, keys["p256dh"], keys["auth"])
    second = notify.encrypt(payload, keys["p256dh"], keys["auth"])

    plain = http_ece.decrypt(first, private_key=key, auth_secret=auth, version="aes128gcm")
    assert plain == payload

    # aes128gcm header: salt (16) · rs (4) · idlen (1) · keyid (65) = the sender's public key
    def sender(body):
        return body[21:86]

    assert sender(first) != sender(second)
    assert notify.b64(sender(first)) != notify.public_key(vapid)


def test_vapid_headers_sign_the_real_claims(tmp_path):
    vapid = notify.ensure_key(tmp_path / "vapid.pem")
    now = int(time.time())

    endpoint = "https://fcm.googleapis.com/fcm/send/abc"
    auth = notify.vapid_headers(vapid, endpoint, now)["Authorization"]

    assert auth.startswith("vapid t=") and f",k={notify.public_key(vapid)}" in auth
    token = auth.removeprefix("vapid t=").split(",k=")[0]
    claims = json.loads(notify.unb64(token.split(".")[1]))
    assert claims == {
        "aud": "https://fcm.googleapis.com",
        "exp": now + notify.TTL,
        "sub": notify.CONTACT,
    }
    assert Vapid02.verify(auth)


EDITION = notify.Message("edicion", "Alba · 2 esenciales, 3 min", "Senado", date(2026, 10, 10))


def subscribe(engine, endpoint="https://fcm.googleapis.com/fcm/send/phone", mobile=True):
    key, auth, keys = browser()
    with Session(engine) as s, s.begin():
        row = PushSubscription(endpoint=endpoint, mobile=mobile, **keys)
        s.add(row)
        s.flush()
        return SimpleNamespace(id=row.id, key=key, auth=auth, endpoint=endpoint)


def service(*answers, seen=None):
    """A push service that answers in order (a status code or an exception to raise)."""
    replies = iter(answers)

    def handler(request):
        if seen is not None:
            seen.append(request)
        reply = next(replies)
        if isinstance(reply, Exception):
            raise reply
        return httpx.Response(reply)

    return httpx.Client(transport=httpx.MockTransport(handler))


def decrypt(request, sub):
    plain = http_ece.decrypt(
        request.content, private_key=sub.key, auth_secret=sub.auth, version="aes128gcm"
    )
    return json.loads(plain)


def test_send_posts_an_encrypted_signed_notice(engine, tmp_path):
    vapid = notify.ensure_key(tmp_path / "vapid.pem")
    phone = subscribe(engine)
    seen = []

    sent = notify.send(engine, vapid, EDITION, lambda _: None, http=service(201, seen=seen))

    assert (sent.accepted, sent.uncertain, sent.total) == (1, 0, 1)
    [request] = seen
    assert str(request.url) == phone.endpoint
    assert request.headers["TTL"] == "43200" and request.headers["Urgency"] == "high"
    assert request.headers["Topic"] == "edicion"
    assert request.headers["Content-Encoding"] == "aes128gcm"
    assert request.headers["Authorization"].startswith("vapid t=")
    assert decrypt(request, phone) == {
        "kind": "edicion",
        "title": "Alba · 2 esenciales, 3 min",
        "body": "Senado",
        "day": "2026-10-10",
        "sid": phone.id,
        "n": None,
    }


def test_the_days_notice_is_urgent_and_numbered(engine, tmp_path):
    vapid = notify.ensure_key(tmp_path / "vapid.pem")
    phone = subscribe(engine)
    seen = []
    message = notify.Message("edicion", "T", "B", DAY, n=1760092860)
    notify.send(engine, vapid, message, lambda _: None, http=service(201, seen=seen))
    assert seen[0].headers["Urgency"] == "high"
    assert decrypt(seen[0], phone)["n"] == 1760092860


def test_the_confirmation_stays_normal(engine, tmp_path):
    vapid = notify.ensure_key(tmp_path / "vapid.pem")
    subscribe(engine)
    seen = []
    test = notify.Message("confirmacion", "Listo", "x")
    notify.send(engine, vapid, test, lambda _: None, http=service(201, seen=seen))
    assert seen[0].headers["Urgency"] == "normal"


def test_the_confirmation_goes_to_one_browser_without_day_sid_or_topic(engine, tmp_path):
    vapid = notify.ensure_key(tmp_path / "vapid.pem")
    subscribe(engine)
    pc = subscribe(engine, "https://fcm.googleapis.com/fcm/send/pc", mobile=False)
    seen = []
    test = notify.Message("confirmacion", "Listo", "Te aviso cuando salga Alba.")

    notify.send(engine, vapid, test, lambda _: None, only=pc.id, http=service(201, seen=seen))

    [request] = seen
    assert str(request.url) == pc.endpoint and "Topic" not in request.headers
    assert decrypt(request, pc) == {
        "kind": "confirmacion",
        "title": "Listo",
        "body": "Te aviso cuando salga Alba.",
    }


def test_send_sorts_every_answer_and_keeps_going(engine, tmp_path):
    vapid = notify.ensure_key(tmp_path / "vapid.pem")
    subs = [subscribe(engine, f"https://fcm.googleapis.com/fcm/send/{n}") for n in range(5)]
    lines = []
    answers = service(201, 410, 500, httpx.ConnectError("refused"), httpx.ReadTimeout("slow"))

    sent = notify.send(engine, vapid, EDITION, lines.append, http=answers)

    assert (sent.accepted, sent.uncertain, sent.total) == (1, 1, 5)
    with Session(engine) as s:
        gone = s.scalars(select(PushSubscription.id).where(PushSubscription.gone_at.is_not(None)))
        assert list(gone) == [subs[1].id]
    assert [line.split(":")[0] for line in lines] == [
        "Aviso no enviado a fcm.googleapis.com",  # 410: gone
        "Aviso no enviado a fcm.googleapis.com",  # 500
        "Aviso no enviado a fcm.googleapis.com",  # never reached
        "Aviso incierto a fcm.googleapis.com",  # reached, answer lost
    ]


def test_gone_subscriptions_get_nothing(engine, tmp_path):
    vapid = notify.ensure_key(tmp_path / "vapid.pem")
    subscribe(engine)
    with Session(engine) as s, s.begin():
        s.execute(update(PushSubscription).values(gone_at=datetime(2026, 10, 10)))

    sent = notify.send(engine, vapid, EDITION, lambda _: None, http=service())

    assert sent.total == 0


def test_a_late_410_never_marks_the_subscription_that_replaced_it(engine, tmp_path):
    vapid = notify.ensure_key(tmp_path / "vapid.pem")
    phone = subscribe(engine)

    def rotating(request):  # the browser rotates (same row) while the old endpoint answers
        with Session(engine) as s, s.begin():
            s.get(PushSubscription, phone.id).endpoint = "https://fcm.googleapis.com/fcm/send/new"
        return httpx.Response(410)

    client = httpx.Client(transport=httpx.MockTransport(rotating))
    notify.send(engine, vapid, EDITION, lambda _: None, http=client)

    with Session(engine) as s:
        assert s.get(PushSubscription, phone.id).gone_at is None


def test_a_key_that_wont_encrypt_is_skipped_and_the_rest_still_go(engine, tmp_path):
    vapid = notify.ensure_key(tmp_path / "vapid.pem")
    bad = subscribe(engine, "https://fcm.googleapis.com/fcm/send/bad")
    good = subscribe(engine)
    with Session(engine) as s, s.begin():
        s.get(PushSubscription, bad.id).p256dh = notify.b64(b"\x04" + bytes(64))
    seen, lines = [], []

    sent = notify.send(engine, vapid, EDITION, lines.append, http=service(201, seen=seen))

    assert (sent.accepted, sent.total) == (1, 2)
    assert [str(r.url) for r in seen] == [good.endpoint]
    assert lines[0].startswith("Aviso no enviado a fcm.googleapis.com")


def test_the_payload_stays_in_one_record_even_in_4_byte_characters():
    message = notify.Message("edicion", "🌅" * 300, "📰" * 900, date(2026, 10, 10))

    payload = message.payload(7)

    data = json.loads(payload)
    assert len(data["title"]) == 80 and data["title"].endswith("…")
    assert len(data["body"]) == 200 and data["body"].endswith("…")
    assert len(payload) <= 3993  # RFC 8291 §4
    _, _, keys = browser()
    assert len(notify.encrypt(payload, keys["p256dh"], keys["auth"])) <= 4096  # one record


DAY = date(2026, 10, 10)  # the seeded edition


@pytest.fixture
def push(config, engine, monkeypatch):
    """A key, the phone subscribed and a fake `send` that records the messages."""
    notify.ensure_key(notify.key_path(config))
    subscribe(engine)
    calls = SimpleNamespace(messages=[], result=notify.Sent(accepted=1, total=1), during=None)

    def fake_send(engine_, vapid, message, log, only=None, http=None):
        calls.messages.append(message)
        if calls.during:
            calls.during()
        return calls.result

    monkeypatch.setattr(notify, "send", fake_send)
    return calls


def set_edition(engine, day=DAY, **values):
    with Session(engine) as s, s.begin():
        edition = s.scalars(select(Edition).where(Edition.edition_date == day)).one()
        for name, value in values.items():
            setattr(edition, name, value)


def marks(engine):
    with Session(engine) as s:
        e = s.scalars(select(Edition).where(Edition.edition_date == DAY)).one()
        return e.notified_at, e.delivered_at, e.clicked_at


def message_for(config, engine, day=DAY):
    with Session(engine) as s:
        edition = s.scalars(select(Edition).where(Edition.edition_date == day)).one()
        return notify.edition_message(s, config, edition)


def test_the_notice_names_the_core_and_its_first_headline(config, engine, seeded):
    message = message_for(config, engine)
    assert (message.kind, message.day) == ("edicion", DAY)
    assert message.title == "Newspaper · 2 esenciales, 3 min"
    assert message.body == "Senado aprueba reforma de pensiones"


def test_one_essential_is_singular_and_work_titles_lose_their_asterisks(config, engine, seeded):
    with Session(engine) as s, s.begin():
        s.delete(s.get(Note, seeded["world"]))
        s.get(Note, seeded["front"]).headline = "Estreno de *Hijas Únikas*"
    message = message_for(config, engine)
    assert message.title == "Newspaper · 1 esencial, 2 min"
    assert message.body == "Estreno de Hijas Únikas"


@pytest.mark.parametrize(
    ("day", "body"),
    [
        (date(2026, 10, 11), "Sigue la de ayer: 2 esenciales, 3 min."),
        (date(2026, 10, 13), "Sigue la del sábado 10 de octubre de 2026: 2 esenciales, 3 min."),
    ],
)
def test_a_failed_edition_says_so_and_which_one_is_left(config, engine, seeded, day, body):
    with Session(engine) as s, s.begin():
        s.add(Edition(edition_date=day, status=EditionStatus.failed))
    message = message_for(config, engine, day)
    assert message.title == "Newspaper · Hoy no hubo edición nueva"
    assert (message.body, message.day) == (body, day)


def test_a_first_edition_that_fails_points_to_the_log(config, engine):
    with Session(engine) as s, s.begin():
        s.add(Edition(edition_date=DAY, status=EditionStatus.failed))
    assert message_for(config, engine).body == "La causa quedó en el registro del PC."


@pytest.mark.parametrize("status", [EditionStatus.ready, EditionStatus.generating])
def test_nothing_to_say_without_a_core_or_while_generating(config, engine, status):
    with Session(engine) as s, s.begin():
        s.add(Edition(edition_date=DAY, status=status))
    assert message_for(config, engine) is None


def test_the_automatic_notice_goes_out_once(config, engine, seeded, push):
    notify.edition_notice(config, DAY, lambda _: None)
    notify.edition_notice(config, DAY, lambda _: None)  # a later fetch, a skipped generate
    assert len(push.messages) == 1 and marks(engine)[0] is not None


EARLY = datetime(2026, 10, 10, 10, 32, tzinfo=UTC)  # 07:32 in Santiago (UTC-3)
LATE = datetime(2026, 10, 10, 10, 36, tzinfo=UTC)  # 07:36


def test_notice_at_is_todays_local_time(config):
    assert notice_at(config, EARLY) is None
    config.general.notice_time = "07:30"
    at = notice_at(config, EARLY)
    assert (at.hour, at.minute, at.utcoffset().total_seconds()) == (7, 30, -3 * 3600)


def test_generate_and_fetch_wait_until_after_the_notice_time(config, engine, seeded, push):
    config.general.notice_time = "07:30"
    notify.edition_notice(config, DAY, print, now=EARLY)
    assert push.messages == [] and marks(engine)[0] is None
    notify.edition_notice(config, DAY, print, now=LATE)
    assert len(push.messages) == 1


def test_an_edition_done_past_midnight_is_held_to_its_own_day(config, engine, seeded, push):
    # The 10th's edition at 23:00, notice at 23:55, done at 00:10 on the 11th: its time is past.
    config.general.notice_time = "23:55"
    notify.edition_notice(config, DAY, print, now=datetime(2026, 10, 11, 3, 10, tzinfo=UTC))
    assert len(push.messages) == 1


def test_the_notice_task_skips_the_hour_guard(config, engine, seeded, push):
    config.general.notice_time = "07:30"
    notify.edition_notice(config, DAY, print, guard=False, now=EARLY)
    assert len(push.messages) == 1


def test_without_notice_time_the_notice_goes_out_with_the_edition(config, engine, seeded, push):
    notify.edition_notice(config, DAY, print, now=EARLY)
    assert len(push.messages) == 1


def test_an_opened_edition_is_not_notified_late(config, engine, seeded, push):
    set_edition(engine, opened_at=datetime(2026, 10, 10, 9, 0))
    notify.edition_notice(config, DAY, lambda _: None)
    assert push.messages == []


def test_no_subscriptions_no_claim(config, engine, seeded, push):
    with Session(engine) as s, s.begin():
        s.execute(update(PushSubscription).values(gone_at=datetime(2026, 10, 10)))
    notify.edition_notice(config, DAY, lambda _: None)
    assert push.messages == [] and marks(engine)[0] is None


@pytest.mark.parametrize(
    "change",
    [
        {"notified_at": datetime(2026, 10, 10, 9, 0)},  # another process notified
        {"opened_at": datetime(2026, 10, 10, 9, 0)},  # the reader opened it
        {"status": EditionStatus.failed},  # it changed state
    ],
)
def test_the_claim_loses_to_a_change_made_meanwhile(
    config, engine, seeded, push, monkeypatch, change
):
    real = notify.edition_message

    def racing(s, config_, edition):
        message = real(s, config_, edition)
        with engine.begin() as conn:  # between our read and our claim
            conn.execute(update(Edition).where(Edition.id == edition.id).values(**change))
        return message

    monkeypatch.setattr(notify, "edition_message", racing)
    notify.edition_notice(config, DAY, lambda _: None)
    assert push.messages == []


def test_nothing_accepted_releases_the_claim_for_the_next_fetch(config, engine, seeded, push):
    push.result = notify.Sent(total=1)
    notify.edition_notice(config, DAY, lambda _: None)
    assert marks(engine)[0] is None


def test_an_uncertain_answer_keeps_the_claim(config, engine, seeded, push):
    push.result = notify.Sent(uncertain=1, total=1)
    notify.edition_notice(config, DAY, lambda _: None)
    assert marks(engine)[0] is not None


def test_the_funnel_is_emptied_before_sending_so_a_fast_ping_survives(config, engine, seeded, push):
    old, ping = datetime(2026, 10, 9, 9, 0), datetime(2026, 10, 10, 9, 21, 5)
    set_edition(engine, delivered_at=old, clicked_at=old)
    push.during = lambda: set_edition(engine, delivered_at=ping)  # the phone, mid-send

    notify.edition_notice(config, DAY, lambda _: None)

    notified, delivered, clicked = marks(engine)
    assert notified is not None and delivered == ping and clicked is None


def test_without_the_key_the_notice_waits_and_says_why(config, engine, seeded, push):
    notify.key_path(config).unlink()
    lines = []
    notify.edition_notice(config, DAY, lines.append)
    assert push.messages == [] and "vapid.pem" in lines[0]


def test_resend_ignores_the_previous_notice(config, engine, seeded, push):
    set_edition(engine, notified_at=datetime(2026, 10, 10, 9, 21))
    assert notify.resend(config, DAY, lambda _: None) == "Aviso enviado a 1 de 1 suscripciones."
    assert len(push.messages) == 1


def test_resend_puts_the_previous_marks_back_when_nothing_goes_out(config, engine, seeded, push):
    before = (datetime(2026, 10, 10, 9, 21), datetime(2026, 10, 10, 10, 10), None)
    set_edition(engine, notified_at=before[0], delivered_at=before[1])
    push.result = notify.Sent(total=1)

    assert notify.resend(config, DAY, lambda _: None) == "Aviso enviado a 0 de 1 suscripciones."
    assert marks(engine) == before


def test_a_failed_resend_never_undoes_one_that_succeeded_meanwhile(config, engine, seeded, push):
    theirs = (datetime(2026, 10, 10, 12, 0, 1), datetime(2026, 10, 10, 12, 0, 2), None)
    push.result = notify.Sent(total=1)
    # Another `newspaper notify` succeeds, and its phone answers, while ours is still sending.
    push.during = lambda: set_edition(engine, notified_at=theirs[0], delivered_at=theirs[1])

    notify.resend(config, DAY, lambda _: None)

    assert marks(engine) == theirs


def test_resend_explains_when_there_is_nothing_to_send(config, engine, seeded, push):
    assert notify.resend(config, date(2026, 10, 11), print) == "Todavía no hay edición de hoy."
    with Session(engine) as s, s.begin():
        s.execute(update(PushSubscription).values(gone_at=datetime(2026, 10, 10)))
    assert notify.resend(config, DAY, print) == "No hay suscripciones: actívalas al pie de Hoy."


def test_the_claim_numbers_the_notice_and_empties_saved(config, engine, seeded, push):
    set_edition(engine, cached_at=datetime(2026, 10, 9, 10, 0))
    notify.edition_notice(config, DAY, lambda _: None)
    with Session(engine) as s:
        e = s.scalars(select(Edition).where(Edition.edition_date == DAY)).one()
        assert e.cached_at is None
        assert push.messages[0].n == notify.number(e.notified_at)


def test_resend_numbers_it_and_restores_saved_when_nothing_goes_out(config, engine, seeded, push):
    saved = datetime(2026, 10, 10, 10, 31)
    set_edition(engine, notified_at=datetime(2026, 10, 10, 10, 30), cached_at=saved)
    push.result = notify.Sent(total=1)
    notify.resend(config, DAY, lambda _: None)
    assert push.messages[0].n is not None
    with Session(engine) as s:
        e = s.scalars(select(Edition).where(Edition.edition_date == DAY)).one()
        assert e.cached_at == saved


def ping_row(engine):
    with Session(engine) as s:
        e = s.scalars(select(Edition).where(Edition.edition_date == DAY)).one()
        return e.notified_at, e.delivered_at, e.cached_at


def test_saved_is_a_ping_of_its_own(config, engine, seeded):
    phone = subscribe(engine)
    notified = datetime(2026, 10, 10, 10, 30)
    set_edition(engine, notified_at=notified)
    assert notify.record_ping(engine, DAY, "guardada", phone.id, n=notify.number(notified))
    assert ping_row(engine)[2] is not None


def test_a_ping_for_an_old_notice_is_ignored_and_one_without_number_counts(config, engine, seeded):
    phone = subscribe(engine)
    set_edition(engine, notified_at=datetime(2026, 10, 10, 10, 30))
    assert notify.record_ping(engine, DAY, "entregado", phone.id, n=123)
    assert ping_row(engine)[1] is None
    assert notify.record_ping(engine, DAY, "entregado", phone.id)
    assert ping_row(engine)[1] is not None


def test_queued_pings_keep_the_earliest_time_inside_the_notice(config, engine, seeded):
    phone = subscribe(engine)
    notified = datetime(2026, 10, 10, 10, 30)
    set_edition(engine, notified_at=notified)
    later = datetime(2026, 10, 10, 11, 5, tzinfo=UTC)
    earlier = datetime(2026, 10, 10, 10, 40, tzinfo=UTC)
    before = datetime(2026, 10, 10, 9, 0, tzinfo=UTC)
    notify.record_ping(engine, DAY, "entregado", phone.id, at=later)
    notify.record_ping(engine, DAY, "entregado", phone.id, at=earlier)
    assert ping_row(engine)[1] == datetime(2026, 10, 10, 10, 40)
    notify.record_ping(engine, DAY, "entregado", phone.id, at=before)
    assert ping_row(engine)[1] == notified  # never before the notice itself


def test_the_notice_task_sends_once_and_stops(config, engine, seeded, push):
    naps = []
    notify.pending_notice(config, DAY, lambda _: None, sleep=naps.append)
    assert len(push.messages) == 1 and naps == []


def test_the_notice_task_tries_again_while_nothing_went_out(config, engine, seeded, push):
    results = iter([notify.Sent(total=1), notify.Sent(accepted=1, total=1)])
    push.during = lambda: setattr(push, "result", next(results))
    naps = []
    notify.pending_notice(config, DAY, lambda _: None, sleep=naps.append)
    assert len(push.messages) == 2 and naps == [notify.PENDING_EVERY]


def test_the_notice_task_waits_for_an_edition_still_being_written(config, engine, seeded, push):
    set_edition(engine, status=EditionStatus.generating)
    naps = []

    def nap(seconds):
        naps.append(seconds)
        set_edition(engine, status=EditionStatus.ready)  # published at 07:31

    notify.pending_notice(config, DAY, lambda _: None, sleep=nap)
    assert len(push.messages) == 1 and len(naps) == 1


def clock_and_nap():
    """A fake monotonic clock and a `sleep` that moves it."""
    now = [0.0]
    return (lambda: now[0]), (lambda seconds: now.__setitem__(0, now[0] + seconds)), now


def test_the_notice_task_gives_up_after_nine_minutes(config, engine, seeded, push):
    push.result = notify.Sent(total=1)
    clock, nap, _ = clock_and_nap()
    lines = []
    notify.pending_notice(config, DAY, lines.append, sleep=nap, clock=clock)
    assert len(push.messages) == 27 and "9 minutos" in lines[-1]  # every 20 s, from 0 to 520


def test_slow_sends_count_against_the_nine_minutes(config, engine, seeded, push):
    # No network: each try waits 25 s for its timeouts. Windows ends the task at 10 min, maybe
    # holding the claim, and then no fetch would ever retry: the last try must end before that.
    push.result = notify.Sent(total=1)
    clock, nap, now = clock_and_nap()
    push.during = lambda: nap(25)
    notify.pending_notice(config, DAY, lambda _: None, sleep=nap, clock=clock)
    assert len(push.messages) == 12 and now[0] <= notify.PENDING_FOR


def test_the_notice_task_waits_out_a_claim_still_being_sent(config, engine, seeded, push):
    # A fetch claimed it a moment ago and found no network: the task is the one that retries.
    set_edition(engine, notified_at=utcnow())
    naps = []

    def nap(seconds):
        naps.append(seconds)
        set_edition(engine, notified_at=None)  # the fetch's send failed: it let go

    notify.pending_notice(config, DAY, lambda _: None, sleep=nap)
    assert len(push.messages) == 1 and naps == [notify.PENDING_EVERY]


def test_the_notice_task_leaves_a_settled_claim_alone(config, engine, seeded, push):
    set_edition(engine, notified_at=utcnow() - notify.IN_FLIGHT)
    naps = []
    notify.pending_notice(config, DAY, lambda _: None, sleep=naps.append)
    assert push.messages == [] and naps == []


def test_the_notice_task_leaves_an_opened_edition_alone(config, engine, seeded, push):
    set_edition(engine, opened_at=datetime(2026, 10, 10, 10, 0))
    naps = []
    notify.pending_notice(config, DAY, lambda _: None, sleep=naps.append)
    assert push.messages == [] and naps == []


def test_the_notice_task_goes_without_the_hour_guard(config, monkeypatch):
    # The seeded day is long past, so the guard would let it through anyway: check the call.
    calls = []
    monkeypatch.setattr(notify, "edition_notice", lambda *args, **kw: calls.append(kw))
    monkeypatch.setattr(notify, "notice_pending", lambda config_, day: False)
    notify.pending_notice(config, DAY, lambda _: None, sleep=lambda _: None)
    assert calls == [{"guard": False}]
