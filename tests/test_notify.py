import json
import os
import time

import http_ece
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from py_vapid import Vapid02

from newspaper import notify


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
