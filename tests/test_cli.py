import logging
import re
from contextlib import contextmanager
from datetime import date
from types import SimpleNamespace

import httpx
from click.testing import CliRunner
from sqlalchemy.orm import Session

from newspaper.cli import main
from newspaper.edition.generate import GenerateResult
from newspaper.storage.models import Note


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


def test_status_counts_undrafted_notes(tmp_path, engine, seeded):
    with Session(engine) as s, s.begin():
        s.get(Note, seeded["world"]).drafted = False
    config = write_config(tmp_path, tmp_path / "newspaper.db")  # the `engine` fixture's database

    result = CliRunner().invoke(main, ["--config", str(config), "status"])

    assert result.exit_code == 0, result.output
    assert "1 sin redactar (detalle en" in result.output


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
    def keep_awake():  # like the real one, it lets go when uvicorn raises
        calls.awake.append("on")
        try:
            yield
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
        seen.update(kwargs)
        logging.getLogger("uvicorn.error").error("Exception in ASGI application")

    result, calls = run_web(tmp_path, monkeypatch, uvicorn_run, "--no-browser")

    assert result.exit_code == 0, result.output
    # log_config=None: uvicorn's default calls sys.stdout.isatty(), and pythonw has no stdout.
    assert seen == {"host": "127.0.0.1", "port": 8765, "log_config": None}
    assert (calls.recovered, calls.opened, calls.awake) == (1, [], ["on", "off"])
    log = (tmp_path / "data" / "web.log").read_text(encoding="utf-8")
    assert "uvicorn.error: Exception in ASGI application" in log


def test_web_on_a_taken_port_points_to_the_running_server(tmp_path, monkeypatch):
    def uvicorn_run(app, **kwargs):
        raise SystemExit(3)  # uvicorn's STARTUP_FAILURE, after logging [Errno 10048]

    result, calls = run_web(tmp_path, monkeypatch, uvicorn_run)

    assert result.exit_code == 3
    assert "Si ya hay un servidor" in result.output
    assert calls.opened == ["http://127.0.0.1:8765/"]
    assert (calls.recovered, calls.awake) == (1, ["on", "off"])
