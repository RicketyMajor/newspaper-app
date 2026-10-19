import json
from datetime import datetime
from types import SimpleNamespace

import httpx
import pytest

from newspaper.ai.ollama import NO_TOPIC, Ollama, OllamaError, OllamaUnavailableError
from newspaper.config.models import LLMConfig
from newspaper.processing.cluster import cluster
from newspaper.processing.select import Interest, select_groups


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
    assert "repeat_penalty" not in seen["options"]  # it makes the body open with a "#" title


def test_draft_retry_uses_a_repeat_penalty():
    seen = {}

    def handler(request):
        seen.update(json.loads(request.content))
        content = {"headline": "t", "dek": "", "body": "b", "differences": ""}
        return httpx.Response(200, json={"message": {"content": json.dumps(content)}})

    ollama_with(handler).draft([("CIPER", "t", "x")], "es", retry=True)
    assert seen["options"]["repeat_penalty"] == 1.15  # stops gemma3's runaway no-break spaces


def test_only_the_retry_forbids_quotes_in_headline_and_dek():
    systems = []

    def handler(request):
        systems.append(json.loads(request.content)["messages"][0]["content"])
        content = {"headline": "t", "dek": "", "body": "b", "differences": ""}
        return httpx.Response(200, json={"message": {"content": json.dumps(content)}})

    llm = ollama_with(handler)
    llm.draft([("CIPER", "t", "x")], "es")
    llm.draft([("CIPER", "t", "x")], "es", retry=True)
    rule = "In the headline and the dek never use quotation marks"
    assert [rule in system for system in systems] == [False, True]


def test_draft_material_never_contains_straight_double_quotes():
    # The JSON grammar ends a field at a raw ", and gemma3 copies quotes from the material.
    seen = {}

    def handler(request):
        seen.update(json.loads(request.content))
        content = {"headline": "t", "dek": "", "body": "b", "differences": ""}
        return httpx.Response(200, json={"message": {"content": json.dumps(content)}})

    ollama_with(handler).draft([("DW", 'Libera a las "cucarachas"', 'Dijo "basta" y 5" ')], "es")

    material = seen["messages"][1]["content"]
    assert '"' not in material
    assert "las «cucarachas»" in material and "Dijo «basta»" in material


def test_draft_drops_an_outlet_tag_before_the_body():
    def handler(request):
        content = {"headline": "t", "dek": "", "body": "[CIPER] El texto.", "differences": ""}
        return httpx.Response(200, json={"message": {"content": json.dumps(content)}})

    assert ollama_with(handler).draft([("CIPER", "t", "x")], "es").body == "El texto."


def test_draft_drops_a_heading_before_the_body():
    def handler(request):
        body = "# Crisis por agua en Guadalajara\n\nMiles protestaron.\n\n# Otro"
        content = {"headline": "t", "dek": "", "body": body, "differences": ""}
        return httpx.Response(200, json={"message": {"content": json.dumps(content)}})

    body = ollama_with(handler).draft([("CIPER", "t", "x")], "es").body
    assert body == "Miles protestaron.\n\n# Otro"  # only the leading one


def test_draft_with_invalid_json_is_a_note_level_error():
    def handler(request):
        return httpx.Response(200, json={"message": {"content": "not json"}})

    with pytest.raises(OllamaError) as info:
        ollama_with(handler).draft([("CIPER", "t", "x")], "es")
    assert not isinstance(info.value, OllamaUnavailableError)


def test_classify_sends_the_topics_in_order_and_returns_the_choice():
    seen = {}

    def handler(request):
        seen.update(json.loads(request.content))
        return httpx.Response(200, json={"message": {"content": '{"tema": "K-pop"}'}})

    topics = ["K-pop", "música", "política y gobierno"]
    assert ollama_with(handler).classify("BTS llega a Chile", topics) == "K-pop"
    assert seen["format"]["properties"]["tema"]["enum"] == [*topics, NO_TOPIC]
    assert seen["options"] == {"temperature": 0, "num_ctx": 8192}
    system = seen["messages"][0]["content"]
    assert system.index("- K-pop") < system.index("- música")  # earlier wins on overlap
    assert seen["messages"][1]["content"] == "BTS llega a Chile"


@pytest.mark.parametrize("content", ["no json", '{"tema": "otra cosa"}', "{}"])
def test_classify_rejects_unusable_answers(content):
    def handler(request):
        return httpx.Response(200, json={"message": {"content": content}})

    with pytest.raises(OllamaError) as info:
        ollama_with(handler).classify("x", ["música"])
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


def test_select_applies_a_per_section_limit():
    tech = [[art("Xataka", "Tecnología", h)] for h in (1, 2, 3)]
    world = [[art("DW", "Mundo", h)] for h in (4, 5, 6)]
    selected = select_groups(
        tech + world, ["Mundo", "Tecnología"], front_size=0, per_section=2, limits={"Tecnología": 1}
    )
    assert [s.section for s in selected] == ["Mundo", "Mundo", "Tecnología"]


def test_select_breaks_outlet_ties_by_article_count_before_recency():
    quake = [art("BBC Mundo", "Mundo", 1), art("BBC Mundo", "Mundo", 2)]  # older, covered twice
    fresh = [art("DW", "Mundo", 9)]
    selected = select_groups([fresh, quake], ["Mundo"], front_size=0, per_section=1)
    assert [s.items for s in selected] == [quake]


def test_select_puts_liked_first_among_equal_coverage_and_disliked_last_off_the_front():
    pop = [art("Culto", "Cultura", 23)]  # newest, but "no"
    books = [art("Guardian", "Cultura", 1)]  # oldest, "sí"
    film = [art("El País", "Cultura", 12)]  # neutral
    selected = select_groups(
        [pop, books, film],
        ["Cultura"],
        front_size=3,
        per_section=5,
        interests=[Interest("K-pop", False), Interest("libros", True), None],
    )
    assert [(s.items, s.is_front, s.interest) for s in selected] == [
        (books, True, Interest("libros", True)),
        (film, True, None),
        (pop, False, Interest("K-pop", False)),  # never on the front page, even with room
    ]


def test_select_keeps_outlet_count_above_a_liked_topic_and_drops_disliked_first():
    covered = [art("BBC Mundo", "Mundo", 1), art("DW", "Mundo", 2)]
    liked = [art("DW", "Mundo", 9)]
    gossip = [art("DW", "Mundo", 10)]
    selected = select_groups(
        [gossip, liked, covered],
        ["Mundo"],
        front_size=1,
        per_section=1,
        interests=[Interest("farándula", False), Interest("IA", True), None],
    )
    assert [s.items for s in selected] == [covered, liked]
