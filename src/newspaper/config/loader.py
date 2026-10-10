import tomllib
from pathlib import Path
import os
from .models import NewspaperConfig

def load_config(path: Path | None = None) -> NewspaperConfig:
    """Loads configuration from a TOML file."""
    if path is None:
        # Check CWD first
        cwd_path = Path.cwd() / "newspaper.toml"
        if cwd_path.exists():
            path = cwd_path
        else:
            # Fallback to ~/.newspaper/newspaper.toml
            home_path = Path.home() / ".newspaper" / "newspaper.toml"
            if home_path.exists():
                path = home_path
            else:
                raise FileNotFoundError("Could not find newspaper.toml in CWD or ~/.newspaper/")
                
    if not path.exists():
        raise FileNotFoundError(f"Configuration file not found: {path}")
        
    with open(path, "rb") as f:
        data = tomllib.load(f)
        
    return NewspaperConfig(**data)
