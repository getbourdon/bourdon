from __future__ import annotations

import json
import sqlite3
import sys
import time
from pathlib import Path

import pytest
import yaml

from core.cascade_turn_compiler import compile_cascade_turn


def _write_manifest(
    library: Path,
    agent_id: str,
    entities: list[dict] | None = None,
    sessions: list[dict] | None = None,
) -> None:
    agents_dir = library / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "spec_version": "0.1",
        "agent": {"id": agent_id, "type": "code-assistant"},
        "last_updated": "2026-05-27T12:00:00+00:00",
        "known_entities": entities or [],
        "recent_sessions": sessions or [],
    }
    (agents_dir / f"{agent_id}.l5.yaml").write_text(
        yaml.safe_dump(manifest, sort_keys=False),
        encoding="utf-8",
    )


def _empty_windsurf(tmp_path: Path) -> Path:
    """A Windsurf data dir override that resolves to nothing (no global DB)."""
    return tmp_path / "no-windsurf"


def _workspace_with_plan(
    tmp_path: Path,
    *,
    plan_title: str = "Bourdon recognition plan",
    plan_body: str = "Wire the Cascade turn compiler onto the shared engine.",
    workflow: tuple[str, str] | None = None,
) -> Path:
    """Create a workspace dir with .windsurf/plans (+ optional workflow)."""
    ws = tmp_path / "ws"
    plans = ws / ".windsurf" / "plans"
    plans.mkdir(parents=True)
    (plans / "plan-1.md").write_text(f"# {plan_title}\n\n{plan_body}\n", encoding="utf-8")
    if workflow is not None:
        wf_dir = ws / ".windsurf" / "workflows"
        wf_dir.mkdir(parents=True)
        filename, description = workflow
        (wf_dir / filename).write_text(
            f"---\ndescription: {description}\n---\n\nsteps...\n", encoding="utf-8"
        )
    return ws


def _windsurf_with_editor_session(tmp_path: Path, *, title: str) -> Path:
    """Build a minimal Windsurf global state DB with one Cascade editor session."""
    data_dir = tmp_path / "Windsurf"
    global_storage = data_dir / "User" / "globalStorage"
    global_storage.mkdir(parents=True)
    editor_states = {
        "space-1": {
            "serializedGrid": {
                "root": {
                    "type": "leaf",
                    "data": {
                        "editors": [
                            {
                                "id": "cascadePanel",
                                "value": json.dumps(
                                    {"title": title, "resource": "file:///tmp/x"}
                                ),
                            }
                        ]
                    },
                }
            }
        }
    }
    with sqlite3.connect(global_storage / "state.vscdb") as conn:
        conn.execute("CREATE TABLE ItemTable (key TEXT PRIMARY KEY, value TEXT)")
        conn.execute(
            "INSERT INTO ItemTable VALUES (?, ?)",
            ("windsurfSpace.editorStates", json.dumps(editor_states)),
        )
    return data_dir


# -- Parity with the Codex/Claude compilers (shared engine, Cascade surfaces) -


def test_compile_turn_ranks_prompt_entity_match_above_recency_only(tmp_path):
    library = tmp_path / "agent-library"
    _write_manifest(
        library,
        "cascade",
        entities=[
            {
                "name": "Bourdon",
                "type": "project",
                "summary": "Recognition orchestration substrate.",
                "visibility": "team",
            },
            {
                "name": "Recent But Unrelated",
                "type": "project",
                "summary": "Fresh unrelated work.",
                "visibility": "team",
            },
        ],
        sessions=[
            {
                "date": "2026-05-27",
                "project_focus": ["Recent But Unrelated"],
                "key_actions": ["Recent unrelated session."],
                "visibility": "team",
            }
        ],
    )

    brief = compile_cascade_turn(
        "Keep working on Bourdon recognition",
        cwd=tmp_path,
        library_path=library,
        windsurf_data_dir=_empty_windsurf(tmp_path),
    )

    assert brief.items[0].name == "Bourdon"
    assert {item.name for item in brief.items} == {"Bourdon"}


