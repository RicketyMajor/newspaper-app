from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field


class FeedConfig(BaseModel):
    name: str
    url: str
    type: Literal["rss"] = "rss"  # RSS 2.0, Atom and RDF; other connectors come later
    section: str
    language: str = "es"
    enabled: bool = True


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
