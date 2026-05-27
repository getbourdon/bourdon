from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import yaml

from core.codex_turn_compiler import compile_codex_turn


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


def _codex_home_with_stage1(tmp_path: Path, *, degraded: bool = False) -> Path:
    codex_home = tmp_path / ".codex"
    codex_home.mkdir()
    with sqlite3.connect(codex_home / "state_5.sqlite") as conn:
        conn.execute(
            "CREATE TABLE threads "
            "(id TEXT PRIMARY KEY, memory_mode TEXT, archived INTEGER)"
        )
        conn.execute("CREATE TABLE stage1_outputs (thread_id TEXT PRIMARY KEY, raw_memory TEXT)")
        conn.execute(
            "CREATE TABLE jobs "
            "(kind TEXT, job_key TEXT, status TEXT, retry_remaining INTEGER, last_error TEXT)"
        )
        conn.execute("INSERT INTO threads VALUES ('thread-1', 'enabled', 0)")
        if degraded:
            conn.execute(
                "INSERT INTO jobs VALUES (?, ?, ?, ?, ?)",
                (
                    "memory_stage1",
                    "thread-1",
                    "error",
                    0,
                    "Codex ran out of room in the model's context window.",
                ),
            )
        else:
            conn.execute("INSERT INTO stage1_outputs VALUES ('thread-1', 'raw')")
            conn.execute(
                "INSERT INTO jobs VALUES ('memory_stage1', 'thread-1', 'done', 0, NULL)"
            )
    return codex_home


def test_compile_turn_ranks_prompt_entity_match_above_recency_only(tmp_path):
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

    brief = compile_codex_turn(
        "Keep working on Bourdon recognition",
        library_path=library,
        codex_home=_codex_home_with_stage1(tmp_path),
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
        "codex",
        entities=[
            {
                "name": "ShipStable",
                "type": "project",
                "summary": "Repo-specific launch work.",
                "visibility": "team",
            }
        ],
    )

    brief = compile_codex_turn(
        "What should I do next?",
        cwd=repo,
        library_path=library,
        codex_home=_codex_home_with_stage1(tmp_path),
    )

    assert brief.repo.name == "shipstable"
    assert brief.items[0].name == "ShipStable"
    assert "repo" in brief.items[0].reason.lower()


def test_compile_turn_does_not_inject_repo_context_for_unrelated_prompt(tmp_path):
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
        sessions=[
            {
                "date": "2026-05-27",
                "project_focus": ["Bourdon"],
                "key_actions": ["Recent repo work."],
                "cwd": str(repo),
                "visibility": "team",
            }
        ],
    )

    brief = compile_codex_turn(
        "What's the weather like?",
        cwd=repo,
        library_path=library,
        codex_home=_codex_home_with_stage1(tmp_path),
    )

    assert brief.items == []
    assert brief.routing["mode"] == "observe"
    assert brief.delivery["repo_overlay_block"] == ""


def test_compile_turn_uses_l6_cross_agent_context_without_stage1(tmp_path):
    library = tmp_path / "agent-library"
    entity = {
        "name": "Bourdon",
        "type": "project",
        "summary": "Shared recognition context.",
        "visibility": "team",
    }
    _write_manifest(library, "codex", entities=[entity])
    _write_manifest(library, "claude-code", entities=[entity])

    brief = compile_codex_turn(
        "Bourdon plan",
        library_path=library,
        codex_home=tmp_path / "missing-codex-home",
    )

    assert brief.health.native_stage1 == "unknown"
    assert brief.items[0].name == "Bourdon"
    assert brief.items[0].source == "l6_federation"
    assert brief.items[0].source_agents == ["claude-code", "codex"]
    assert "cross-agent agreement" in brief.items[0].reason


def test_compile_turn_marks_stage1_degraded_but_still_returns_brief(tmp_path):
    library = tmp_path / "agent-library"
    _write_manifest(
        library,
        "codex",
        entities=[
            {
                "name": "Bourdon",
                "type": "project",
                "summary": "Recognition survives degraded native memory.",
                "visibility": "team",
            }
        ],
    )

    brief = compile_codex_turn(
        "Bourdon",
        library_path=library,
        codex_home=_codex_home_with_stage1(tmp_path, degraded=True),
    )

    assert brief.health.native_stage1 == "degraded"
    assert brief.items[0].name == "Bourdon"
    assert "native Stage 1 is degraded" in brief.delivery["explicit_text"]


