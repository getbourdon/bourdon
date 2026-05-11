"""
Bourdon federation round-trip integration tests.

Scope
-----
These tests assert the contract that ties Bourdon together:

    each adapter's export_l5() output is queryable through L6Store with
    correct attribution, visibility filtering, and cross-agent aggregation.

Per-adapter unit suites verify only the adapter -> L5 leg. The L6Store
unit suite verifies only the store -> query leg with synthetic manifests.
Nothing else covers the seam where they meet. If an adapter silently
changes the shape of its L5 in a way the store does not expect, the unit
tests stay green and the federation product silently breaks.

This is Layer 1 of the cross-agent test plan recorded in
PROJECTS/NEUROLAYER/NOTES.md (2026-05-11 entry on claude-brain).
Layers 2 and 3 (`bourdon dogfood` CLI + public acceptance scenario)
live outside the test suite.

Coverage as of v0.4.1
---------------------
Wired end-to-end:
    copilot   (convention-file adapter, plants memory.md)
    cascade   (convention-file adapter, plants memory.md)
    cursor    (SQLite adapter, seeds state.vscdb directly)

Stubbed (TODO -- fixture plumbing only, the assertions below already cover them):
    claude-code  (needs Path.home() monkeypatch over a 3-source tree)
    codex        (needs Path.home() monkeypatch over sessions+memories+brain)
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Callable

import pytest

from adapters.base import L5Manifest
from adapters.cascade import CascadeAdapter
from adapters.claude_code import ClaudeCodeAdapter
from adapters.codex import CodexAdapter
from adapters.copilot import CopilotAdapter
from adapters.cursor import CursorAdapter
from core.l5_io import write_l5_dict
from core.l6_store import L6Store

# ---------------------------------------------------------------------------
# Marker facts -- a distinct, easily-grepped entity per adapter.
#
# The round-trip test plants each adapter with a marker entity whose name is
# unique to that adapter (so we can prove the L5 reached L6 and is attributed
# to the right agent), plus a shared entity ("Bourdon") that every adapter
# knows about (so we can prove cross-agent aggregation works).
# ---------------------------------------------------------------------------

SHARED_ENTITY = "Bourdon"
SHARED_SUMMARY_PREFIX = "Cross-agent memory federation, as seen by"

# Federation queries default to access_level="public", but three of five
# adapters (codex always, copilot + cursor by default policy) tag entities
# as TEAM. A realistic single-user federation queries at "team" level so its
# own agents can see each other's content. We use that here. See the
# "team-default visibility" product finding in
# PROJECTS/NEUROLAYER/NOTES.md for the open question on whether the L6 default
# itself should bump to "team" for the single-user path.
FEDERATION_ACCESS = "team"

UNIQUE_MARKERS: dict[str, str] = {
    "copilot": "CopilotOnlyFact",
    "cascade": "CascadeOnlyFact",
    "cursor": "CursorOnlyFact",
    "claude-code": "ClaudeCodeOnlyFact",
    "codex": "CodexOnlyFact",
}


# ---------------------------------------------------------------------------
# Per-adapter fixture planters.
#
# Each helper accepts `tmp_path` and returns a configured adapter whose
# `export_l5()` will produce a manifest containing:
#   - one entity named UNIQUE_MARKERS[agent_id] (attribution proof)
#   - one entity named SHARED_ENTITY            (federation proof)
# Plus whatever incidental rows the adapter naturally produces from the
# fixture (sessions, project entities, etc.) -- those are not asserted on,
# only the marker shape is contract.
# ---------------------------------------------------------------------------


# Convention-file and Cursor planters take their explicit dir parameter, so
# they ignore `monkeypatch` and `Path.home()`. Path.home()-dependent planters
# (claude-code, codex) use `monkeypatch` to redirect home into their slice
# of tmp_path. The federation fixture passes both args to every planter so
# the signature is uniform.

def _plant_copilot(tmp_path: Path, monkeypatch) -> CopilotAdapter:
    d = tmp_path / ".copilot-bourdon"
    d.mkdir()
    (d / "memory.md").write_text(
        "---\n"
        "entities:\n"
        f"  - name: {UNIQUE_MARKERS['copilot']}\n"
        "    type: project\n"
        "    summary: A fact only Copilot knows.\n"
        "    tags: [marker, federation-test]\n"
        f"  - name: {SHARED_ENTITY}\n"
        # Match Cursor's inferred entity type so build_recognition_manifest()
        # (which dedupes on (name, type)) collapses all three adapters into
        # one row. See "Cross-type entity-dedupe question" in
        # PROJECTS/NEUROLAYER/NOTES.md for why this is contract-relevant.
        "    type: project\n"
        f"    summary: {SHARED_SUMMARY_PREFIX} Copilot\n"
        "    tags: [shared, federation-test]\n"
        "sessions: []\n"
        "---\n"
        "Freeform body intentionally left short.\n",
        encoding="utf-8",
    )
    return CopilotAdapter(copilot_dir=d)


def _plant_cascade(tmp_path: Path, monkeypatch) -> CascadeAdapter:
    d = tmp_path / ".cascade-bourdon"
    d.mkdir()
    (d / "memory.md").write_text(
        "---\n"
        "entities:\n"
        f"  - name: {UNIQUE_MARKERS['cascade']}\n"
        "    type: project\n"
        "    summary: A fact only Cascade knows.\n"
        "    tags: [marker, federation-test]\n"
        f"  - name: {SHARED_ENTITY}\n"
        # Match Cursor's inferred entity type so build_recognition_manifest()
        # (which dedupes on (name, type)) collapses all three adapters into
        # one row. See "Cross-type entity-dedupe question" in
        # PROJECTS/NEUROLAYER/NOTES.md for why this is contract-relevant.
        "    type: project\n"
        f"    summary: {SHARED_SUMMARY_PREFIX} Cascade\n"
        "    tags: [shared, federation-test]\n"
        "sessions: []\n"
        "---\n"
        "Freeform body intentionally left short.\n",
        encoding="utf-8",
    )
    return CascadeAdapter(cascade_dir=d)


def _plant_cursor(tmp_path: Path, monkeypatch) -> CursorAdapter:
    cursor_dir = tmp_path / "Cursor"
    (cursor_dir / "User" / "globalStorage").mkdir(parents=True)
    workspace = cursor_dir / "User" / "workspaceStorage" / "fedtest"
    workspace.mkdir(parents=True)
    db = workspace / "state.vscdb"

    # Cursor's adapter infers project entities from composer workspacePaths.
    # We use the marker names as the project names so they appear in
    # manifest.known_entities verbatim.
    records = [
        (
            "composer.composerData",
            {
                "workspacePath": f"/projects/{UNIQUE_MARKERS['cursor']}",
                "title": "Marker session for federation round-trip",
                "messages": [],
                "lastUpdatedAt": "2026-05-11T12:00:00Z",
            },
        ),
        (
            "composer.composerData.bourdon",
            {
                "workspacePath": f"/projects/{SHARED_ENTITY}",
                "title": "Shared-entity session for federation round-trip",
                "messages": [],
                "lastUpdatedAt": "2026-05-11T12:00:01Z",
            },
        ),
    ]
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db))
    try:
        conn.execute("CREATE TABLE ItemTable (key TEXT PRIMARY KEY, value TEXT)")
        for key, value in records:
            conn.execute(
                "INSERT INTO ItemTable (key, value) VALUES (?, ?)",
                (key, json.dumps(value)),
            )
        conn.commit()
    finally:
        conn.close()
    return CursorAdapter(cursor_dir=cursor_dir)


def _plant_claude_code(tmp_path: Path, monkeypatch) -> ClaudeCodeAdapter:
    """
    Claude Code adapter reads from three sources rooted at $HOME:
    ~/claude-brain (PROJECTS/<name>/OVERVIEW.md becomes entities),
    ~/.claude/projects/.../memory/*.md (auto-memory), and
    ~/claude-memory/memory.jsonl (knowledge graph).
    For the round-trip we only need PROJECTS entities, so just claude-brain.
    """
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.delenv("CLAUDE_BRAIN", raising=False)
    monkeypatch.setattr(Path, "home", lambda: fake_home)

    brain = fake_home / "claude-brain"
    brain.mkdir()
    (brain / "CURRENT.md").write_text("# Current focus\n", encoding="utf-8")
    (brain / "LOG").mkdir()
    projects = brain / "PROJECTS"
    projects.mkdir()

    for project_name, summary in (
        (UNIQUE_MARKERS["claude-code"], "A fact only Claude Code knows."),
        (SHARED_ENTITY, f"{SHARED_SUMMARY_PREFIX} Claude Code"),
    ):
        proj = projects / project_name
        proj.mkdir()
        (proj / "OVERVIEW.md").write_text(
            f"# {project_name}\n\n{summary}\n",
            encoding="utf-8",
        )

    return ClaudeCodeAdapter()


def _plant_codex(tmp_path: Path, monkeypatch) -> CodexAdapter:
    """
    Codex adapter reads ~/.codex/session_index.jsonl; each entry's
    thread_name becomes a known-entity. Index-only is sufficient for
    entity extraction -- rollout files only affect session bodies.
    """
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: fake_home)

    codex_home = fake_home / ".codex"
    codex_home.mkdir()
    (codex_home / "sessions").mkdir()

    idx = codex_home / "session_index.jsonl"
    with open(idx, "w", encoding="utf-8") as f:
        for session_id, thread_name in (
            ("marker-session", UNIQUE_MARKERS["codex"]),
            ("shared-session", SHARED_ENTITY),
        ):
            f.write(
                json.dumps(
                    {
                        "id": session_id,
                        "thread_name": thread_name,
                        "updated_at": "2026-05-11T12:00:00Z",
                    }
                )
                + "\n"
            )

    return CodexAdapter()


PLANTERS: dict[str, Callable[[Path, pytest.MonkeyPatch], object]] = {
    "copilot": _plant_copilot,
    "cascade": _plant_cascade,
    "cursor": _plant_cursor,
    "claude-code": _plant_claude_code,
    "codex": _plant_codex,
}


# ---------------------------------------------------------------------------
# Shared fixture: a populated library + L6Store.
#
# Plants every wired adapter, exports each to <tmp>/agent-library/agents/,
# loads the store. Stubbed adapters are silently skipped at planter level
# via pytest.skip -- their fixtures will reappear once the planter lands.
# ---------------------------------------------------------------------------


@pytest.fixture
def federation(tmp_path, monkeypatch):
    """Return (L6Store, library_path, planted_agents) with all wired adapters loaded.

    Note on Path.home() handling: claude-code and codex planters monkey-patch
    Path.home() into their own slice of tmp_path. The patch is sequential
    inside this loop, so the last patched home wins after the fixture returns.
    That's fine because:
      (a) export_l5() runs *immediately* after each planter, before the next
          planter overwrites home,
      (b) convention-file adapters (copilot, cascade) and cursor accept an
          explicit dir parameter and never read Path.home() themselves, and
      (c) tests query the L6Store -- which already has all manifests on disk
          -- so the post-fixture state of Path.home() is irrelevant.
    """
    library = tmp_path / "agent-library"
    agents_dir = library / "agents"
    agents_dir.mkdir(parents=True)

    planted: list[str] = []
    for agent_id, planter in PLANTERS.items():
        agent_tmp = tmp_path / agent_id
        agent_tmp.mkdir()
        try:
            adapter = planter(agent_tmp, monkeypatch)
        except pytest.skip.Exception:
            # Stubbed planter -- skip this adapter, don't fail the whole test.
            continue
        manifest: L5Manifest = adapter.export_l5()
        write_l5_dict(manifest.to_dict(), agents_dir / f"{agent_id}.l5.yaml")
        planted.append(agent_id)

    store = L6Store(library_path=library)
    store.reload_all()
    return store, library, planted


# ---------------------------------------------------------------------------
# Tests.
# ---------------------------------------------------------------------------


def test_all_planted_adapters_visible_in_store(federation):
    """Sanity: every adapter that exported an L5 shows up in list_agents()."""
    store, _library, planted = federation
    assert set(store.list_agents()) >= set(planted)
    # Federation test is only meaningful with >=2 agents.
    assert len(planted) >= 2, (
        f"Only {len(planted)} adapters wired -- need at least two to test "
        f"federation. Wire the stubbed planters in this file."
    )


@pytest.mark.parametrize("agent_id", list(UNIQUE_MARKERS.keys()))
def test_unique_marker_round_trips_with_correct_attribution(federation, agent_id):
    """
    The contract: a fact known only to agent A is retrievable via L6Store
    and attributed only to agent A.
    """
    store, _library, planted = federation
    if agent_id not in planted:
        pytest.skip(f"{agent_id} planter is stubbed -- see PLANTERS")

    marker = UNIQUE_MARKERS[agent_id]
    matches = store.find_entity(marker, access_level=FEDERATION_ACCESS)

    assert matches, f"{agent_id} marker {marker!r} did not surface in L6"
    assert len(matches) == 1, (
        f"{marker!r} matched multiple entity rows; expected exactly one"
    )
    match = matches[0]
    assert match.agents == [agent_id], (
        f"{marker!r} should be attributed only to {agent_id}, "
        f"got {match.agents}"
    )


def test_shared_entity_aggregates_across_agents(federation):
    """
    The federation payoff: a fact known to multiple agents surfaces as
    ONE EntityMatch with multiple agents in match.agents and per-agent
    summaries in match.summaries.
    """
    store, _library, planted = federation
    matches = store.find_entity(SHARED_ENTITY, access_level=FEDERATION_ACCESS)

    assert matches, f"shared entity {SHARED_ENTITY!r} did not surface in L6"
    assert len(matches) == 1, (
        f"shared entity {SHARED_ENTITY!r} did not deduplicate across agents; "
        f"got {len(matches)} matches: {[(m.name, m.agents) for m in matches]}"
    )
    match = matches[0]

    # Every planted adapter publishes SHARED_ENTITY (each planter is responsible
    # for emitting it in whatever shape that adapter naturally produces).
    # If any adapter's entity extraction silently stops surfacing the marker,
    # this assertion catches it.
    expected_publishers = set(planted)
    assert set(match.agents) == expected_publishers, (
        f"shared-entity attribution drift: expected {expected_publishers}, "
        f"got {set(match.agents)}"
    )

    # Each convention-file adapter should contribute a distinct summary.
    for convention_agent in {"copilot", "cascade"} & expected_publishers:
        summary = match.summaries.get(convention_agent, "")
        assert SHARED_SUMMARY_PREFIX in summary, (
            f"{convention_agent} did not contribute its summary for "
            f"{SHARED_ENTITY!r}; got {summary!r}"
        )


def test_recognition_manifest_recovers_shared_entity_with_full_attribution(federation):
    """
    build_recognition_manifest() is the surface recognition-runtime
    consumes. The federation invariant we care about: every agent that
    published the shared entity is recoverable from the manifest with
    attribution, regardless of how many type-buckets the entity spans.

    Stricter dedupe (collapse across types into one row) is a live
    product question -- see "Cross-type entity-dedupe question" in
    PROJECTS/NEUROLAYER/NOTES.md. Current contract is (name, type),
    which produces multiple rows when adapters disagree on type
    (e.g. Codex emits 'topic', Cursor infers 'project'). This test
    asserts the weaker invariant that holds either way: across all
    rows whose name matches, the union of source_agents covers every
    planted publisher.
    """
    store, _library, planted = federation
    rec = store.build_recognition_manifest(access_level=FEDERATION_ACCESS)

    shared_rows = [
        e
        for e in rec["known_entities"]
        if e.get("name", "").strip().lower() == SHARED_ENTITY.lower()
    ]
    assert shared_rows, "shared entity missing from recognition manifest"

    attributed: set[str] = set()
    for row in shared_rows:
        for agent in row.get("source_agents") or []:
            attributed.add(agent)
    assert attributed == set(planted), (
        f"recognition manifest dropped attribution for some agents: "
        f"expected {set(planted)}, got {attributed} "
        f"across {len(shared_rows)} row(s)"
    )


def test_unknown_entity_returns_empty(federation):
    """Negative case: a fact no agent has published returns no matches."""
    store, _library, _planted = federation
    assert (
        store.find_entity("NoAgentEverPublishedThis", access_level=FEDERATION_ACCESS)
        == []
    )
