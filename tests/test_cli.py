import logging
import re
from contextlib import contextmanager
from datetime import date, datetime, time
from types import SimpleNamespace

import httpx
import pytest
from click.testing import CliRunner
from sqlalchemy.orm import Session

from newspaper import schedule
from newspaper.cli import main
from newspaper.edition.generate import GenerateResult
from newspaper.storage.models import Note, PushSubscription


def write_config(tmp_path, db_path, extra=""):
    path = tmp_path / "newspaper.toml"
    path.write_text(
        f'[storage]\ndatabase_path = "{db_path.as_posix()}"\n\n'
        '[[feeds]]\nname = "CIPER"\nurl = "https://ciper.test/feed"\nsection = "Chile"\n' + extra,
        encoding="utf-8",
    )
    return path


FEED = (
    b'<?xml version="1.0"?><rss version="2.0"><channel><item><title>Senado</title>'
    b"<link>https://ciper.test/senado</link><description>x</description></item></channel></rss>"
)
BBC = '\n[[feeds]]\nname = "BBC Mundo"\nurl = "https://bbc.test/rss"\nsection = "Mundo"\n'


def serve_feeds(monkeypatch):
    def handler(request):
        if str(request.url) == "https://ciper.test/feed":
            return httpx.Response(200, content=FEED)
        return httpx.Response(404)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    monkeypatch.setattr("newspaper.ingestion.rss.make_client", lambda: client)


def test_fetch_stores_new_articles_without_logging(tmp_path, monkeypatch):
    serve_feeds(monkeypatch)
    config = write_config(tmp_path, tmp_path / "newspaper.db")
    first = CliRunner().invoke(main, ["--config", str(config), "fetch"])
    second = CliRunner().invoke(main, ["--config", str(config), "fetch"])

    assert (first.exit_code, second.exit_code) == (0, 0), first.output + second.output
    assert "Artículos nuevos: 1" in first.output and "Artículos nuevos: 0" in second.output
    assert not (tmp_path / "generate.log").exists()  # only errors are logged