def test_compile_turn_filters_private_items_at_team_access(tmp_path):
    library = tmp_path / "agent-library"
    _write_manifest(
        library,
        "codex",
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
        sessions=[
            {
                "date": "2026-05-27",
                "project_focus": ["Unrelated"],
                "key_actions": ["Fresh but lower-signal work."],
                "visibility": "team",
            }
        ],
    )

    brief = compile_codex_turn(
        "Private Anchor and Public Anchor",
        library_path=library,
        codex_home=_codex_home_with_stage1(tmp_path),
        access_level="team",
    )

    names = {item.name for item in brief.items}
    assert "Private Anchor" not in names
    assert "Public Anchor" in names


def test_compile_turn_redacts_credential_like_text(tmp_path):
    library = tmp_path / "agent-library"
    _write_manifest(
        library,
        "codex",
        entities=[
            {
                "name": "Bourdon",
                "type": "project",
                "summary": "api_key should never be echoed.",
                "visibility": "team",
            }
        ],
    )

    brief = compile_codex_turn(
        "Bourdon",
        library_path=library,
        codex_home=_codex_home_with_stage1(tmp_path),
    )

    assert brief.items[0].summary == "[redacted credential-like text]"
    assert "api_key" not in brief.delivery["explicit_text"]


def test_compile_turn_respects_max_items_and_max_chars(tmp_path):
    library = tmp_path / "agent-library"
    _write_manifest(
        library,
        "codex",
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

    brief = compile_codex_turn(
        "Bourdon",
        library_path=library,
        codex_home=_codex_home_with_stage1(tmp_path),
        max_items=2,
        max_chars=420,
    )

    assert len(brief.items) == 2
    assert len(brief.delivery["explicit_text"]) <= 420


def test_compile_turn_latency_stays_small_for_fixture_library(tmp_path):
    library = tmp_path / "agent-library"
    _write_manifest(
        library,
        "codex",
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
    brief = compile_codex_turn(
        "Project 42",
        library_path=library,
        codex_home=_codex_home_with_stage1(tmp_path),
    )
    elapsed_ms = (time.perf_counter() - start) * 1_000

    assert brief.items
    assert elapsed_ms < 200


def test_compile_turn_routes_high_confidence_brief_to_explicit_and_mcp(tmp_path):
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
        "claude-code",
        entities=[
            {
                "name": "Bourdon",
                "type": "project",
                "summary": "Cross-agent planning context.",
                "visibility": "team",
            }
        ],
    )

    brief = compile_codex_turn(
        "Bourdon recognition orchestration",
        library_path=library,
        codex_home=_codex_home_with_stage1(tmp_path, degraded=True),
    )

    assert brief.routing["mode"] == "inject"
    assert brief.routing["primary_surface"] == "explicit_pre_turn"
    assert "mcp" in brief.routing["surfaces"]
    assert "native_stage1_primary" in brief.routing["suppressed_surfaces"]
    assert brief.routing["confidence"] == "high"


def test_compile_turn_renders_repo_overlay_candidate_when_repo_is_known(tmp_path):
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

    brief = compile_codex_turn(
        "Bourdon recognition orchestration",
        cwd=repo,
        library_path=library,
        codex_home=_codex_home_with_stage1(tmp_path),
    )

    overlay = brief.delivery["repo_overlay_block"]
    assert "repo_overlay_candidate" in brief.routing["surfaces"]
    assert "<!-- BEGIN BOURDON REPO OVERLAY CANDIDATE -->" in overlay
    assert "Repo overlay candidate for bourdon" in overlay
    assert "Bourdon turn recognition brief" in overlay


def test_compile_turn_trace_explains_selected_and_ignored_sources(tmp_path):
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
            },
            {
                "name": "Bourdon Sidecar",
                "type": "project",
                "summary": "Different project.",
                "visibility": "team",
            },
        ],
        sessions=[
            {
                "date": "2026-05-27",
                "project_focus": ["Unrelated"],
                "key_actions": ["Fresh but lower-signal work."],
                "visibility": "team",
            }
        ],
    )

    brief = compile_codex_turn(
        "Bourdon",
        library_path=library,
        codex_home=_codex_home_with_stage1(tmp_path),
        max_items=1,
    )

    assert brief.trace["routing_decision"]["primary_surface"] == "explicit_pre_turn"
    assert brief.trace["surface_health"]["candidate_count"] >= 2
    assert brief.trace["selected_items"][0]["name"] == "Bourdon"
    assert "prompt" in brief.trace["selected_items"][0]["dominant_components"]
    assert brief.trace["source_mix"]["ignored"]
