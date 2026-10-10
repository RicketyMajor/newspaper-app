"""Newspaper command-line interface."""

from __future__ import annotations

import shutil
import sys
import tomllib
from pathlib import Path

import click
from pydantic import ValidationError
from rich.console import Console
from rich.table import Table
from sqlalchemy.orm import Session

from .config.loader import (
    EXAMPLE_CONFIG,
    append_feed,
    default_config_path,
    find_config_path,
    load_config,
)
from .config.models import FeedConfig, NewspaperConfig
from .edition.generate import generate as run_generate
from .edition.generate import local_today
from .storage import queries
from .storage.database import open_db

# ponytail: redirected output on Windows may be cp1252; replace unencodable chars, don't crash.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(errors="replace")

console = Console()


@click.group()
@click.version_option(package_name="newspaper")
@click.option(
    "--config",
    "config_path",
    type=click.Path(path_type=Path, dir_okay=False),
    help="Ruta a newspaper.toml (por defecto ./newspaper.toml o ~/.newspaper/newspaper.toml).",
)
@click.pass_context
def main(ctx: click.Context, config_path: Path | None) -> None:
    """Newspaper: tu diario personal."""
    ctx.obj = config_path.resolve() if config_path else None


def _load(ctx: click.Context) -> NewspaperConfig:
    try:
        return load_config(ctx.obj)
    except FileNotFoundError as e:
        raise click.ClickException(f"{e}\nCrea una con `newspaper config`.") from e
    except (ValidationError, tomllib.TOMLDecodeError) as e:
        raise click.ClickException(f"Configuración inválida: {e}") from e


@main.command()
@click.option("--force", is_flag=True, help="Regenera la edición de hoy aunque ya esté lista.")
@click.pass_context
def generate(ctx: click.Context, force: bool) -> None:
    """Prepara la edición de hoy."""
    config = _load(ctx)
    console.print(f"Preparando la edición del {local_today(config):%d-%m-%Y}…")
    result = run_generate(config, force=force, log=lambda msg: console.print(f"[dim]{msg}[/dim]"))
    color = {"ready": "green", "failed": "red", "skipped": "yellow"}[result.status]
    console.print(f"[{color}]{result.message}[/{color}]")
    if result.status == "failed":
        raise SystemExit(1)


@main.command()
@click.pass_context
def read(ctx: click.Context) -> None:
    """Abre el lector de terminal."""
    _load(ctx)
    console.print("[dim]El lector de terminal todavía no está implementado.[/dim]")


@main.command(name="add-feed")
@click.pass_context
def add_feed(ctx: click.Context) -> None:
    """Añade una fuente RSS a newspaper.toml."""
    config = _load(ctx)
    feed = FeedConfig(
        name=click.prompt("Nombre"),
        url=click.prompt("URL del feed RSS"),
        section=click.prompt("Sección", default=config.sections[0] if config.sections else None),
        language=click.prompt("Idioma de la fuente", default=config.general.language),
    )
    assert config.path is not None
    append_feed(config.path, feed)
    console.print(f"Añadida [bold]{feed.name}[/bold] a {config.path}")


@main.command(name="list-feeds")
@click.pass_context
def list_feeds(ctx: click.Context) -> None:
    """Lista las fuentes configuradas."""
    config = _load(ctx)
    table = Table(title="Fuentes", show_lines=True)
    for column in ("#", "Nombre", "Sección", "Idioma", "Activa", "URL"):
        table.add_column(column)
    for i, feed in enumerate(config.feeds, 1):
        active = "sí" if feed.enabled else "no"
        table.add_row(str(i), feed.name, feed.section, feed.language, active, feed.url)
    console.print(table)


@main.command()
@click.pass_context
def config(ctx: click.Context) -> None:
    """Abre newspaper.toml en tu editor (lo crea desde el ejemplo si no existe)."""
    path = ctx.obj or find_config_path()
    if path is None:
        path = default_config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(EXAMPLE_CONFIG, path)
        console.print(f"Creada {path}")
    click.edit(filename=str(path))


@main.command()
@click.pass_context
def status(ctx: click.Context) -> None:
    """Muestra el estado de la edición de hoy y la última disponible."""
    config = _load(ctx)
    with Session(open_db(config)) as s:
        today = queries.today_edition(s, local_today(config))
        latest = queries.latest_ready_edition(s)
        if today is None:
            console.print("Hoy: sin generar.")
        else:
            console.print(f"Hoy: {today.status.value}")
            if today.error:
                console.print(f"[red]{today.error}[/red]")
            if today.feed_errors:
                console.print(f"[yellow]Fuentes con error:\n{today.feed_errors}[/yellow]")
        if latest:
            notes = len(latest.notes)
            console.print(f"Última edición lista: {latest.edition_date} ({notes} notas)")
        else:
            console.print("Todavía no hay ediciones listas.")


if __name__ == "__main__":
    main()