def test_compile_turn_uses_cwd_repo_identity_when_prompt_is_vague(tmp_path):
    repo = tmp_path / "shipstable"
    (repo / ".git").mkdir(parents=True)
    (repo / ".git" / "config").write_text(
        '[remote "origin"]\nurl = git@github.com:radlab/shipstable.git\n',
        encoding="utf-8",
    )
    library = tmp_path / "agent-library"
    _write_manifest(
        library,
        "cascade",
        entities=[
            {
                "name": "ShipStable",
                "type": "project",
                "summary": "Repo-specific launch work.",
                "visibility": "team",
            }
        ],
    )

    brief = compile_cascade_turn(
        "What should I do next?",
        cwd=repo,
        library_path=library,
        windsurf_data_dir=_empty_windsurf(tmp_path),
    )

    assert brief.repo.name == "shipstable"
    assert brief.items[0].name == "ShipStable"
    assert "repo" in brief.items[0].reason.lower()


def test_compile_turn_does_not_inject_for_unrelated_prompt(tmp_path):
    repo = tmp_path / "bourdon"
    (repo / ".git").mkdir(parents=True)
    library = tmp_path / "agent-library"
    _write_manifest(
        library,
        "cascade",
        entities=[
            {
                "name": "Bourdon",
                "type": "project",
                "summary": "Recognition orchestration substrate.",
                "visibility": "team",
            }
        ],
    )

    brief = compile_cascade_turn(
        "What's the weather like?",
        cwd=repo,
        library_path=library,
        windsurf_data_dir=_empty_windsurf(tmp_path),
    )

    assert brief.items == []
    assert brief.routing["mode"] == "observe"
    assert brief.delivery["repo_overlay_block"] == ""


def test_compile_turn_uses_l6_cross_agent_context_without_native_state(tmp_path):
    library = tmp_path / "agent-library"
    entity = {
        "name": "Bourdon",
        "type": "project",
        "summary": "Shared recognition context.",
        "visibility": "team",
    }
    _write_manifest(library, "codex", entities=[entity])
    _write_manifest(library, "cascade", entities=[entity])

    brief = compile_cascade_turn(
        "Bourdon plan",
        cwd=tmp_path,
        library_path=library,
        windsurf_data_dir=_empty_windsurf(tmp_path),
    )

    assert brief.health.value == "unknown"
    assert brief.to_dict()["health"]["native_state"] == "unknown"
    assert brief.items[0].name == "Bourdon"
    assert brief.items[0].source == "l6_federation"
    assert brief.items[0].source_agents == ["cascade", "codex"]
    assert "cross-agent agreement" in brief.items[0].reason


def test_compile_turn_filters_private_items_at_team_access(tmp_path):
    library = tmp_path / "agent-library"
    _write_manifest(
        library,
        "cascade",
        entities=[
            {
                "name": "Private Anchor",
                "type": "project",
                "summary": "Should not leak.",
                "visibility": "private",
            },
            {
                "name": "Public Anchor",
                "type": "project",
                "summary": "Visible at team.",
                "visibility": "team",
            },
        ],
    )

    brief = compile_cascade_turn(
        "Private Anchor and Public Anchor",
        cwd=tmp_path,
        library_path=library,
        windsurf_data_dir=_empty_windsurf(tmp_path),
        access_level="team",
    )

    names = {item.name for item in brief.items}
    assert "Private Anchor" not in names
    assert "Public Anchor" in names


