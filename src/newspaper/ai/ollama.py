"""Ollama HTTP API: embeddings for grouping and structured note drafting."""

from __future__ import annotations

import json
from dataclasses import dataclass

import httpx

from ..config.models import LLMConfig


class OllamaError(RuntimeError):
    """A single request failed or returned something unusable."""


class OllamaUnavailableError(OllamaError):
    """Ollama is down or the model is missing: the whole edition must fail."""


@dataclass
class Draft:
    headline: str
    dek: str
    body: str
    differences: str


_FIELDS = ("headline", "dek", "body", "differences")
DRAFT_SCHEMA = {
    "type": "object",
    "properties": {field: {"type": "string"} for field in _FIELDS},
    "required": list(_FIELDS),
}
LANGUAGE_NAMES = {"es": "español", "en": "English"}

SYSTEM_PROMPT = """\
You are the editor of a personal daily newspaper. You receive one or more articles, \
from one or more outlets, about the same event. Write ONE integrated note.

Rules:
- Write everything in {language}.
- Use only facts present in the material. Never invent data, quotes, names or figures.
- headline: informative, at most 15 words, no clickbait.
- dek: one sentence on why it matters.
- body: 3 to 6 paragraphs of Markdown: what happened, then context. Attribute facts that \
only one outlet reports ("según CIPER, ...").
- differences: when two or more outlets are present, explain where their coverage differs \
(facts, figures, emphasis or contradictions), always naming each outlet. Empty string if \
there is one outlet or no relevant difference.
"""


class Ollama:
    def __init__(self, cfg: LLMConfig, client: httpx.Client | None = None) -> None:
        self.cfg = cfg
        self.client = client or httpx.Client(base_url=cfg.base_url, timeout=cfg.timeout_seconds)

    def _post(self, path: str, payload: dict) -> dict:
        try:
            response = self.client.post(path, json=payload)
        except (httpx.ConnectError, httpx.ConnectTimeout) as e:
            raise OllamaUnavailableError(f"Ollama no responde en {self.cfg.base_url} ({e})") from e
        except httpx.HTTPError as e:  # read timeout on one long note, etc.
            raise OllamaError(f"Ollama: {e}") from e
        if response.status_code == 404:
            model = payload["model"]
            raise OllamaUnavailableError(
                f"Modelo no instalado ({model}): ejecuta `ollama pull {model}`"
            )
        if response.status_code >= 400:
            raise OllamaError(f"Ollama {response.status_code}: {response.text[:300]}")
        return response.json()

    def embed(self, texts: list[str]) -> list[list[float]]:
        data = self._post("/api/embed", {"model": self.cfg.embed_model, "input": texts})
        vectors = data.get("embeddings")
        if not vectors or len(vectors) != len(texts):
            raise OllamaError("embed: respuesta inesperada")
        return vectors

    def draft(self, materials: list[tuple[str, str, str]], language: str) -> Draft:
        """materials: (outlet, title, text) per article."""
        sources = "\n\n".join(
            f"### Outlet: {outlet}\nTitle: {title}\n\n{text}" for outlet, title, text in materials
        )
        system = SYSTEM_PROMPT.format(language=LANGUAGE_NAMES.get(language, language))
        data = self._post(
            "/api/chat",
            {
                "model": self.cfg.model,
                "stream": False,
                "format": DRAFT_SCHEMA,
                "options": {"temperature": 0.3},
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": sources},
                ],
            },
        )
        try:
            fields = json.loads(data["message"]["content"])
            return Draft(**{k: str(fields.get(k) or "").strip() for k in _FIELDS})
        except (KeyError, TypeError, AttributeError, json.JSONDecodeError) as e:
            raise OllamaError(f"draft: JSON inválido ({e})") from e
