from typing import Literal
from pydantic import BaseModel, Field
from pathlib import Path

class FeedConfig(BaseModel):
    name: str
    url: str
    type: Literal["rss", "hackernews", "scraper", "newsapi", "github"]
    section: str
    language: str = "en"
    enabled: bool = True

class LLMConfig(BaseModel):
    backend: str = "ollama"
    model: str = "llama3.1:8b"
    base_url: str | None = None
    fallback_backend: str | None = None
    fallback_model: str | None = None

class EmbeddingsConfig(BaseModel):
    model: str = "all-MiniLM-L6-v2"
    device: str = "cpu"

class StorageConfig(BaseModel):
    database_path: Path

class GeneralConfig(BaseModel):
    edition_name: str = "My Newspaper"
    language: str = "en"
    timezone: str = "UTC"
    edition_hour: int = 5

class NewspaperConfig(BaseModel):
    general: GeneralConfig = Field(default_factory=GeneralConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    embeddings: EmbeddingsConfig = Field(default_factory=EmbeddingsConfig)
    storage: StorageConfig
    feeds: list[FeedConfig] = Field(default_factory=list)