def test_compile_turn_redacts_credential_like_text(tmp_path):
    library = tmp_path / "agent-library"
    _write_manifest(
        library,
        "cascade",
        entities=[
            {
                "name": "Bourdon",
                "type": "project",
                "summary": "api_key should never be echoed.",
                "visibility": "team",
            }
        ],
    )

    brief = compile_cascade_turn(
        "Bourdon",
        cwd=tmp_path,
        library_path=library,
        windsurf_data_dir=_empty_windsurf(tmp_path),
    )

    assert brief.items[0].summary == "[redacted credential-like text]"
    assert "api_key" not in brief.delivery["explicit_text"]


def test_compile_turn_respects_max_items_and_max_chars(tmp_path):
    library = tmp_path / "agent-library"
    _write_manifest(
        library,
        "cascade",
        entities=[
            {
                "name": f"Bourdon Anchor {index}",
                "type": "project",
                "summary": "Recognition context.",
                "visibility": "team",
            }
            for index in range(5)
        ],
    )

    brief = compile_cascade_turn(
        "Bourdon",
        cwd=tmp_path,
        library_path=library,
        windsurf_data_dir=_empty_windsurf(tmp_path),
        max_items=2,
        max_chars=420,
    )

    assert len(brief.items) == 2
    assert len(brief.delivery["explicit_text"]) <= 420


def test_compile_turn_routes_high_confidence_brief_to_explicit_and_mcp(tmp_path):
    repo = tmp_path / "bourdon"
    (repo / ".git").mkdir(parents=True)
    library = tmp_path / "agent-library"
    _write_manifest(
        library,
        "codex",
        entities=[
            {
                "name": "Bourdon",
                "type": "project",
                "summary": "Recognition orchestration substrate.",
                "visibility": "team",
            }
        ],
    )
    _write_manifest(
        library,
        "cascade",
        entities=[
            {
                "name": "Bourdon",
                "type": "project",
                "summary": "Cross-agent planning context.",
                "visibility": "team",
            }
        ],
    )

    brief = compile_cascade_turn(
        "Bourdon recognition orchestration",
        cwd=repo,
        library_path=library,
        windsurf_data_dir=_empty_windsurf(tmp_path),
    )

    assert brief.routing["mode"] == "inject"
    assert brief.routing["primary_surface"] == "explicit_pre_turn"
    assert "mcp" in brief.routing["surfaces"]
    assert brief.routing["confidence"] == "high"


def test_compile_turn_renders_repo_overlay_candidate_when_repo_is_known(tmp_path):
    repo = tmp_path / "bourdon"
    (repo / ".git").mkdir(parents=True)
    library = tmp_path / "agent-library"
    _write_manifest(
        library,
        "cascade",
        entities=[
            {
                "name": "Bourdon",
                "type": "project",
                "summary": "Recognition orchestration substrate.",
                "visibility": "team",
            }
        ],
    )

    brief = compile_cascade_turn(
        "Bourdon recognition orchestration",
        cwd=repo,
        library_path=library,
        windsurf_data_dir=_empty_windsurf(tmp_path),
    )

    overlay = brief.delivery["repo_overlay_block"]
    assert "repo_overlay_candidate" in brief.routing["surfaces"]
    assert "<!-- BEGIN BOURDON REPO OVERLAY CANDIDATE -->" in overlay
    assert "Repo overlay candidate for bourdon" in overlay


def test_compile_turn_emits_cascade_schema_version_and_health_key(tmp_path):
    library = tmp_path / "agent-library"
    _write_manifest(
        library,
        "cascade",
        entities=[
            {
                "name": "Bourdon",
                "type": "project",
                "summary": "Recognition orchestration substrate.",
                "visibility": "team",
            }
        ],
    )

    brief = compile_cascade_turn(
        "Bourdon",
        cwd=tmp_path,
        library_path=library,
        windsurf_data_dir=_empty_windsurf(tmp_path),
    )

    data = brief.to_dict()
    assert data["schema_version"] == "cascade-turn-brief/v1"
    assert "native_state" in data["health"]
    assert "native_stage1" not in data["health"]
    assert data["delivery"]["mcp_payload"]["schema_version"] == "cascade-turn-brief/v1"


