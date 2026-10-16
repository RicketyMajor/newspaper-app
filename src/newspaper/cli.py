"""Newspaper command-line interface."""

from __future__ import annotations

import functools
import shutil
import sys
import tomllib
from datetime import datetime
from pathlib import Path

import click
from pydantic import ValidationError
from rich.console import Console
from rich.table import Table
from sqlalchemy.orm import Session

from . import schedule
from .config.loader import (
    EXAMPLE_CONFIG,
    append_feed,
    default_config_path,
    find_config_path,
    load_config,
)
from .config.models import FeedConfig, NewspaperConfig
from .edition.today import local_today, maybe_spawn_generate
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


def _log_path(config: NewspaperConfig) -> Path:
    return Path(config.storage.database_path).expanduser().parent / "generate.log"


def _log(config: NewspaperConfig, message: str, style: str = "dim") -> None:
    """Show a progress message and append it, timestamped, to generate.log."""
    # markup=False: titles and errors may contain "[/...]", which Rich would parse.
    console.print(message, style=style, markup=False, highlight=False)
    path = _log_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    # The scheduled tasks run under pythonw (no console): the log is the only trace they leave.
    # ponytail: append-only, ~3 KB per edition; rotate it if it ever gets in the way.
    with path.open("a", encoding="utf-8") as f:  # one entry per line (httpx errors span two)
        f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S}  {' '.join(message.splitlines())}\n")


@main.command()
@click.option("--force", is_flag=True, help="Regenera la edición de hoy aunque ya esté lista.")
@click.pass_context
def generate(ctx: click.Context, force: bool) -> None:
    """Prepara la edición de hoy."""
    from .edition.generate import generate as run_generate  # heavy: keep the readers fast

    config = _load(ctx)
    log = functools.partial(_log, config)
    log(f"Preparando la edición del {local_today(config):%d-%m-%Y}…", style="none")
    with schedule.keep_awake():
        result = run_generate(config, force=force, log=log)
    log(
        result.message,
        style={"ready": "green", "failed": "red", "skipped": "yellow"}[result.status],
    )
    if result.status == "failed":
        raise SystemExit(1)


@main.command()
@click.pass_context
def fetch(ctx: click.Context) -> None:
    """Descarga las fuentes y guarda los artículos nuevos, sin redactar."""
    from .edition.generate import ingest  # heavy: keep the readers fast
    from .ingestion import rss

    config = _load(ctx)
    try:
        new, errors = ingest(open_db(config), config, rss.make_client())
    except RuntimeError as e:
        _log(config, f"Descarga: {e}", style="red")
        raise SystemExit(1) from e
    for error in errors:
        _log(config, f"Descarga: {error}", style="yellow")
    console.print(f"Artículos nuevos: {new}")


@main.command()
@click.pass_context
def read(ctx: click.Context) -> None:
    """Abre el lector de terminal."""
    from .tui.app import NewspaperApp

    config = _load(ctx)
    _recover(config)
    NewspaperApp(config).run()


@main.command()
@click.option("--port", default=8765, show_default=True)
@click.option("--no-browser", is_flag=True, help="No abrir el navegador.")
@click.pass_context
def web(ctx: click.Context, port: int, no_browser: bool) -> None:
    """Abre el lector web local en el navegador."""
    from .web.app import serve

    config = _load(ctx)
    _recover(config)
    serve(config, port=port, open_browser=not no_browser)


def _recover(config: NewspaperConfig) -> None:
    if maybe_spawn_generate(config):
        console.print("Preparando la edición de hoy en segundo plano…")


@main.group(name="schedule")
def schedule_group() -> None:
    """Programa la edición diaria en el Programador de tareas de Windows."""


def _powershell(script: str) -> None:
    if schedule.sys.platform != "win32":
        raise click.ClickException("La programación automática solo está soportada en Windows.")
    result = schedule.run_powershell(script)
    if result.returncode:
        raise click.ClickException(result.stderr.strip() or "PowerShell devolvió un error.")


@schedule_group.command()
@click.pass_context
def install(ctx: click.Context) -> None:
    """Crea (o actualiza) la tarea diaria y la descarga cada 30 minutos."""
    config = _load(ctx)
    _powershell(schedule.install_script(config))
    hour = config.general.edition_hour
    console.print(
        f"Tarea '{schedule.TASK_NAME}' lista: cada día a las {hour:02d}:00, "
        "o en cuanto enciendas el equipo si estaba apagado."
    )
    console.print(
        f"Tarea '{schedule.FETCH_TASK_NAME}' lista: descarga las fuentes cada "
        f"{schedule.FETCH_EVERY_MINUTES} minutos."
    )


@schedule_group.command()
def uninstall() -> None:
    """Elimina las tareas programadas."""
    _powershell(schedule.uninstall_script())
    console.print(f"Tareas '{schedule.TASK_NAME}' y '{schedule.FETCH_TASK_NAME}' eliminadas.")


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
            undrafted = sum(not n.drafted for n in latest.notes)
            if undrafted:
                console.print(
                    f"{undrafted} sin redactar (detalle en {_log_path(config)})", style="yellow"
                )
        else:
            console.print("Todavía no hay ediciones listas.")
