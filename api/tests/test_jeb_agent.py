"""Invariants of the JEB Training Center agent that a wrong edit would break
silently: the graph shape, the transition functions the prompts cite, the
knowledge block's contents, and the FAQ's alignment across languages."""

import re

import pytest

from api.services.workflow.dto import ReactFlowDTO
from api.services.workflow.jeb_agent import (
    JEB_TOOL_NODE_ID,
    NODE_ID_ANSWERS,
    NODE_ID_END,
    NODE_ID_NEXT_STEP,
    NODE_ID_START,
    build_jeb_workflow,
    collect_tool_uuids,
)
from api.services.workflow.jeb_knowledge import (
    CENTER_FACTS,
    KNOWLEDGE_BLOCK,
    VERBATIM_MARKER,
    knowledge_block_stats,
)
from api.services.workflow.jeb_knowledge_data import FAQ, SECTION_ORDER, SECTION_TEXTS


def _function_name(label: str) -> str:
    return re.sub(r"[^a-z0-9]", "_", label.lower())


def test_graph_validates_with_and_without_tool():
    ReactFlowDTO.model_validate(build_jeb_workflow())
    with_tool = build_jeb_workflow(["11111111-2222-3333-4444-555555555555"])
    ReactFlowDTO.model_validate(with_tool)
    carriers = [n["id"] for n in with_tool["nodes"] if n["data"].get("tool_uuids")]
    assert carriers == [JEB_TOOL_NODE_ID]
    assert collect_tool_uuids(with_tool) == ["11111111-2222-3333-4444-555555555555"]


def test_call_steps_and_transitions():
    definition = build_jeb_workflow()
    nodes = {n["id"]: n for n in definition["nodes"]}
    assert nodes[NODE_ID_START]["data"]["is_start"] is True
    assert nodes[NODE_ID_END]["data"]["is_end"] is True
    # Every step inherits the global prompt (knowledge + safety rules).
    for node_id in (NODE_ID_START, NODE_ID_ANSWERS, NODE_ID_NEXT_STEP, NODE_ID_END):
        assert nodes[node_id]["data"]["add_global_prompt"] is True

    edges = {(e["source"], e["target"]): e for e in definition["edges"]}
    assert set(edges) == {
        (NODE_ID_START, NODE_ID_ANSWERS),
        (NODE_ID_START, NODE_ID_END),
        (NODE_ID_ANSWERS, NODE_ID_NEXT_STEP),
        (NODE_ID_ANSWERS, NODE_ID_END),
        (NODE_ID_NEXT_STEP, NODE_ID_END),
    }


def test_every_transition_function_is_named_in_its_source_prompt():
    """Gemini Live only leaves a node when the model calls the edge function;
    a prompt that does not name it produces an agent stuck in the greeting."""
    definition = build_jeb_workflow()
    prompts = {n["id"]: n["data"].get("prompt", "") for n in definition["nodes"]}
    for edge in definition["edges"]:
        name = _function_name(edge["data"]["label"])
        assert name in prompts[edge["source"]], (edge["data"]["label"], name)
        # Labels are ASCII so the names are readable in the editor and prompts.
        assert re.fullmatch(r"[A-Za-z ]+", edge["data"]["label"]), edge["data"]["label"]


def test_prompts_have_no_template_braces():
    for node in build_jeb_workflow()["nodes"]:
        for field in ("prompt", "greeting"):
            text = node["data"].get(field) or ""
            assert "{" not in text and "}" not in text, (node["id"], field)


def test_knowledge_block_carries_contacts_sections_and_faq():
    for needle in (
        CENTER_FACTS["telephone_principal"],
        CENTER_FACTS["whatsapp"],
        CENTER_FACTS["email"],
        "FAQ OFFICIELLE DU CENTRE — FRANÇAIS (35)",
        "الأسئلة الشائعة الرسمية للمركز",
        VERBATIM_MARKER,
    ):
        assert needle in KNOWLEDGE_BLOCK, needle
    for section in SECTION_ORDER:
        assert SECTION_TEXTS[section["key"]].strip() in KNOWLEDGE_BLOCK
    # "What the center does not publish" is the fallback and must come last.
    assert KNOWLEDGE_BLOCK.rstrip().endswith(SECTION_TEXTS["G_hors_socle"].strip())


def test_faq_is_aligned_across_languages():
    shapes = {
        lang: [len(s["items"]) for s in faq["sections"]] for lang, faq in FAQ.items()
    }
    assert shapes["fr"] == shapes["en"] == shapes["ar"]
    assert sum(shapes["fr"]) == 35
    for faq in FAQ.values():
        numbers = [item["n"] for s in faq["sections"] for item in s["items"]]
        assert numbers == list(range(1, 36))
        assert len(faq["pathway"]) == 10 and len(faq["notice"]) == 4


@pytest.mark.parametrize("key", [s["key"] for s in SECTION_ORDER])
def test_sections_are_speakable(key):
    text = SECTION_TEXTS[key]
    prefix = next(s["prefix"] for s in SECTION_ORDER if s["key"] == key)
    lines = [line for line in text.splitlines()[1:] if line.strip()]
    assert lines and all(re.match(rf"^(\[TEXTUEL\] )?{prefix}\d+\. ", line) or
                         line.startswith(("[TEXTUEL]", prefix)) for line in lines), key
    for symbol in ("%", "$", "→", "←", "×", "http://", "https://"):
        assert symbol not in text, (key, symbol)


def test_stats_report_every_part():
    stats = knowledge_block_stats()
    assert stats["chars"] == len(KNOWLEDGE_BLOCK)
    assert {"faq_fr", "faq_ar", "regles_utilisation"} <= set(stats["parts"])
    assert stats["faq_questions"] == 35
