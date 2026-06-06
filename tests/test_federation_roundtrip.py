"""
Bourdon federation round-trip integration tests.

Scope
-----
These tests assert the contract that ties Bourdon together:

    each participant's export_l5() output is queryable through L6Store with
    correct attribution, visibility filtering, and cross-agent aggregation.

Per-participant unit suites verify only the participant -> L5 leg. The L6Store
unit suite verifies only the store -> query leg with synthetic manifests.
Nothing else covers the seam where they meet. If a participant silently
changes the shape of its L5 in a way the store does not expect, the unit
tests stay green and the federation product silently breaks.

This is Layer 1 of the cross-agent test plan recorded in
PROJECTS/NEUROLAYER/NOTES.md (2026-05-11 entry on claude-brain).
Layers 2 and 3 (`bourdon dogfood` CLI + public acceptance scenario)
live outside the test suite.

Coverage as of v0.4.1
---------------------
Wired end-to-end:
    copilot   (convention-file participant, plants memory.md)
    cascade   (convention-file participant, plants memory.md)
    cursor    (SQLite participant, seeds state.vscdb directly)

Stubbed (TODO -- fixture plumbing only, the assertions below already cover them):
    claude-code  (needs Path.home() monkeypatch over a 3-source tree)
    codex        (needs Path.home() monkeypatch over sessions+memories+brain)
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from pathlib import Path

import pytest

from participants.base import L5Manifest
from participants.cascade import CascadeParticipant
from participants.claude_code import ClaudeCodeParticipant
from participants.codex import CodexParticipant
from participants.copilot import CopilotParticipant
from participants.cursor import CursorParticipant
from core.l5_io import write_l5_dict
from core.l6_store import L6Store

# ---------------------------------------------------------------------------
# Marker facts -- a distinct, easily-grepped entity per participant.
#
# The round-trip test plants each participant with a marker entity whose name is
# unique to that participant (so we can prove the L5 reached L6 and is attributed
# to the right agent), plus a shared entity ("Bourdon") that every participant
# knows about (so we can prove cross-agent aggregation works).
# ---------------------------------------------------------------------------

SHARED_ENTITY = "Bourdon"
SHARED_SUMMARY_PREFIX = "Cross-agent memory federation, as seen by"

# Federation queries default to access_level="public", but three of five
# participants (codex always, copilot + cursor by default policy) tag entities
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
# Per-participant fixture planters.
#
# Each helper accepts `tmp_path` and returns a configured participant whose
# `export_l5()` will produce a manifest containing:
#   - one entity named UNIQUE_MARKERS[agent_id] (attribution proof)
#   - one entity named SHARED_ENTITY            (federation proof)
# Plus whatever incidental rows the participant naturally produces from the
# fixture (sessions, project entities, etc.) -- those are not asserted on,
# only the marker shape is contract.
# ---------------------------------------------------------------------------


# Convention-file and Cursor planters take their explicit dir parameter, so
# they ignore `monkeypatch` and `Path.home()`. Path.home()-dependent planters
# (claude-code, codex) use `monkeypatch` to redirect home into their slice
# of tmp_path. The federation fixture passes both args to every planter so
# the signature is uniform.

def _plant_copilot(tmp_path: Path, monkeypatch) -> CopilotParticipant:
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
        # Same type as Cursor's inferred entity. With Finding #1 resolved
        # (dedupe by name only), this no longer matters for the dedupe
        # behavior -- different types would still collapse into one row
        # with a merged `types` list. Kept as-is for fixture readability.
        "    type: project\n"
        f"    summary: {SHARED_SUMMARY_PREFIX} Copilot\n"
        "    tags: [shared, federation-test]\n"
        "sessions: []\n"
        "---\n"
        "Freeform body intentionally left short.\n",
        encoding="utf-8",
    )
    return CopilotParticipant(copilot_dir=d)


def _plant_cascade(tmp_path: Path, monkeypatch) -> CascadeParticipant:
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
        # Same type as Cursor's inferred entity. With Finding #1 resolved
        # (dedupe by name only), this no longer matters for the dedupe
        # behavior -- different types would still collapse into one row
        # with a merged `types` list. Kept as-is for fixture readability.
        "    type: project\n"
        f"    summary: {SHARED_SUMMARY_PREFIX} Cascade\n"
        "    tags: [shared, federation-test]\n"
        "sessions: []\n"
        "---\n"
        "Freeform body intentionally left short.\n",
        encoding="utf-8",
    )
    return CascadeParticipant(cascade_dir=d)


def _plant_cursor(tmp_path: Path, monkeypatch) -> CursorParticipant:
    cursor_dir = tmp_path / "Cursor"
    (cursor_dir / "User" / "globalStorage").mkdir(parents=True)
    workspace = cursor_dir / "User" / "workspaceStorage" / "fedtest"
    workspace.mkdir(parents=True)
    db = workspace / "state.vscdb"

    # Cursor's participant infers project entities from composer workspacePaths.
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
    return CursorParticipant(cursor_dir=cursor_dir)


def _plant_claude_code(tmp_path: Path, monkeypatch) -> ClaudeCodeParticipant:
    """
    Claude Code participant reads from three sources rooted at $HOME:
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

    return ClaudeCodeParticipant()


