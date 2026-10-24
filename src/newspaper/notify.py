"""Web Push for the daily notice (#17): VAPID (RFC 8292) and aes128gcm (RFC 8291).

The crypto lives in py-vapid, http-ece and cryptography; this module glues them to the
database and to httpx. Imported lazily: the readers start without it.
"""

from __future__ import annotations

import base64
import json
import os
import time
from collections.abc import Callable
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

import http_ece
import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from py_vapid import Vapid02
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from .config.models import NewspaperConfig
from .dates import long_date
from .edition.today import notice_at
from .storage import queries
from .storage.database import open_db
from .storage.models import Edition, EditionStatus, PushSubscription, clamped, utcnow

KEY_FILE = "vapid.pem"
# RFC 8292's contact claim. It names nobody on purpose: a repo URL would tie the author's
# public alias to every POST that reaches the push service (decision 2026-10-21).
CONTACT = "mailto:newspaper@localhost"
TTL = 12 * 3600  # a notice older than half a day is stale
PUSH_HOSTS = {"fcm.googleapis.com", "updates.push.services.mozilla.com", "web.push.apple.com"}
PUSH_SUFFIX = ".notify.windows.com"
# generate and fetch let the notice task go first at notice_time: it is the one that retries.
NOTICE_GRACE = timedelta(minutes=5)


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


