import json
from datetime import datetime
from types import SimpleNamespace

import httpx
import pytest

from newspaper.ai.ollama import Ollama, OllamaError, OllamaUnavailableError
from newspaper.config.models import LLMConfig
from newspaper.processing.cluster import cluster
from newspaper.processing.select import select_groups


def ollama_with(handler) -> Ollama:
    cfg = LLMConfig()
    client = httpx.Client(base_url=cfg.base_url, transport=httpx.MockTransport(handler))
    return Ollama(cfg, client)


def test_embed_sends_model_and_returns_vectors():
    seen = {}

    def handler(request):
        seen.update(json.loads(request.content))
        return httpx.Response(200, json={"embeddings": [[1.0, 0.0], [0.0, 1.0]]})

    assert ollama_with(handler).embed(["a", "b"]) == [[1.0, 0.0], [0.0, 1.0]]
    assert seen == {"model": "bge-m3", "input": ["a", "b"]}


def test_unreachable_or_missing_model_is_unavailable():
    def down(request):
        raise httpx.ConnectError("refused")

    with pytest.raises(OllamaUnavailableError):
        ollama_with(down).embed(["a"])

    def missing(request):
        return httpx.Response(404, json={"error": "model 'gemma3:12b' not found"})

    with pytest.raises(OllamaUnavailableError, match="ollama pull"):
        ollama_with(missing).draft([("CIPER", "t", "x")], "es")


def test_draft_parses_structured_output_and_prompts_in_language():
    seen = {}
    content = {"headline": " Titular ", "dek": "Bajada", "body": "Cuerpo", "differences": ""}

    def handler(request):
        seen.update(json.loads(request.content))
        return httpx.Response(200, json={"message": {"content": json.dumps(content)}})

    draft = ollama_with(handler).draft([("CIPER", "Título", "Texto")], "es")

    assert (draft.headline, draft.dek, draft.body, draft.differences) == (
        "Titular",
        "Bajada",
        "Cuerpo",
        "",
    )
    assert seen["model"] == "gemma3:12b"
    assert seen["format"]["required"] == ["headline", "dek", "body", "differences"]
    assert "español" in seen["messages"][0]["content"]
    assert "CIPER" in seen["messages"][1]["content"]
    assert seen["options"]["num_ctx"] >= 8192  # Ollama's 4096 default truncates the material


def test_draft_with_invalid_json_is_a_note_level_error():
    def handler(request):
        return httpx.Response(200, json={"message": {"content": "not json"}})

    with pytest.raises(OllamaError) as info:
        ollama_with(handler).draft([("CIPER", "t", "x")], "es")
    assert not isinstance(info.value, OllamaUnavailableError)


def test_cluster_groups_similar_vectors_transitively():
    vectors = [
        [1.0, 0.0, 0.0],
        [0.0, 1.0, 0.0],
        [0.95, 0.05, 0.0],
        [0.9, 0.2, 0.1],
        [0.0, 0.0, 1.0],
    ]
    assert cluster(vectors, 0.9) == [[0, 2, 3], [1], [4]]
    assert cluster(vectors, 0.9999) == [[0], [1], [2], [3], [4]]
    assert cluster([], 0.8) == []


def art(source, section, hour):
    return SimpleNamespace(source_name=source, section=section, when=datetime(2026, 10, 10, hour))


def test_select_ranks_by_distinct_sources_then_recency_and_applies_limits():
    big = [art("CIPER", "Chile", 8), art("BBC Mundo", "Mundo", 9), art("DW", "Mundo", 7)]
    same_outlet = [art("CIPER", "Chile", 23), art("CIPER", "Chile", 22)]
    tie = [art("CIPER", "Chile", 5), art("BBC Mundo", "Mundo", 6)]
    late = [art("SINC", "Ciencia", 20)]
    early = [art("SINC", "Ciencia", 1)]
    chile_old = [art("Cooperativa", "Chile", 2)]

    selected = select_groups(
        [early, big, same_outlet, late, tie, chile_old],
        sections=["Chile", "Mundo", "Ciencia"],
        front_size=2,
        per_section=1,
    )

    assert [(s.items, s.section, s.is_front) for s in selected] == [
        (big, "Mundo", True),
        (tie, "Chile", True),  # tie Chile/Mundo -> first section in the TOML
        (same_outlet, "Chile", False),
        (late, "Ciencia", False),
    ]


def test_select_breaks_outlet_ties_by_article_count_before_recency():
    quake = [art("BBC Mundo", "Mundo", 1), art("BBC Mundo", "Mundo", 2)]  # older, covered twice
    fresh = [art("DW", "Mundo", 9)]
    selected = select_groups([fresh, quake], ["Mundo"], front_size=0, per_section=1)
    assert [s.items for s in selected] == [quake]
