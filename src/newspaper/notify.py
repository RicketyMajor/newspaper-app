"""Web Push for the daily notice (#17): VAPID (RFC 8292) and aes128gcm (RFC 8291).

The crypto lives in py-vapid, http-ece and cryptography; this module glues them to the
database and to httpx. Imported lazily: the readers start without it.
"""

from __future__ import annotations

import base64
import os
from pathlib import Path
from urllib.parse import urlsplit

import http_ece
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from py_vapid import Vapid02

from .config.models import NewspaperConfig

KEY_FILE = "vapid.pem"
# RFC 8292's contact claim. It names nobody on purpose: a repo URL would tie the author's
# public alias to every POST that reaches the push service (decision 2026-10-21).
CONTACT = "mailto:newspaper@localhost"
TTL = 12 * 3600  # a notice older than half a day is stale
PUSH_HOSTS = {"fcm.googleapis.com", "updates.push.services.mozilla.com", "web.push.apple.com"}
PUSH_SUFFIX = ".notify.windows.com"


def key_path(config: NewspaperConfig) -> Path:
    return Path(config.storage.database_path).expanduser().parent / KEY_FILE


def load_key(path: Path) -> Vapid02 | None:
    # Not Vapid02.from_file: it quietly creates a key when the file is missing.
    return Vapid02.from_pem(path.read_bytes()) if path.exists() else None


def ensure_key(path: Path) -> Vapid02:
    """The web's key, created once. A hard link is an atomic create-if-missing on Windows and
    POSIX, so two servers starting together end up with the same key."""
    if not path.exists():
        fresh = Vapid02()
        fresh.generate_keys()
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")  # same folder: same volume
        tmp.write_bytes(fresh.private_pem())
        try:
            os.link(tmp, path)
        except FileExistsError:  # another process won: use its key
            pass
        finally:
            tmp.unlink(missing_ok=True)
    return load_key(path)


def b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def unb64(text: str) -> bytes:
    # Browsers send base64url without padding (PushSubscription.toJSON()).
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def public_key(vapid: Vapid02) -> str:
    """The browser's applicationServerKey: the uncompressed point, base64url."""
    raw = vapid.public_key.public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    return b64(raw)


def valid_p256dh(text: str) -> bytes:
    raw = unb64(text)
    # Uncompressed only: http-ece feeds these bytes to the key derivation as they are, so a
    # compressed point would encrypt for a key the browser doesn't have.
    if len(raw) != 65 or raw[0] != 4:
        raise ValueError("p256dh: se espera un punto P-256 sin comprimir")
    ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), raw)  # off the curve: raises
    return raw


def allowed_endpoint(url: str) -> bool:
    """Known push services only: anyone in the tailnet could otherwise make the PC POST to a
    URL of their choosing every morning."""
    parts = urlsplit(url)
    host = parts.hostname or ""
    return parts.scheme == "https" and (host in PUSH_HOSTS or host.endswith(PUSH_SUFFIX))


def encrypt(payload: bytes, p256dh: str, auth: str) -> bytes:
    # A fresh ECDH key per message, never the VAPID key (RFC 8291 §3.1, RFC 8292).
    return http_ece.encrypt(
        payload,
        private_key=ec.generate_private_key(ec.SECP256R1()),
        dh=unb64(p256dh),
        auth_secret=unb64(auth),
        version="aes128gcm",
    )


def vapid_headers(vapid: Vapid02, endpoint: str, now: int) -> dict[str, str]:
    parts = urlsplit(endpoint)
    audience = f"https://{parts.hostname}" + (f":{parts.port}" if parts.port else "")
    return vapid.sign({"aud": audience, "exp": now + TTL, "sub": CONTACT})
