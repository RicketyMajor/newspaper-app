import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, model_validator


class FeedConfig(BaseModel):
    name: str
    url: str
    type: Literal["rss"] = "rss"  # RSS 2.0, Atom and RDF; other connectors come later
    section: str
    language: str = "es"
    enabled: bool = True
    # Regexes searched (case-insensitive) in each article's URL and title.
    exclude: list[str] = []  # drop the article
    route: dict[str, str] = {}  # pattern -> section; the first match wins

    @model_validator(mode="after")
    def _patterns_compile(self) -> "FeedConfig":
        for pattern in [*self.exclude, *self.route]:
            try:
                re.compile(pattern)
            except re.error as e:
                raise ValueError(f"{self.name}: patrón inválido {pattern!r} ({e})") from None
        return self

    def section_for(self, title: str, url: str) -> str | None:
        """Section for one of this feed's articles; None when `exclude` drops it."""

        def hit(pattern: str) -> bool:
            return any(re.search(pattern, text, re.IGNORECASE) for text in (url, title))

        if any(hit(p) for p in self.exclude):
            return None
        return next((s for p, s in self.route.items() if hit(p)), self.section)


class GeneralConfig(BaseModel):
    edition_name: str = "Newspaper"
    language: str = "es"
    timezone: str = "America/Santiago"
    edition_hour: int = Field(default=6, ge=0, le=23)


class LLMConfig(BaseModel):
    base_url: str = "http://localhost:11434"
    model: str = "gemma3:12b"
    embed_model: str = "bge-m3"
    timeout_seconds: float = 300


class EditionConfig(BaseModel):
    window_hours: int = 36
    front_page_size: int = 5
    max_notes_per_section: int = 10
    cluster_threshold: float = 0.75


class StorageConfig(BaseModel):
    database_path: Path = Path("~/.newspaper/newspaper.db")


class NewspaperConfig(BaseModel):
    general: GeneralConfig = Field(default_factory=GeneralConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    edition: EditionConfig = Field(default_factory=EditionConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    feeds: list[FeedConfig] = Field(default_factory=list)
    path: Path | None = Field(default=None, exclude=True)  # file it was loaded from

    @property
    def active_feeds(self) -> list[FeedConfig]:
        return [f for f in self.feeds if f.enabled]

    @property
    def sections(self) -> list[str]:
        """Section names in order of first appearance among active feeds."""
        return list(dict.fromkeys(f.section for f in self.active_feeds))
