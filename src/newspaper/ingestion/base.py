"""Base adapter interface for news source ingestion.

All source-specific adapters (RSS, Hacker News, Scraper, etc.) must implement
this abstract base class.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class RawArticle:
    """A normalized article as produced by any adapter.

    This is the common interchange format between adapters and the storage layer.
    """

    title: str
    url: str
    summary: str = ""
    author: str = ""
    published_at: datetime | None = None
    source_name: str = ""
    language: str = "en"
    tags: list[str] = field(default_factory=list)
    raw_content: str = ""  # Full text if available, for embedding generation


class BaseAdapter(ABC):
    """Abstract base class for all news source adapters.

    Each adapter knows how to connect to one type of source and yield
    normalized RawArticle instances.
    """

    @abstractmethod
    async def fetch(self) -> list[RawArticle]:
        """Fetch articles from the source.

        Returns:
            A list of RawArticle instances.

        Raises:
            ConnectionError: If the source is unreachable.
        """
        ...

    @property
    @abstractmethod
    def source_type(self) -> str:
        """The type identifier for this adapter (e.g. 'rss', 'hackernews')."""
        ...