def _plant_codex(tmp_path: Path, monkeypatch) -> CodexParticipant:
    """
    Codex participant reads ~/.codex/session_index.jsonl; each entry's
    thread_name becomes a known-entity. Index-only is sufficient for
    entity extraction -- rollout files only affect session bodies.
    """
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.delenv("CODEX_HOME", raising=False)
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

    return CodexParticipant()


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
# Plants every wired participant, exports each to <tmp>/agent-library/agents/,
# loads the store. Stubbed participants are silently skipped at planter level
# via pytest.skip -- their fixtures will reappear once the planter lands.
# ---------------------------------------------------------------------------


@pytest.fixture
def federation(tmp_path, monkeypatch):
    """Return (L6Store, library_path, planted_agents) with all wired participants loaded.

    Note on Path.home() handling: claude-code and codex planters monkey-patch
    Path.home() into their own slice of tmp_path. The patch is sequential
    inside this loop, so the last patched home wins after the fixture returns.
    That's fine because:
      (a) export_l5() runs *immediately* after each planter, before the next
          planter overwrites home,
      (b) convention-file participants (copilot, cascade) and cursor accept an
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
            participant = planter(agent_tmp, monkeypatch)
        except pytest.skip.Exception:
            # Stubbed planter -- skip this participant, don't fail the whole test.
            continue
        manifest: L5Manifest = participant.export_l5()
        write_l5_dict(manifest.to_dict(), agents_dir / f"{agent_id}.l5.yaml")
        planted.append(agent_id)

    store = L6Store(library_path=library)
    store.reload_all()
    return store, library, planted


# ---------------------------------------------------------------------------
# Tests.
# ---------------------------------------------------------------------------


def test_all_planted_participants_visible_in_store(federation):
    """Sanity: every participant that exported an L5 shows up in list_agents()."""
    store, _library, planted = federation
    assert set(store.list_agents()) >= set(planted)
    # Federation test is only meaningful with >=2 agents.
    assert len(planted) >= 2, (
        f"Only {len(planted)} participants wired -- need at least two to test "
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

    # Every planted participant publishes SHARED_ENTITY (each planter is responsible
    # for emitting it in whatever shape that participant naturally produces).
    # If any participant's entity extraction silently stops surfacing the marker,
    # this assertion catches it.
    expected_publishers = set(planted)
    assert set(match.agents) == expected_publishers, (
        f"shared-entity attribution drift: expected {expected_publishers}, "
        f"got {set(match.agents)}"
    )

    # Each convention-file participant should contribute a distinct summary.
    for convention_agent in {"copilot", "cascade"} & expected_publishers:
        summary = match.summaries.get(convention_agent, "")
        assert SHARED_SUMMARY_PREFIX in summary, (
            f"{convention_agent} did not contribute its summary for "
            f"{SHARED_ENTITY!r}; got {summary!r}"
        )


def test_recognition_manifest_collapses_shared_entity_to_one_row(federation):
    """
    build_recognition_manifest() is the surface recognition-runtime
    consumes. After Finding #1's resolution (dedupe by name only with
    types as a list), a shared entity collapses to exactly one row even
    when participants disagree on its type (Codex emits 'topic', Cursor
    infers 'project', convention-file participants emit whatever the user
    wrote in memory.md).

    The strong contract: one row per name, all source_agents merged,
    all distinct types preserved in the `types` field.
    """
    store, _library, planted = federation
    rec = store.build_recognition_manifest(access_level=FEDERATION_ACCESS)

    shared_rows = [
        e
        for e in rec["known_entities"]
        if e.get("name", "").strip().lower() == SHARED_ENTITY.lower()
    ]
    assert len(shared_rows) == 1, (
        f"shared entity should dedupe to exactly one row across participants "
        f"(Finding #1 resolution); got {len(shared_rows)}: "
        f"{[(r['name'], r.get('types')) for r in shared_rows]}"
    )
    row = shared_rows[0]
    assert set(row.get("source_agents") or []) == set(planted), (
        f"row's source_agents should cover every planted publisher: "
        f"expected {set(planted)}, got {set(row.get('source_agents') or [])}"
    )
    # `types` should list every distinct type any participant emitted.
    assert isinstance(row.get("types"), list)
    assert len(row["types"]) >= 1
    # `type` (singular) still present for backward-compat with
    # recognition_runtime._single_match_recognition and similar callers.
    assert isinstance(row.get("type"), str)
    assert row["type"] in row["types"]


def test_unknown_entity_returns_empty(federation):
    """Negative case: a fact no agent has published returns no matches."""
    store, _library, _planted = federation
    assert (
        store.find_entity("NoAgentEverPublishedThis", access_level=FEDERATION_ACCESS)
        == []
    )