def _cut(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _why(error: Exception) -> str:
    return str(error) or type(error).__name__


@dataclass
class Message:
    kind: str  # "edicion": the day's notice; "confirmacion": the test sent when activating
    title: str
    body: str
    day: date | None = None
    n: int | None = None  # the notice's number (#20a): its pings carry it back

    def payload(self, sid: int) -> bytes:
        # Cut so the JSON stays far below one aes128gcm record (3993 bytes, RFC 8291 §4), even
        # in 4-byte characters.
        data = {"kind": self.kind, "title": _cut(self.title, 80), "body": _cut(self.body, 200)}
        if self.kind == "edicion":  # the service worker pings back with these
            data |= {"day": self.day.isoformat(), "sid": sid, "n": self.n}
        return json.dumps(data, ensure_ascii=False).encode()


@dataclass
class Sent:
    accepted: int = 0
    uncertain: int = 0  # reached the push service but the answer was lost: maybe delivered
    total: int = 0


def number(at: datetime) -> int:
    """A notice's number, sent in it and in its pings: its notified_at in seconds."""
    return int(at.replace(tzinfo=UTC).timestamp())


def send(
    engine: Engine,
    vapid: Vapid02,
    message: Message,
    log: Callable[[str], None],
    only: int | None = None,
    http: httpx.Client | None = None,
) -> Sent:
    """Encrypt, sign and POST the message to every live subscription (or just `only`)."""
    with Session(engine) as s:
        stmt = select(PushSubscription).where(PushSubscription.gone_at.is_(None))
        if only is not None:
            stmt = stmt.where(PushSubscription.id == only)
        subscriptions = list(s.scalars(stmt.order_by(PushSubscription.id)))
    sent = Sent(total=len(subscriptions))
    # No redirects: the endpoint was checked against the known push services, its target wasn't.
    client = nullcontext(http) if http else httpx.Client(timeout=10, follow_redirects=False)
    with client as client:
        for sub in subscriptions:
            host = urlsplit(sub.endpoint).hostname
            try:
                body = encrypt(message.payload(sub.id), sub.p256dh, sub.auth)
                headers = vapid_headers(vapid, sub.endpoint, int(time.time())) | {
                    "TTL": str(TTL),
                    # High for the day's notice: Android delivers it in Doze, and the worker gets
                    # the network to save the copy (#20a).
                    "Urgency": "high" if message.kind == "edicion" else "normal",
                    "Content-Encoding": "aes128gcm",
                    "Content-Type": "application/octet-stream",
                }
                if message.kind == "edicion":
                    # The push service replaces a pending message of the same topic with the
                    # newer one (RFC 8030 §5.4); never on the test, or it could eat the notice.
                    headers["Topic"] = "edicion"
                response = client.post(sub.endpoint, content=body, headers=headers)
            except (httpx.ConnectError, httpx.ConnectTimeout) as e:  # never reached the service
                log(f"Aviso no enviado a {host}: {_why(e)}")
                continue
            except httpx.HTTPError as e:  # reached it and lost the answer: don't send it twice
                log(f"Aviso incierto a {host}: {_why(e)}")
                sent.uncertain += 1
                continue
            except Exception as e:  # a key that won't encrypt or sign: nothing left the PC
                log(f"Aviso no enviado a {host}: {_why(e)}")
                continue
            if response.is_success:
                sent.accepted += 1
            elif response.status_code in (404, 410):  # gone for good: Today renews it
                with Session(engine) as s, s.begin():
                    s.execute(
                        update(PushSubscription)
                        # The endpoint too: a rotation meanwhile keeps the id, not the endpoint.
                        .where(
                            PushSubscription.id == sub.id,
                            PushSubscription.endpoint == sub.endpoint,
                        )
                        .values(gone_at=utcnow())
                    )
                log(f"Aviso no enviado a {host}: la suscripción ya no existe")
            else:
                log(f"Aviso no enviado a {host}: HTTP {response.status_code}")
    return sent


def _essentials(core: list) -> str:
    n = len(core)
    return f"{n} {'esencial' if n == 1 else 'esenciales'}, {sum(x.read_minutes for x in core)} min"


def edition_message(s: Session, config: NewspaperConfig, edition: Edition) -> Message | None:
    """The notice for this edition's state, or None when there is nothing to say."""
    name = config.general.edition_name
    if edition.status == EditionStatus.ready:
        core, _ = queries.reading_order(edition.notes)
        if not core:
            return None
        headline = core[0].headline.replace("*", "")  # *work titles* are italics on the web only
        return Message("edicion", f"{name} · {_essentials(core)}", headline, edition.edition_date)
    if edition.status == EditionStatus.failed:
        latest = queries.latest_ready_edition(s)
        if latest is None:
            body = "La causa quedó en el registro del PC."
        else:  # honest labels: "yesterday" only when it is
            yesterday = latest.edition_date == edition.edition_date - timedelta(days=1)
            which = "la de ayer" if yesterday else f"la del {long_date(latest.edition_date)}"
            body = f"Sigue {which}: {_essentials(queries.reading_order(latest.notes)[0])}."
        return Message("edicion", f"{name} · Hoy no hubo edición nueva", body, edition.edition_date)
    return None


def _live(s: Session) -> int:
    live = select(func.count()).select_from(PushSubscription)
    return s.scalar(live.where(PushSubscription.gone_at.is_(None)))


def _set(engine: Engine, edition_id: int, where: tuple = (), **values) -> int:
    with engine.begin() as conn:
        stmt = update(Edition).where(Edition.id == edition_id, *where).values(**values)
        return conn.execute(stmt).rowcount


def edition_notice(
    config: NewspaperConfig,
    day: date,
    log: Callable[[str], None],
    *,
    guard: bool = True,
    now: datetime | None = None,
) -> bool:
    """The automatic notice: at most one per edition, never for one already opened. Runs after
    every `generate` and at the start of every `fetch` (decision 2026-10-21), and with
    `guard=False` from the notice task (`notify --pendiente`): with notice_time set, the
    others wait until it plus NOTICE_GRACE. True when this call's notice went out."""
    at = notice_at(config, day)
    if guard and at and (now or datetime.now(UTC)) < at + NOTICE_GRACE:
        return False
    engine = open_db(config)
    with Session(engine) as s:
        edition = queries.today_edition(s, day)
        if edition is None or edition.notified_at or edition.opened_at or not _live(s):
            return False
        message = edition_message(s, config, edition)
        if message is None:
            if edition.status == EditionStatus.ready:
                log("Aviso: la edición no tiene notas esenciales; no se avisa.")
            return False
        edition_id, seen = edition.id, edition.status
    vapid = load_key(key_path(config))
    if vapid is None:
        log(f"Aviso: falta {KEY_FILE}; abre la web una vez para crearla.")
        return False
    claimed = utcnow()
    # Claim before the first POST: of two processes arriving together only one sends; an
    # opening or a new state meanwhile wins; and the funnel is emptied before a fast ping from
    # this very notice can land. SQLite and the push service share no transaction: at most one
    # notice, never exactly one.
    unchanged = (
        Edition.notified_at.is_(None),
        Edition.opened_at.is_(None),
        Edition.status == seen,
    )
    claim = {"notified_at": claimed, "delivered_at": None, "clicked_at": None, "cached_at": None}
    if _set(engine, edition_id, unchanged, **claim) != 1:
        return False
    message.n = number(claimed)
    sent = send(engine, vapid, message, log)
    # ponytail: one acceptance keeps the claim, so a phone that failed while the PC was accepted
    # waits for `newspaper notify`; retrying per subscription needs a table of its own.
    if not sent.accepted and not sent.uncertain:  # nothing went out: the next fetch tries again
        _set(engine, edition_id, (Edition.notified_at == claimed,), notified_at=None)
        return False
    return True


PENDING_EVERY = 20  # seconds between tries of the notice task
# 9 minutes, sends included: the task's 10-minute limit must never end it holding the claim,
# which no fetch would retry.
PENDING_FOR = 9 * 60
# Another process's claim this young may still be sending, and may let go if it fails.
IN_FLIGHT = timedelta(minutes=1)


def notice_pending(config: NewspaperConfig, day: date) -> bool:
    """Today's edition still owes its automatic notice: being written, or ready (or failed)
    with something to say, not yet notified nor opened, with someone to tell and a key."""
    if not key_path(config).exists():  # edition_notice already said why
        return False
    with Session(open_db(config)) as s:
        edition = queries.today_edition(s, day)
        if edition is None or edition.opened_at or not _live(s):
            return False
        if edition.notified_at:
            return utcnow() - edition.notified_at < IN_FLIGHT
        if edition.status == EditionStatus.generating:
            return True
        return edition_message(s, config, edition) is not None


def pending_notice(
    config: NewspaperConfig,
    day: date,
    log: Callable[[str], None],
    sleep=time.sleep,
    clock=time.monotonic,
) -> None:
    """The notice task at notice_time (`notify --pendiente`): the automatic notice without the
    hour guard, tried again while it is still owed, whoever failed: the Wi-Fi comes back a few
    seconds after the wake, a fetch may have claimed it and found no network, the edition may
    still be finishing."""
    start = clock()
    while True:
        try:
            if edition_notice(config, day, log, guard=False) or not notice_pending(config, day):
                return
        except OperationalError:  # locked past busy_timeout (a fetch at 07:30): try again
            log("Aviso: base ocupada; reintento.")
        if clock() - start + PENDING_EVERY >= PENDING_FOR:
            log("Aviso: no salió en 9 minutos; lo intentará el próximo fetch.")
            return
        sleep(PENDING_EVERY)


def resend(config: NewspaperConfig, day: date, log: Callable[[str], None]) -> str:
    """`newspaper notify`: today's notice, now, whatever was sent before. If nothing goes out
    the previous marks come back, so a failed test never makes `fetch` think the edition was
    never notified, nor erases its funnel."""
    engine = open_db(config)
    with Session(engine) as s:
        edition = queries.today_edition(s, day)
        if edition is None:
            return "Todavía no hay edición de hoy."
        if not _live(s):
            return "No hay suscripciones: actívalas al pie de Hoy."
        message = edition_message(s, config, edition)
        if message is None:
            return "La edición de hoy no tiene nada que avisar todavía."
        edition_id = edition.id
        before = {
            "notified_at": edition.notified_at,
            "delivered_at": edition.delivered_at,
            "clicked_at": edition.clicked_at,
            "cached_at": edition.cached_at,
        }
    vapid = load_key(key_path(config))
    if vapid is None:
        return f"Falta {KEY_FILE}: abre la web una vez para crearla."
    now = utcnow()
    _set(engine, edition_id, notified_at=now, delivered_at=None, clicked_at=None, cached_at=None)
    message.n = number(now)
    sent = send(engine, vapid, message, log)
    if not sent.accepted and not sent.uncertain:
        # Only if still ours: another notify may have succeeded meanwhile.
        _set(engine, edition_id, (Edition.notified_at == now,), **before)
    text = f"Aviso enviado a {sent.accepted} de {sent.total} suscripciones."
    return text + (f" {sent.uncertain} sin respuesta." if sent.uncertain else "")


class Keys(BaseModel):
    p256dh: str = Field(max_length=200)
    auth: str = Field(max_length=100)

    @field_validator("p256dh")
    @classmethod
    def _point(cls, value: str) -> str:
        valid_p256dh(value)
        return value

    @field_validator("auth")
    @classmethod
    def _secret(cls, value: str) -> str:
        if len(unb64(value)) != 16:
            raise ValueError("auth: se esperan 16 bytes")
        return value


class Subscription(BaseModel):
    endpoint: str = Field(max_length=2000)
    keys: Keys

    @field_validator("endpoint")
    @classmethod
    def _service(cls, value: str) -> str:
        if not allowed_endpoint(value):
            raise ValueError("endpoint: no es un servicio de push conocido")
        return value


class Alta(BaseModel):
    subscription: Subscription
    # Defaults make them optional: in pydantic 2 "| None" alone is still required.
    mobile: bool | None = None
    confirm: bool = False
    old: str | None = Field(default=None, max_length=2000)


class Baja(BaseModel):
    endpoint: str = Field(max_length=2000)


def upsert(engine: Engine, alta: Alta) -> tuple[bool, bool, int]:
    """Save a browser's subscription: (new, gone, id). With `old` (a rotation or a renewal) the
    old row is updated in place, so its id, the sid of notices already in the tray, and its
    mobile flag survive."""
    sub = alta.subscription
    with Session(engine) as s, s.begin():

        def find(endpoint):
            stmt = select(PushSubscription).where(PushSubscription.endpoint == endpoint)
            return s.scalars(stmt).first()

        row = find(sub.endpoint)
        old = find(alta.old) if alta.old and alta.old != sub.endpoint else None
        if old and row:  # the new endpoint is known already: the old row goes
            s.delete(old)
        elif old:
            row, old.endpoint, old.gone_at = old, sub.endpoint, None
        new = False
        if row is None:
            # Two first registrations of one endpoint can race: the database decides who's new.
            first = insert(PushSubscription).values(
                endpoint=sub.endpoint, p256dh="", auth="", mobile=bool(alta.mobile)
            )
            new = s.execute(first.on_conflict_do_nothing(index_elements=["endpoint"])).rowcount == 1
            row = find(sub.endpoint)
        row.p256dh, row.auth = sub.keys.p256dh, sub.keys.auth
        if alta.mobile is not None:
            row.mobile = alta.mobile
        s.flush()
        return new, row.gone_at is not None, row.id


def remove(engine: Engine, endpoint: str) -> None:
    with Session(engine) as s, s.begin():
        s.execute(delete(PushSubscription).where(PushSubscription.endpoint == endpoint))


PING_COLUMNS = {"entregado": "delivered_at", "tocado": "clicked_at", "guardada": "cached_at"}


def record_ping(
    engine: Engine,
    day: date,
    event: str,
    sid: int,
    n: int | None = None,
    at: datetime | None = None,
) -> bool:
    """The worker's 'entregado', 'guardada' (its copy saved, #20a) or 'tocado' for the edition
    of `day`, in any state. Only the phone fills the funnel: the desktop, awake at 06:20, would
    always answer first. `n` names the notice: a ping for an older one is never credited to a
    resend, and one without it (a worker from before #20a) counts. `at` is when it happened,
    for pings queued while the PC slept: the earliest wins, never before the notice. False when
    there is no edition that day."""
    column = getattr(Edition, PING_COLUMNS[event])
    with engine.begin() as conn:
        found = conn.execute(
            select(Edition.id, Edition.notified_at).where(Edition.edition_date == day)
        ).first()
        if found is None:
            return False
        edition_id, notified = found
        if n is not None and (notified is None or number(notified) != n):
            return True
        if conn.scalar(select(PushSubscription.mobile).where(PushSubscription.id == sid)):
            when = clamped(at, notified)
            stmt = update(Edition).where(
                Edition.id == edition_id, or_(column.is_(None), column > when)
            )
            conn.execute(stmt.values({column: when}))
    return True