def test_fetch_logs_a_failing_feed(tmp_path, monkeypatch):
    serve_feeds(monkeypatch)
    config = write_config(tmp_path, tmp_path / "newspaper.db", BBC)
    result = CliRunner().invoke(main, ["--config", str(config), "fetch"])

    assert result.exit_code == 0, result.output
    [line] = (tmp_path / "generate.log").read_text(encoding="utf-8").splitlines()
    assert re.match(r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d  Descarga: BBC Mundo: ", line)


def test_generate_appends_its_progress_to_a_log_next_to_the_database(tmp_path, monkeypatch):
    def fake_generate(config, *, force, log):
        log("Leyendo fuentes…")
        # Titles and errors are not Rich markup: an unmatched closing tag must not crash.
        log("  Intento 1 fallido (Video [/VIDEO]): Ollama 500: token repeat limit reached")
        return GenerateResult("ready", "Edición publicada con 1 notas.", date(2026, 10, 14))

    monkeypatch.setattr("newspaper.edition.generate.generate", fake_generate)
    config = write_config(tmp_path, tmp_path / "data" / "newspaper.db")
    for _ in range(2):  # a second run appends, it doesn't overwrite
        result = CliRunner().invoke(main, ["--config", str(config), "generate"])
        assert result.exit_code == 0, result.output

    lines = (tmp_path / "data" / "generate.log").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 8
    assert all(re.match(r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d  ", line) for line in lines)
    assert "Preparando la edición del" in lines[0]
    assert lines[2].endswith("(Video [/VIDEO]): Ollama 500: token repeat limit reached")
    assert lines[3].endswith("Edición publicada con 1 notas.")


def test_generate_keeps_the_pc_awake_while_it_runs(tmp_path, monkeypatch):
    awake = []

    @contextmanager
    def fake_keep_awake():
        awake.append("on")
        yield
        awake.append("off")

    def fake_generate(config, *, force, log):
        assert awake == ["on"]
        return GenerateResult("ready", "Edición publicada con 1 notas.", date(2026, 10, 16))

    monkeypatch.setattr("newspaper.schedule.keep_awake", fake_keep_awake)
    monkeypatch.setattr("newspaper.edition.generate.generate", fake_generate)
    result = CliRunner().invoke(
        main, ["--config", str(write_config(tmp_path, tmp_path / "n.db")), "generate"]
    )

    assert result.exit_code == 0, result.output
    assert awake == ["on", "off"]


@pytest.mark.parametrize(("status", "code"), [("ready", 0), ("failed", 1), ("skipped", 0)])
def test_generate_tries_the_notice_after_every_result(tmp_path, monkeypatch, status, code):
    days = []
    monkeypatch.setattr(
        "newspaper.edition.generate.generate",
        lambda config, *, force, log: GenerateResult(status, "fin", date(2026, 10, 21)),
    )
    monkeypatch.setattr(
        "newspaper.notify.edition_notice", lambda config, day, log: days.append(day)
    )

    result = CliRunner().invoke(
        main, ["--config", str(write_config(tmp_path, tmp_path / "n.db")), "generate"]
    )

    assert result.exit_code == code, result.output
    assert days == [date(2026, 10, 21)]


def test_a_failing_notice_never_changes_the_exit_code(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "newspaper.edition.generate.generate",
        lambda config, *, force, log: GenerateResult("ready", "fin", date(2026, 10, 21)),
    )

    def boom(config, day, log):
        raise RuntimeError("sin red")

    monkeypatch.setattr("newspaper.notify.edition_notice", boom)
    db = tmp_path / "data" / "n.db"

    result = CliRunner().invoke(main, ["--config", str(write_config(tmp_path, db)), "generate"])

    assert result.exit_code == 0, result.output
    assert "Aviso: sin red" in (db.parent / "generate.log").read_text(encoding="utf-8")


def test_fetch_tries_the_pending_notice_before_downloading(tmp_path, monkeypatch):
    order = []
    monkeypatch.setattr(
        "newspaper.notify.edition_notice", lambda config, day, log: order.append("aviso")
    )

    def ingest(engine, config, http):
        order.append("descarga")
        raise RuntimeError("Ninguna fuente respondió")

    monkeypatch.setattr("newspaper.edition.generate.ingest", ingest)

    result = CliRunner().invoke(
        main, ["--config", str(write_config(tmp_path, tmp_path / "n.db")), "fetch"]
    )

    assert result.exit_code == 1  # the download failed, as before
    assert order == ["aviso", "descarga"]


def test_notify_prints_what_resend_says(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "newspaper.notify.resend", lambda config, day, log: "Aviso enviado a 1 de 1 suscripciones."
    )
    result = CliRunner().invoke(
        main, ["--config", str(write_config(tmp_path, tmp_path / "n.db")), "notify"]
    )
    assert result.exit_code == 0 and "Aviso enviado a 1 de 1 suscripciones." in result.output


def test_status_counts_undrafted_notes(tmp_path, engine, seeded):
    with Session(engine) as s, s.begin():
        s.get(Note, seeded["world"]).drafted = False
    config = write_config(tmp_path, tmp_path / "newspaper.db")  # the `engine` fixture's database

    result = CliRunner().invoke(main, ["--config", str(config), "status"])

    assert result.exit_code == 0, result.output
    assert "1 sin redactar (detalle en" in result.output


def test_status_shows_the_funnel_and_the_subscriptions(tmp_path, engine, seeded):
    with Session(engine) as s, s.begin():
        note = s.get(Note, seeded["front"])
        note.opened_at = datetime(2026, 10, 10, 10, 12)
        note.edition.notified_at = datetime(2026, 10, 10, 9, 21)  # UTC: 06:21 in Santiago
        note.edition.delivered_at = datetime(2026, 10, 10, 10, 10)
        s.add(
            PushSubscription(
                endpoint="https://fcm.googleapis.com/x", p256dh="k", auth="a", mobile=True
            )
        )
    config = write_config(tmp_path, tmp_path / "newspaper.db")

    result = CliRunner().invoke(main, ["--config", str(config), "status"])

    assert result.exit_code == 0, result.output
    assert (
        "Aviso 06:21 · llegó 07:10 · guardada — · tocado — · abierta — · 1 nota abierta"
        " · 0 de 2 esenciales leídas"
    ) in result.output
    assert "Avisos: 1 suscripción (1 en el teléfono)" in result.output


def run_web(tmp_path, monkeypatch, uvicorn_run, *args):
    """`newspaper web` with uvicorn, the browser, recovery and keep_awake faked."""
    calls = SimpleNamespace(recovered=0, opened=[], awake=[])

    def recover(config):
        calls.recovered += 1
        return False

    class NowTimer:  # the browser opens at once instead of a second later
        def __init__(self, _delay, fn, args):
            self.fn, self.args = fn, args

        def start(self):
            self.fn(*self.args)

    @contextmanager
    def keep_awake(needed, sleep=None):  # like the real one, it lets go when uvicorn raises
        calls.needed, calls.sleep, calls.recheck = needed, sleep, lambda: None
        calls.awake.append("on")
        try:
            yield calls.recheck
        finally:
            calls.awake.append("off")

    monkeypatch.setattr("newspaper.cli.maybe_spawn_generate", recover)
    monkeypatch.setattr("threading.Timer", NowTimer)
    monkeypatch.setattr("webbrowser.open", calls.opened.append)
    monkeypatch.setattr("newspaper.schedule.keep_awake", keep_awake)
    monkeypatch.setattr("uvicorn.run", uvicorn_run)
    config = write_config(tmp_path, tmp_path / "data" / "newspaper.db")
    result = CliRunner().invoke(main, ["--config", str(config), "web", *args])
    for handler in logging.root.handlers[:]:  # serve() logs to web.log through the root logger
        handler.close()
        logging.root.removeHandler(handler)
    return result, calls


def test_web_serves_awake_on_localhost_and_logs_errors_to_a_file(tmp_path, monkeypatch):
    seen = {}

    def uvicorn_run(app, **kwargs):
        seen.update(kwargs, app=app)
        logging.getLogger("uvicorn.error").error("Exception in ASGI application")
        logging.getLogger("uvicorn.error").info("Started server process")  # noise: not logged
        logging.getLogger("newspaper.schedule").info("Suelto el PC: …: lo duermo")

    result, calls = run_web(tmp_path, monkeypatch, uvicorn_run, "--no-browser")

    assert result.exit_code == 0, result.output
    # log_config=None: uvicorn's default calls sys.stdout.isatty(), and pythonw has no stdout.
    app = seen.pop("app")
    assert seen == {"host": "127.0.0.1", "port": 8765, "log_config": None}
    assert (calls.recovered, calls.opened, calls.awake) == (1, [], ["on", "off"])
    assert calls.needed is app.state.awake  # awake only after an edition or a visit
    assert app.state.recheck is calls.recheck  # and a visit asks again at once
    # and the end of a hold may sleep a PC nobody touched; our wake is the edition's hour
    assert calls.sleep.func is schedule.sleep_if_unattended
    assert calls.sleep.args == ([time(6)],)
    log = (tmp_path / "data" / "web.log").read_text(encoding="utf-8")
    assert "uvicorn.error: Exception in ASGI application" in log
    assert "newspaper.schedule: Suelto el PC: …: lo duermo" in log  # each decision, to calibrate
    assert "Started server process" not in log


def test_web_on_a_taken_port_points_to_the_running_server(tmp_path, monkeypatch):
    def uvicorn_run(app, **kwargs):
        raise SystemExit(3)  # uvicorn's STARTUP_FAILURE, after logging [Errno 10048]

    result, calls = run_web(tmp_path, monkeypatch, uvicorn_run)

    assert result.exit_code == 3
    assert "Si ya hay un servidor" in result.output
    assert "está en http://127.0.0.1:8765/." in result.output
    assert calls.opened == ["http://127.0.0.1:8765/"]
    assert (calls.recovered, calls.awake) == (1, ["on", "off"])


def test_web_logs_a_startup_error_that_pythonw_would_otherwise_lose(tmp_path, monkeypatch):
    def uvicorn_run(app, **kwargs):
        raise RuntimeError("database is locked")

    result, _ = run_web(tmp_path, monkeypatch, uvicorn_run, "--no-browser")

    assert result.exit_code == 1
    log = (tmp_path / "data" / "web.log").read_text(encoding="utf-8")
    assert "RuntimeError: database is locked" in log  # with its traceback
