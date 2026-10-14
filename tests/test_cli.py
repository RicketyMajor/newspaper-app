import re
from datetime import date

from click.testing import CliRunner
from sqlalchemy.orm import Session

from newspaper.cli import main
from newspaper.edition.generate import GenerateResult
from newspaper.storage.models import Note


def write_config(tmp_path, db_path):
    path = tmp_path / "newspaper.toml"
    path.write_text(
        f'[storage]\ndatabase_path = "{db_path.as_posix()}"\n\n'
        '[[feeds]]\nname = "CIPER"\nurl = "https://ciper.test/feed"\nsection = "Chile"\n',
        encoding="utf-8",
    )
    return path


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


def test_status_counts_undrafted_notes(tmp_path, engine, seeded):
    with Session(engine) as s, s.begin():
        s.get(Note, seeded["world"]).drafted = False
    config = write_config(tmp_path, tmp_path / "newspaper.db")  # the `engine` fixture's database

    result = CliRunner().invoke(main, ["--config", str(config), "status"])

    assert result.exit_code == 0, result.output
    assert "1 sin redactar (detalle en" in result.output
