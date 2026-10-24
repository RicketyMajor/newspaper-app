"""Newspaper command-line interface."""

from __future__ import annotations

import functools
import logging
import shutil
import sys
import tomllib
from datetime import UTC, date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import click
from pydantic import ValidationError
from rich.console import Console
from rich.table import Table
from sqlalchemy import select
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
from .storage.models import Edition, PushSubscription

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


def _log_path(config: NewspaperConfig, name: str = "generate.log") -> Path:
    return Path(config.storage.database_path).expanduser().parent / name


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


def _notify(config: NewspaperConfig, day: date) -> None:
    """The day's automatic notice (#17). It never fails the command that calls it."""
    try:
        from . import notify  # heavy (cryptography): keep the readers fast

        notify.edition_notice(config, day, functools.partial(_log, config))
    except Exception as e:
        _log(config, f"Aviso: {str(e) or type(e).__name__}", style="red")


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
        # Every result, skipped too: an edition whose process died before notifying gets its
        # notice now. Its own date, so a run past midnight notifies about its edition.
        _notify(config, result.edition_date)
    if result.status == "failed":
        raise SystemExit(1)


@main.command()
@click.pass_context
def fetch(ctx: click.Context) -> None:
    """Descarga las fuentes y guarda los artículos nuevos, sin redactar."""
    from .edition.generate import ingest  # heavy: keep the readers fast
    from .ingestion import rss

    config = _load(ctx)
    # First, the pending notice (no network at 06:20, a refused POST): before downloading,
    # because the scheduler kills a fetch stuck on a feed.
    _notify(config, local_today(config))
    try:
        new, errors = ingest(open_db(config), config, rss.make_client())
    except RuntimeError as e:
        _log(config, f"Descarga: {e}", style="red")
        raise SystemExit(1) from e
    for error in errors:
        _log(config, f"Descarga: {error}", style="yellow")
    console.print(f"Artículos nuevos: {new}")


@main.command(name="notify")
@click.pass_context
def notify_command(ctx: click.Context) -> None:
    """Manda ahora el aviso de la edición de hoy (para probarlo o reintentarlo)."""
    from . import notify

    config = _load(ctx)
    text = notify.resend(config, local_today(config), functools.partial(_log, config))
    console.print(text, markup=False)


@main.command()
@click.pass_context
def read(ctx: click.Context) -> None:
    """Abre el lector de terminal."""
    from .tui.app import NewspaperApp

    config = _load(ctx)
    _recover(config)
    NewspaperApp(config).run()


@main.command()
@click.option("--port", default=schedule.WEB_PORT, show_default=True)
@click.option("--no-browser", is_flag=True, help="No abrir el navegador.")
@click.pass_context
def web(ctx: click.Context, port: int, no_browser: bool) -> None:
    """Abre el lector web local en el navegador."""
    from .web.app import serve

    config = _load(ctx)
    log = _log_path(config, "web.log")
    log.parent.mkdir(parents=True, exist_ok=True)
    # The scheduled task runs under pythonw, with no console: startup errors, bind errors and
    # 500s (with their tracebacks) land here. ponytail: append-only and errors only; rotate it
    # if it grows.
    logging.basicConfig(
        filename=log,
        encoding="utf-8",
        level=logging.WARNING,
        format="%(asctime)s %(name)s: %(message)s",
        force=True,
    )
    logging.getLogger("newspaper.schedule").setLevel(logging.INFO)  # each sleep decision (#22)
    try:
        _recover(config)
        serve(config, port=port, open_browser=not no_browser)
    except SystemExit as e:
        # ponytail: uvicorn's STARTUP_FAILURE; in practice the port is taken, web.log says.
        if e.code == 3:
            click.echo(
                f"No se pudo usar el puerto {port}. Si ya hay un servidor (la tarea "
                f"{schedule.WEB_TASK_NAME}), está en http://127.0.0.1:{port}/. Detalle en {log}"
            )
        raise
    except Exception:
        logging.exception("newspaper web se detuvo")
        raise


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
    """Crea (o actualiza) la tarea diaria, la descarga cada 30 minutos y el servidor web."""
    config = _load(ctx)
    _powershell(schedule.install_script(config))
    hour = config.general.edition_hour
    console.print(
        f"Tarea '{schedule.TASK_NAME}' lista: cada día a las {hour:02d}:00 (despierta el "
        "equipo si está suspendido), o en cuanto lo enciendas si estaba apagado."
    )
    console.print(
        f"Tarea '{schedule.FETCH_TASK_NAME}' lista: descarga las fuentes cada "
        f"{schedule.FETCH_EVERY_MINUTES} minutos."
    )
    console.print(
        f"Tarea '{schedule.WEB_TASK_NAME}' lista y en marcha: la web en "
        f"http://127.0.0.1:{schedule.WEB_PORT}, "
        f"al iniciar sesión y revisada cada {schedule.FETCH_EVERY_MINUTES} minutos; mantiene el "
        "equipo despierto tras la edición y mientras lees."
    )


@schedule_group.command()
def uninstall() -> None:
    """Elimina las tareas programadas."""
    _powershell(schedule.uninstall_script())
    console.print(
        f"Tareas '{schedule.TASK_NAME}', '{schedule.FETCH_TASK_NAME}' y "
        f"'{schedule.WEB_TASK_NAME}' eliminadas."
    )


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


def _funnel(config: NewspaperConfig, edition: Edition) -> str:
    """Notice → arrived → saved → tapped (the phone) → opened → read (any device)."""
    zone = ZoneInfo(config.general.timezone)

    def at(moment: datetime | None) -> str:
        return "—" if moment is None else f"{moment.replace(tzinfo=UTC).astimezone(zone):%H:%M}"

    core, _ = queries.reading_order(edition.notes)
    opened = sum(n.opened_at is not None for n in edition.notes)
    notice = f"Aviso {at(edition.notified_at)}" if edition.notified_at else "Sin aviso"
    return (
        f"{notice} · llegó {at(edition.delivered_at)} · guardada {at(edition.cached_at)}"
        f" · tocado {at(edition.clicked_at)} · abierta {at(edition.opened_at)}"
        f" · {opened} {'nota abierta' if opened == 1 else 'notas abiertas'}"
        f" · {sum(n.is_read for n in core)} de {len(core)} esenciales leídas"
    )


def _subscriptions(s: Session) -> str:
    live = list(s.scalars(select(PushSubscription).where(PushSubscription.gone_at.is_(None))))
    if not live:
        return "Avisos: ninguna suscripción"
    word = "suscripción" if len(live) == 1 else "suscripciones"
    return f"Avisos: {len(live)} {word} ({sum(p.mobile for p in live)} en el teléfono)"


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
        shown = today or latest
        if shown is not None:
            console.print(_funnel(config, shown), markup=False, highlight=False, soft_wrap=True)
        console.print(_subscriptions(s), markup=False, highlight=False)
