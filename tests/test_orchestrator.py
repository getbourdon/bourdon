"""Tests for core.orchestrator -- the Phase 1 L0/L1 memory orchestrator."""

from __future__ import annotations

import pytest

from core import orchestrator
from core.orchestrator import (
    Bourdon,
    build_system_prompt,
    detect_entities,
    estimate_tokens,
    load_l0,
    load_l1_synopsis,
)

# -- Pure-function tests (no fixture needed) -----------------------------------


def test_estimate_tokens_empty_string():
    assert estimate_tokens("") == 0


def test_estimate_tokens_short_string():
    # 4 chars per token, floor division
    assert estimate_tokens("hello") == 1
    assert estimate_tokens("hello world") == 2


def test_detect_entities_case_insensitive():
    keywords = ["Clyde", "ILTT", "Bourdon"]
    assert detect_entities("tell me about clyde", keywords) == ["Clyde"]
    assert detect_entities("CLYDE is cool", keywords) == ["Clyde"]


def test_detect_entities_multiple_hits():
    keywords = ["Alpha", "Beta", "Gamma"]
    hits = detect_entities("alpha and beta together", keywords)
    assert set(hits) == {"Alpha", "Beta"}


def test_detect_entities_no_matches():
    keywords = ["Alpha", "Beta"]
    assert detect_entities("talking about the weather", keywords) == []


def test_detect_entities_empty_keywords():
    assert detect_entities("anything at all", []) == []


def test_build_system_prompt_l0_only():
    prompt = build_system_prompt("Base instructions", "L0 context here")
    assert prompt.startswith("Base instructions")
    assert "L0 context here" in prompt
    # No L1 / L2 sections when not supplied
    assert "L1" not in prompt or "## CLYDE MEMORY -- L1" not in prompt


def test_build_system_prompt_with_l1():
    prompt = build_system_prompt(
        "Base", "L0 block", "## CLYDE MEMORY -- L1 ENTITY CONTEXT\nfoo"
    )
    assert "L0 block" in prompt
    assert "## CLYDE MEMORY -- L1 ENTITY CONTEXT" in prompt
    assert "foo" in prompt


def test_build_system_prompt_with_l2():
    prompt = build_system_prompt("Base", "L0", l2_context="retrieved stuff")
    assert "## CLYDE MEMORY -- L2 EPISODIC CONTEXT" in prompt
    assert "retrieved stuff" in prompt


# -- Fixtured tests ------------------------------------------------------------


def test_load_l0_returns_context_and_keywords(isolated_memory_dirs):
    context, keywords = load_l0()
    assert "Test User" in context
    assert "TestCo" in context
    # Projects with priority <=2 appear as Active Projects
    assert "Alpha" in context  # priority 1 -- included
    # Entities list (all 3 keywords regardless of priority)
    assert set(keywords) == {"Alpha", "Beta", "Gamma"}


def test_load_l0_filters_active_projects_by_priority(isolated_memory_dirs):
    """Only projects with priority <=2 should appear in Active Projects line."""
    context, _ = load_l0()
    # Alpha is priority 1 (included), Beta is priority 3 (should NOT be in active)
    active_line = [
        line for line in context.splitlines() if line.startswith("Active Projects:")
    ]
    assert len(active_line) == 1
    assert "Alpha" in active_line[0]
    assert "Beta" not in active_line[0]


def test_load_l1_synopsis_exact_match(isolated_memory_dirs):
    isolated_memory_dirs["write_l1"]("Alpha", "# Alpha\nSynopsis body here.")
    result = load_l1_synopsis("Alpha")
    assert result is not None
    assert "Synopsis body here" in result


def test_load_l1_synopsis_case_insensitive(isolated_memory_dirs):
    isolated_memory_dirs["write_l1"]("Alpha", "# Alpha synopsis")
    result = load_l1_synopsis("alpha")  # lowercase lookup, mixed-case file
    assert result is not None


def test_load_l1_synopsis_missing_returns_none(isolated_memory_dirs):
    result = load_l1_synopsis("NonExistent")
    assert result is None


# -- Bourdon class tests ------------------------------------------------------


def test_bourdon_init_loads_l0(isolated_memory_dirs):
    memory = Bourdon()
    assert len(memory.keywords) == 3
    assert set(memory.keywords) == {"Alpha", "Beta", "Gamma"}
    assert "L0 CONTEXT" in memory.l0_context


