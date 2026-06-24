"""Tests for core.cursor_turn_compiler — previously a shipped recognition
surface (`bourdon cursor compile-turn`) with ZERO coverage (3-Star audit
tests-P1-2). Covers ranking, the input-bound clamps, and graceful no-match."""

from __future__ import annotations

from pathlib import Path

import yaml

from core.cursor_turn_compiler import (
    MAX_ITEMS_CEILING,
    CursorTurnBrief,
    compile_cursor_turn,
)


def _lib(tmp_path: Path, entities: list[dict]) -> Path:
    agents = tmp_path / "agent-library" / "agents"
    agents.mkdir(parents=True)
    (agents / "codex.l5.yaml").write_text(
        yaml.safe_dump(
            {
                "spec_version": "0.1",
                "agent": {"id": "codex", "type": "code-assistant"},
                "last_updated": "2026-06-01T00:00:00+00:00",
                "known_entities": entities,
            }
        ),
        encoding="utf-8",
    )
    return tmp_path / "agent-library"


def test_compile_cursor_turn_ranks_match_first(tmp_path):
    lib = _lib(
        tmp_path,
        [
            {"name": "Bourdon", "type": "project", "summary": "Recognition.", "visibility": "team"},
            {"name": "Unrelated", "type": "topic", "summary": "x", "visibility": "team"},
        ],
    )
    brief = compile_cursor_turn("Tell me about Bourdon", library_path=lib)
    assert isinstance(brief, CursorTurnBrief)
    assert brief.matched_entities
    assert brief.matched_entities[0]["name"] == "Bourdon"


def test_negative_max_items_does_not_drop_top_entity(tmp_path):
    # Before the clamp, max_items=-1 did scored_entities[:-1] and silently
    # dropped the single highest-scored entity.
    lib = _lib(
        tmp_path,
        [{"name": "Bourdon", "type": "project", "summary": "s", "visibility": "team"}],
    )
    brief = compile_cursor_turn("Bourdon", library_path=lib, max_items=-1)
    assert [m["name"] for m in brief.matched_entities] == ["Bourdon"]


def test_zero_max_items_clamped_to_one(tmp_path):
    lib = _lib(
        tmp_path,
        [
            {"name": "Bourdon", "type": "project", "summary": "s", "visibility": "team"},
            {"name": "Continuo", "type": "project", "summary": "s", "visibility": "team"},
        ],
    )
    brief = compile_cursor_turn("Bourdon Continuo", library_path=lib, max_items=0)
    assert len(brief.matched_entities) == 1


def test_oversized_max_items_clamped_to_ceiling(tmp_path):
    lib = _lib(
        tmp_path,
        [{"name": "Bourdon", "type": "project", "summary": "s", "visibility": "team"}],
    )
    brief = compile_cursor_turn("Bourdon", library_path=lib, max_items=10_000)
    assert len(brief.matched_entities) <= MAX_ITEMS_CEILING


def test_huge_prompt_does_not_crash(tmp_path):
    lib = _lib(
        tmp_path,
        [{"name": "Bourdon", "type": "project", "summary": "s", "visibility": "team"}],
    )
    brief = compile_cursor_turn("Bourdon " * 200_000, library_path=lib)
    assert isinstance(brief, CursorTurnBrief)


def test_no_match_yields_none_confidence(tmp_path):
    lib = _lib(
        tmp_path,
        [{"name": "Zzzqq", "type": "topic", "summary": "s", "visibility": "team"}],
    )
    brief = compile_cursor_turn("completely different vocabulary entirely", library_path=lib)
    assert brief.matched_entities == []
    assert brief.routing["confidence"] == "none"
