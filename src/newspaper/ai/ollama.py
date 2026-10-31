"""Ollama HTTP API: embeddings for grouping and structured note drafting."""

from __future__ import annotations

import json
import re
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
# ponytail: fits generate.MAX_CHARS_PER_NOTE (~6k tokens) + prompt + answer; Ollama's 4096
# default silently cuts the material. Raise both together if notes need more source text.
NUM_CTX = 8192
# ponytail: gemma3 sometimes emits no-break spaces (U+00A0) in a loop until Ollama aborts with
# "token repeat limit reached" (a note published undrafted). 1.15 stopped it in 5/5 runs where
# 0.7 temperature failed 2/5 (2026-10-14). Retry only: on every call it made 4/32 bodies open
# with a "#" title, which the prompt forbids (0 in ~135 notes without it).
REPEAT_PENALTY = 1.15
# A straight quote inside a JSON string ends it: "…del bulo del" (#16). Retry only, like the
# penalty: the main prompt already asks for «…», and prompt changes have caused regressions.
RETRY_RULE = "- In the headline and the dek never use quotation marks of any kind.\n"

SYSTEM_PROMPT = """\
You are the editor of a personal daily newspaper. You receive one or more articles, \
from one or more outlets, about the same event. Write ONE integrated note.

Rules:
- Write everything in {language}, following its newspaper conventions.
- Use only facts present in the material. Never invent data, quotes, names or figures.
- Ignore website boilerplate in the material (subscription or donation appeals, newsletter, \
follow-us, cookie or app notices) and never comment on the material itself or on what it lacks.
- For quotations use «…» in Spanish or “…” in English, never the straight double quote.
- headline: informative, at most 15 words, no clickbait, in sentence case: capitalize only \
the first word and proper nouns, never every word.
- dek: one sentence on why it matters.
- body: 3 to 6 paragraphs of Markdown, starting directly with the first paragraph (no title, \
no headings, do not repeat the headline): what happened, then context. Attribute facts that \
only one outlet reports (según <outlet>, …).
- Opinion columns, analyses and interviews: say what they are and attribute every view to \
its authors. Never present an opinion as a fact.
- differences: when two or more outlets are present, explain where their coverage differs \
(facts, figures, emphasis or contradictions), always naming each outlet. Only real \
differences: facts or figures the outlets share are never a difference. Empty string if \
there is one outlet or no relevant difference.
"""

# ponytail: fixed catch-all topics so the model never forces a story into the reader's list.
# Without them, wars landed in "tech business" and hurricanes in "sports" (2026-10-14).
NEUTRAL_TOPICS = (
    "política y gobierno",
    "justicia, tribunales y corrupción",
    "geopolítica, diplomacia y guerras",
    "economía, empresas y trabajo",
    "clima, desastres naturales y medio ambiente",
    "salud y medicina",
    "otras ciencias: biología, geología, paleontología",
    "historia y arqueología",
    "sociedad, educación y religión",
)
NO_TOPIC = "ninguno"
# Spanish like the topics; validated on 273 articles (~96 % right on "like", ~89 % on "dislike").
CLASSIFY_PROMPT = """\
Clasificas noticias por tema. Elige el tema de la lista que sea el asunto principal de la \
noticia; si encaja en varios, el que aparece primero en la lista. Si no encaja en ninguno, \
"ninguno". Que la noticia mencione de pasada un tema no basta: tiene que tratar de él.

Temas:
{topics}
"""


_QUOTED = re.compile(r'"([^"\n]*)"')
_OUTLET_TAG = re.compile(r"^\[[^\]\n]{1,40}\]\s*")
# The whole heading line; just the mark when the body has no break after it (#19).
_LEADING_HEADING = re.compile(r"^#{1,6}[ \t]+(?:[^\n]*\n+)?")
_LEADING_DASH = re.compile(r"^—\s*")  # the outlet's lead-in, «— El texto» (#19)


def _no_straight_quotes(text: str) -> str:
    """The JSON grammar ends a field at a raw " and the model copies quotes from its material,
    which silently cut headlines and bodies; it never gets to see one."""
    return _QUOTED.sub(r"«\1»", text).replace('"', "'")


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

    def draft(
        self, materials: list[tuple[str, str, str]], language: str, retry: bool = False
    ) -> Draft:
        """materials: (outlet, title, text) per article. retry: sample with REPEAT_PENALTY."""
        options = {"temperature": 0.3, "num_ctx": NUM_CTX}
        if retry:
            options["repeat_penalty"] = REPEAT_PENALTY
        sources = _no_straight_quotes(
            "\n\n".join(
                f"### Outlet: {outlet}\nTitle: {title}\n\n{text}"
                for outlet, title, text in materials
            )
        )
        system = SYSTEM_PROMPT.format(language=LANGUAGE_NAMES.get(language, language))
        if retry:
            system += RETRY_RULE
        data = self._post(
            "/api/chat",
            {
                "model": self.cfg.model,
                "stream": False,
                "format": DRAFT_SCHEMA,
                "options": options,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": sources},
                ],
            },
        )
        try:
            fields = json.loads(data["message"]["content"])
            draft = Draft(**{k: str(fields.get(k) or "").strip() for k in _FIELDS})
        except (KeyError, TypeError, AttributeError, json.JSONDecodeError) as e:
            raise OllamaError(f"draft: JSON inválido ({e})") from e
        # It mimics the material's outlet headers, and sometimes opens with a title anyway.
        for leading in (_OUTLET_TAG, _LEADING_HEADING, _LEADING_DASH):
            draft.body = leading.sub("", draft.body)
        return draft

    def classify(self, text: str, topics: list[str]) -> str:
        """The topic `text` is mainly about, from `topics` (the earlier one when several fit),
        or NO_TOPIC."""
        choices = [*topics, NO_TOPIC]
        system = CLASSIFY_PROMPT.format(topics="\n".join(f"- {t}" for t in topics))
        data = self._post(
            "/api/chat",
            {
                "model": self.cfg.model,
                "stream": False,
                "format": {
                    "type": "object",
                    "properties": {"tema": {"enum": choices}},
                    "required": ["tema"],
                },
                # Same num_ctx as draft: a different one makes Ollama reload the model.
                "options": {"temperature": 0, "num_ctx": NUM_CTX},
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": text},
                ],
            },
        )
        try:
            topic = json.loads(data["message"]["content"])["tema"]
        except (KeyError, TypeError, json.JSONDecodeError) as e:
            raise OllamaError(f"classify: JSON inválido ({e})") from e
        if topic not in choices:
            raise OllamaError(f"classify: tema desconocido ({topic!r})")
        return topic