@pytest.mark.asyncio
async def test_bourdon_prepare_with_l0_hit(isolated_memory_dirs):
    isolated_memory_dirs["write_l1"]("Alpha", "# Alpha\nProject synopsis.")
    memory = Bourdon()
    prompt = await memory.prepare("Let's work on Alpha today", "You are helpful.")
    assert "You are helpful." in prompt
    assert "L0 CONTEXT" in prompt
    assert "Project synopsis" in prompt  # L1 loaded


@pytest.mark.asyncio
async def test_bourdon_prepare_with_no_hits(isolated_memory_dirs):
    memory = Bourdon()
    prompt = await memory.prepare("Unrelated question", "You are helpful.")
    assert "L0 CONTEXT" in prompt  # L0 always present
    # No L1 section because no entities matched
    assert "L1 ENTITY CONTEXT" not in prompt


@pytest.mark.asyncio
async def test_bourdon_prepare_multiple_entities(isolated_memory_dirs):
    isolated_memory_dirs["write_l1"]("Alpha", "# Alpha\nAlpha content.")
    isolated_memory_dirs["write_l1"]("Beta", "# Beta\nBeta content.")
    memory = Bourdon()
    prompt = await memory.prepare("compare alpha and beta", "You are helpful.")
    assert "Alpha content" in prompt
    assert "Beta content" in prompt


@pytest.mark.asyncio
async def test_bourdon_prepare_missing_synopsis_degrades_gracefully(
    isolated_memory_dirs,
):
    """L0 keyword hit but no L1 file should not crash the orchestrator."""
    memory = Bourdon()  # No L1 files created
    prompt = await memory.prepare("Talk about Alpha", "Base.")
    # L0 present, L1 absent, no exception
    assert "L0 CONTEXT" in prompt
    # No L1 section because there was no actual synopsis body to load
    assert "L1 ENTITY CONTEXT" not in prompt


def test_bourdon_reload_l0_picks_up_changes(isolated_memory_dirs):
    """After editing hot_cache.yaml on disk, reload_l0() should pick up the new entities."""
    memory = Bourdon()
    initial_count = len(memory.keywords)

    # Append a new entity to the YAML
    isolated_memory_dirs["write_l0"](
        {
            "identity": {"user": "Test", "alias": "T", "company": "X", "role": "Y"},
            "projects": [{"name": "Alpha", "priority": 1}],
            "hardware": {"local_model": "m", "inference": "i"},
            "current_focus": {
                "primary": "p",
                "last_session": "2026-04-15",
                "last_topic": "t",
            },
            "entities": [
                {"keyword": "Alpha", "type": "project"},
                {"keyword": "Beta", "type": "project"},
                {"keyword": "Gamma", "type": "concept"},
                {"keyword": "Delta", "type": "project"},  # NEW
            ],
        }
    )

    memory.reload_l0()
    assert len(memory.keywords) == initial_count + 1
    assert "Delta" in memory.keywords


@pytest.mark.asyncio
async def test_l1_token_budget_skips_entities_over_budget(
    isolated_memory_dirs, monkeypatch
):
    """When L1 budget would be exceeded, later entities should be skipped.

    Token math (estimate_tokens is chars // 4):
      - L1 header "\n\n## CLYDE MEMORY -- L1 ENTITY CONTEXT\n" ~= 10 tokens
      - Each chunk wrapper "\n---\n<body>" ~= 2 tokens + body tokens
      - Body of 200 "A"s ~= 50 tokens -> chunk ~= 52 tokens
      - After header+first chunk: ~62 tokens. Budget 100 fits 1, not 2.
    """
    monkeypatch.setattr(orchestrator, "L1_TOKEN_BUDGET", 100)

    body = "A" * 200  # ~50 tokens
    isolated_memory_dirs["write_l1"]("Alpha", body)
    isolated_memory_dirs["write_l1"]("Beta", body)
    isolated_memory_dirs["write_l1"]("Gamma", body)

    memory = Bourdon()
    prompt = await memory.prepare("alpha beta gamma", "base")

    # First entity fits; at least one body occurrence expected
    assert body in prompt
    # Budget should have been hit before all three entities were added
    assert prompt.count(body) < 3
