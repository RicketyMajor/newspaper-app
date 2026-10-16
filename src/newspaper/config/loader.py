import json
import tomllib
from pathlib import Path

from .models import FeedConfig, NewspaperConfig

EXAMPLE_CONFIG = Path(__file__).parent.parent / "newspaper.example.toml"


def default_config_path() -> Path:
    return Path.home() / ".newspaper" / "newspaper.toml"


def find_config_path() -> Path | None:
    """./newspaper.toml first, then ~/.newspaper/newspaper.toml."""
    for path in (Path.cwd() / "newspaper.toml", default_config_path()):
        if path.exists():
            return path
    return None


def load_config(path: Path | None = None) -> NewspaperConfig:
    path = path or find_config_path()
    if path is None:
        raise FileNotFoundError("No hay newspaper.toml en este directorio ni en ~/.newspaper/")
    if not path.exists():
        raise FileNotFoundError(f"No existe el archivo de configuración: {path}")
    with open(path, "rb") as f:
        data = tomllib.load(f)
    return NewspaperConfig(**data, path=path)


def append_feed(path: Path, feed: FeedConfig) -> None:
    """Append a [[feeds]] block, refusing to write a file that would no longer parse."""
    # ponytail: json.dumps(ensure_ascii=False) emits a valid TOML basic string for these values.
    lines = ["", "[[feeds]]"]
    for key, value in feed.model_dump(exclude_defaults=True).items():  # empty rules, type…
        lines.append(f"{key} = {json.dumps(value, ensure_ascii=False)}")
    current = path.read_text(encoding="utf-8")
    updated = current.rstrip("\n") + "\n" + "\n".join(lines) + "\n"
    tomllib.loads(updated)
    path.write_text(updated, encoding="utf-8")
