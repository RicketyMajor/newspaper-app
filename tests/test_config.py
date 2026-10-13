from pathlib import Path

import pytest
from click.testing import CliRunner
from pydantic import ValidationError

from newspaper.cli import main
from newspaper.config.loader import EXAMPLE_CONFIG, append_feed, find_config_path, load_config
from newspaper.config.models import FeedConfig, NewspaperConfig


def test_example_config_loads():
    cfg = load_config(EXAMPLE_CONFIG)
    assert len(cfg.active_feeds) == 5
    assert cfg.sections == ["Chile", "Mundo", "Ciencia"]
    assert cfg.general.timezone == "America/Santiago"
    assert cfg.path == EXAMPLE_CONFIG


def test_defaults():
    cfg = NewspaperConfig()
    assert cfg.llm.model == "gemma3:12b"
    assert cfg.llm.embed_model == "bge-m3"
    assert cfg.edition.front_page_size == 5
    assert cfg.general.language == "es"


def test_edition_hour_must_be_an_hour():
    with pytest.raises(ValidationError):
        NewspaperConfig(general={"edition_hour": 24})


def test_only_rss_feeds_are_supported():
    with pytest.raises(ValidationError):
        FeedConfig(name="HN", url="https://x", type="hackernews", section="Tech")


def test_append_feed_escapes_and_stays_valid(tmp_path):
    path = tmp_path / "newspaper.toml"
    path.write_text('[general]\nedition_name = "X"\n', encoding="utf-8")
    feed = FeedConfig(name='Diario "El \\ Sur"\nbis', url="https://e.cl/rss", section="Región")

    append_feed(path, feed)
    append_feed(path, feed)

    assert load_config(path).feeds == [feed, feed]
    text = path.read_text(encoding="utf-8")
    assert "exclude" not in text and "route" not in text


def test_feed_rules_pick_the_section():
    feed = FeedConfig(
        name="Coop",
        url="https://x",
        section="Chile",
        exclude=["/deportes/", "^marcador virtual"],
        route={"/mundo/": "Mundo", "/mundo/europa/": "Europa"},
    )
    pais, mundo = "https://c.cl/noticias/pais/x", "https://c.cl/noticias/mundo/europa/x"
    assert feed.section_for("Senado aprueba", pais) == "Chile"
    assert feed.section_for("Gol", "https://c.cl/noticias/DEPORTES/futbol/x") is None
    assert feed.section_for("Marcador Virtual: U vs. Ñublense", pais) is None  # title
    assert feed.section_for("Ciberataque", mundo) == "Mundo"  # first route wins
    assert feed.section_for("Gol", "https://c.cl/noticias/mundo/deportes/x") is None


def test_invalid_feed_pattern_is_reported(tmp_path):
    path = tmp_path / "newspaper.toml"
    path.write_text(
        '[[feeds]]\nname = "Coop"\nurl = "https://x"\nsection = "Chile"\n'
        'route = { "(mundo" = "Mundo" }\n',
        encoding="utf-8",
    )
    with pytest.raises(ValidationError, match=r"Coop: patrón inválido '\(mundo'"):
        load_config(path)


def test_find_config_prefers_cwd_then_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".newspaper").mkdir(parents=True)
    (home / ".newspaper" / "newspaper.toml").write_text("", encoding="utf-8")
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.chdir(cwd)

    assert find_config_path() == home / ".newspaper" / "newspaper.toml"
    (cwd / "newspaper.toml").write_text("", encoding="utf-8")
    assert find_config_path() == cwd / "newspaper.toml"


def test_missing_config_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(FileNotFoundError):
        load_config()


def test_cli_list_feeds_uses_config_option():
    result = CliRunner().invoke(main, ["--config", str(EXAMPLE_CONFIG), "list-feeds"])
    assert result.exit_code == 0, result.output
    assert "CIPER" in result.output
