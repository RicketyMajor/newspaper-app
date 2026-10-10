"""Newspaper CLI — the main command-line interface.

Usage:
    newspaper generate     # Run the full ingestion pipeline
    newspaper read         # Open the TUI reader
    newspaper add-feed     # Add a new feed interactively
    newspaper list-feeds   # Show configured feeds
    newspaper config       # Open newspaper.toml in your editor
    newspaper status       # Show last edition status
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

from .config.loader import load_config

# ponytail: redirected output on Windows may be cp1252; replace what it can't encode instead of crashing.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(errors="replace")

console = Console()


@click.group()
@click.version_option(package_name="newspaper")
def main() -> None:
    """Newspaper: your personal daily newspaper."""
    pass


@main.command()
def generate() -> None:
    """Run the full ingestion → processing → edition pipeline."""
    try:
        config = load_config()
    except FileNotFoundError as e:
        console.print(f"[red]Error:[/red] {e}")
        console.print("Run [bold]newspaper config[/bold] to create a configuration file.")
        raise SystemExit(1)

    console.print(f"\n[bold blue]🗞️ {config.general.edition_name}[/bold blue]")
    console.print(f"   Timezone: {config.general.timezone}")
    console.print(f"   Feeds: {len([f for f in config.feeds if f.enabled])} active")
    console.print(f"   LLM: {config.llm.backend} ({config.llm.model})")
    console.print(f"   Embeddings: {config.embeddings.model} on {config.embeddings.device}")
    console.print()

    active_feeds = [f for f in config.feeds if f.enabled]
    if not active_feeds:
        console.print("[yellow]Warning:[/yellow] No active feeds configured.")
        return

    console.print("[bold]Generating edition...[/bold]")
    console.print("[dim]Pipeline not yet implemented. Coming in Phase 2.[/dim]")


@main.command()
def read() -> None:
    """Open the TUI reader with the latest edition."""
    console.print("[bold blue]🗞️ Opening Newspaper TUI...[/bold blue]")
    console.print("[dim]TUI not yet implemented. Coming in Phase 4.[/dim]")


@main.command(name="add-feed")
def add_feed() -> None:
    """Add a new feed interactively."""
    console.print("[bold blue]🗞️ Add a new feed[/bold blue]\n")

    name = click.prompt("Feed name")
    url = click.prompt("Feed URL")
    feed_type = click.prompt(
        "Feed type",
        type=click.Choice(["rss", "hackernews", "scraper", "newsapi", "github"]),
        default="rss",
    )
    section = click.prompt("Section (e.g. Tech, World, Science)")
    language = click.prompt("Language", default="en", type=click.Choice(["en", "es"]))

    # Generate TOML block
    toml_block = f"""
[[feeds]]
name = "{name}"
url = "{url}"
type = "{feed_type}"
section = "{section}"
language = "{language}"
enabled = true
"""

    console.print("\n[bold]Add this to your newspaper.toml:[/bold]")
    console.print(f"[green]{toml_block}[/green]")

    if click.confirm("Append to newspaper.toml now?", default=True):
        config_path = _find_config_path()
        if config_path:
            with open(config_path, "a") as f:
                f.write(toml_block)
            console.print(f"[green]✓[/green] Appended to {config_path}")
        else:
            console.print("[yellow]Could not find newspaper.toml. Please add manually.[/yellow]")


@main.command(name="list-feeds")
def list_feeds() -> None:
    """List all configured feeds."""
    try:
        config = load_config()
    except FileNotFoundError as e:
        console.print(f"[red]Error:[/red] {e}")
        raise SystemExit(1)

    table = Table(title="📡 Configured Feeds", show_lines=True)
    table.add_column("#", style="dim", width=3)
    table.add_column("Name", style="bold")
    table.add_column("Type", style="cyan")
    table.add_column("Section", style="magenta")
    table.add_column("Language")
    table.add_column("Enabled", justify="center")

    for i, feed in enumerate(config.feeds, 1):
        enabled = "[green]✓[/green]" if feed.enabled else "[red]✗[/red]"
        table.add_row(
            str(i),
            feed.name,
            feed.type,
            feed.section,
            feed.language,
            enabled,
        )

    console.print(table)


@main.command()
def config() -> None:
    """Open newspaper.toml in your editor."""
    config_path = _find_config_path()

    if config_path is None:
        console.print("[yellow]No newspaper.toml found.[/yellow]")
        if click.confirm("Create one in the current directory?", default=True):
            # Copy the example config
            example = Path(__file__).parent.parent.parent / "newspaper.toml"
            if example.exists():
                import shutil
                shutil.copy(example, Path.cwd() / "newspaper.toml")
                config_path = Path.cwd() / "newspaper.toml"
                console.print(f"[green]✓[/green] Created {config_path}")
            else:
                console.print("[red]Could not find example config.[/red]")
                return
        else:
            return

    editor = os.environ.get("EDITOR", "notepad" if sys.platform == "win32" else "vim")
    console.print(f"Opening {config_path} with {editor}...")
    subprocess.run([editor, str(config_path)])


@main.command()
def status() -> None:
    """Show the status of the latest edition."""
    try:
        config = load_config()
    except FileNotFoundError as e:
        console.print(f"[red]Error:[/red] {e}")
        raise SystemExit(1)

    console.print(f"\n[bold blue]🗞️ {config.general.edition_name}[/bold blue]")
    console.print(f"   Database: {config.storage.database_path}")

    db_path = Path(config.storage.database_path).expanduser()
    if db_path.exists():
        size_mb = db_path.stat().st_size / (1024 * 1024)
        console.print(f"   DB Size: {size_mb:.1f} MB")
        console.print("[dim]   Edition status query not yet implemented.[/dim]")
    else:
        console.print("   [yellow]Database not yet created. Run `newspaper generate` first.[/yellow]")


def _find_config_path() -> Path | None:
    """Find the newspaper.toml config file.

    Searches CWD first, then ~/.newspaper/.

    Returns:
        Path to the config file, or None.
    """
    cwd_path = Path.cwd() / "newspaper.toml"
    if cwd_path.exists():
        return cwd_path

    home_path = Path.home() / ".newspaper" / "newspaper.toml"
    if home_path.exists():
        return home_path

    return None


if __name__ == "__main__":
    main()