# -- Cascade-native (Windsurf) surfaces ---------------------------------------


def test_compile_turn_surfaces_windsurf_plan(tmp_path):
    ws = _workspace_with_plan(tmp_path, plan_title="Bourdon recognition plan")
    library = tmp_path / "agent-library"
    _write_manifest(library, "cascade", entities=[])

    brief = compile_cascade_turn(
        "Bourdon recognition plan",
        cwd=ws,
        library_path=library,
        windsurf_data_dir=_empty_windsurf(tmp_path),
    )

    assert brief.items, "expected the active .windsurf plan to surface"
    top = brief.items[0]
    assert top.kind == "plan"
    assert top.source == "windsurf_workspace"
    assert top.source_agents == ["cascade"]
    assert top.name == "Bourdon recognition plan"
    assert brief.health.value == "available"


def test_compile_turn_surfaces_windsurf_workflow(tmp_path):
    ws = _workspace_with_plan(
        tmp_path,
        plan_title="Unrelated plan",
        workflow=("ship-bourdon.md", "Ship the Bourdon release"),
    )
    library = tmp_path / "agent-library"
    _write_manifest(library, "cascade", entities=[])

    brief = compile_cascade_turn(
        "ship-bourdon",
        cwd=ws,
        library_path=library,
        windsurf_data_dir=_empty_windsurf(tmp_path),
    )

    workflow_items = [item for item in brief.items if item.kind == "workflow"]
    assert workflow_items, "expected the .windsurf workflow to surface"
    assert workflow_items[0].name == "ship-bourdon"
    assert workflow_items[0].source == "windsurf_workspace"


def test_compile_turn_surfaces_cascade_editor_session_from_state_db(tmp_path):
    data_dir = _windsurf_with_editor_session(
        tmp_path, title="Bourdon recognition compiler session"
    )
    library = tmp_path / "agent-library"
    _write_manifest(library, "cascade", entities=[])

    brief = compile_cascade_turn(
        "Bourdon recognition compiler session",
        cwd=tmp_path,
        library_path=library,
        windsurf_data_dir=data_dir,
    )

    threads = [item for item in brief.items if item.source == "windsurf_native"]
    assert threads, "expected the Cascade editor session to surface as a thread"
    assert threads[0].kind == "thread"
    assert threads[0].source_agents == ["cascade"]
    assert "Bourdon recognition compiler session" in threads[0].name
    assert brief.health.value == "available"


def test_compile_turn_native_state_unknown_when_no_windsurf(tmp_path):
    library = tmp_path / "agent-library"
    _write_manifest(
        library,
        "cascade",
        entities=[
            {
                "name": "Bourdon",
                "type": "project",
                "summary": "Recognition orchestration substrate.",
                "visibility": "team",
            }
        ],
    )

    brief = compile_cascade_turn(
        "Bourdon",
        cwd=tmp_path,
        library_path=library,
        windsurf_data_dir=_empty_windsurf(tmp_path),
    )

    assert brief.health.value == "unknown"
    assert brief.diagnostics["native_state"]["available"] is False


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="GH Actions Windows runner latency is too variable for absolute-time perf assertion",
)
def test_compile_turn_latency_stays_small_for_fixture_library(tmp_path):
    library = tmp_path / "agent-library"
    _write_manifest(
        library,
        "cascade",
        entities=[
            {
                "name": f"Project {index}",
                "type": "project",
                "summary": "Fixture project.",
                "visibility": "team",
            }
            for index in range(120)
        ],
    )

    start = time.perf_counter()
    brief = compile_cascade_turn(
        "Project 42",
        cwd=tmp_path,
        library_path=library,
        windsurf_data_dir=_empty_windsurf(tmp_path),
    )
    elapsed_ms = (time.perf_counter() - start) * 1_000

    assert brief.items
    assert elapsed_ms < 200
