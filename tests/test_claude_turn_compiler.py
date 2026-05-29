from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest
import yaml

from core.claude_turn_compiler import _decode_claude_slug, compile_claude_turn

# A Windows-shaped Claude project slug + the path it encodes. Claude replaces
# path separators (and the drive colon) with "-" when naming the per-workspace
# project directory under ~/.claude/projects/. These are plain strings, so the
# fixtures below exercise Windows-shaped project/memory paths on any CI OS.
WINDOWS_SLUG = "C--Users-cumul-repos-bourdon"
WINDOWS_CWD = r"C:\Users\cumul\repos\bourdon"


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


def _projects_base(
    tmp_path: Path,
    *,
    slug: str = WINDOWS_SLUG,
    memory_md: bool = True,
    oversized: bool = False,
    transcripts: list[list[dict]] | None = None,
) -> Path:
    """Build a fake ``~/.claude/projects`` base with a Windows-shaped slug.

    ``oversized`` writes a MEMORY.md past the soft size limit so the native
    memory health classifies as ``degraded`` (the Claude analogue of Codex's
    degraded native Stage 1). ``transcripts`` writes ``*.jsonl`` session files,
    each a list of JSON-serializable record dicts.
    """
    base = tmp_path / ".claude" / "projects"
    project_dir = base / slug
    project_dir.mkdir(parents=True)
    if memory_md:
        memory_dir = project_dir / "memory"
        memory_dir.mkdir()
        content = "# Project Memory Index\n\n- [Bourdon](bourdon.md) - federation\n"
        if oversized:
            content += "x" * 26_000  # over _MEMORY_MD_SOFT_LIMIT_BYTES (25_000)
        (memory_dir / "MEMORY.md").write_text(content, encoding="utf-8")
    for index, records in enumerate(transcripts or []):
        lines = "\n".join(json.dumps(record) for record in records)
        (project_dir / f"session-{index}.jsonl").write_text(lines, encoding="utf-8")
    return base


def _available(tmp_path: Path) -> Path:
    return _projects_base(tmp_path)


def _degraded(tmp_path: Path) -> Path:
    return _projects_base(tmp_path, oversized=True)


# -- Parity with the Codex compiler (shared engine, Claude surfaces) ----------


def test_compile_turn_ranks_prompt_entity_match_above_recency_only(tmp_path):
    library = tmp_path / "agent-library"
    _write_manifest(
        library,
        "claude-code",
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

    brief = compile_claude_turn(
        "Keep working on Bourdon recognition",
        cwd=tmp_path,
        library_path=library,
        projects_base=_available(tmp_path),
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
        "claude-code",
        entities=[
            {
                "name": "ShipStable",
                "type": "project",
                "summary": "Repo-specific launch work.",
                "visibility": "team",
            }
        ],
    )

    brief = compile_claude_turn(
        "What should I do next?",
        cwd=repo,
        library_path=library,
        projects_base=_available(tmp_path),
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
        "claude-code",
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

    brief = compile_claude_turn(
        "What's the weather like?",
        cwd=repo,
        library_path=library,
        projects_base=_available(tmp_path),
    )

    assert brief.items == []
    assert brief.routing["mode"] == "observe"
    assert brief.delivery["repo_overlay_block"] == ""


def test_compile_turn_uses_l6_cross_agent_context_without_native_memory(tmp_path):
    library = tmp_path / "agent-library"
    entity = {
        "name": "Bourdon",
        "type": "project",
        "summary": "Shared recognition context.",
        "visibility": "team",
    }
    _write_manifest(library, "codex", entities=[entity])
    _write_manifest(library, "claude-code", entities=[entity])

    brief = compile_claude_turn(
        "Bourdon plan",
        cwd=tmp_path,
        library_path=library,
        projects_base=tmp_path / "missing-projects-base",
    )

    assert brief.health.value == "unknown"
    assert brief.to_dict()["health"]["native_memory"] == "unknown"
    assert brief.items[0].name == "Bourdon"
    assert brief.items[0].source == "l6_federation"
    assert brief.items[0].source_agents == ["claude-code", "codex"]
    assert "cross-agent agreement" in brief.items[0].reason


def test_compile_turn_marks_native_memory_degraded_with_windows_memory_path(tmp_path):
    library = tmp_path / "agent-library"
    _write_manifest(
        library,
        "claude-code",
        entities=[
            {
                "name": "Bourdon",
                "type": "project",
                "summary": "Recognition survives degraded native memory.",
                "visibility": "team",
            }
        ],
    )

    # Oversized MEMORY.md lives at <base>/C--Users-cumul-repos-bourdon/memory/MEMORY.md
    brief = compile_claude_turn(
        "Bourdon",
        cwd=tmp_path,
        library_path=library,
        projects_base=_degraded(tmp_path),
    )

    assert brief.health.value == "degraded"
    assert brief.items[0].name == "Bourdon"
    assert "native memory is degraded" in brief.delivery["explicit_text"]
    assert "native_memory_primary" in brief.routing["suppressed_surfaces"]


def test_compile_turn_filters_private_items_at_team_access(tmp_path):
    library = tmp_path / "agent-library"
    _write_manifest(
        library,
        "claude-code",
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

    brief = compile_claude_turn(
        "Private Anchor and Public Anchor",
        cwd=tmp_path,
        library_path=library,
        projects_base=_available(tmp_path),
        access_level="team",
    )

    names = {item.name for item in brief.items}
    assert "Private Anchor" not in names
    assert "Public Anchor" in names


def test_compile_turn_redacts_credential_like_text(tmp_path):
    library = tmp_path / "agent-library"
    _write_manifest(
        library,
        "claude-code",
        entities=[
            {
                "name": "Bourdon",
                "type": "project",
                "summary": "api_key should never be echoed.",
                "visibility": "team",
            }
        ],
    )

    brief = compile_claude_turn(
        "Bourdon",
        cwd=tmp_path,
        library_path=library,
        projects_base=_available(tmp_path),
    )

    assert brief.items[0].summary == "[redacted credential-like text]"
    assert "api_key" not in brief.delivery["explicit_text"]


def test_compile_turn_respects_max_items_and_max_chars(tmp_path):
    library = tmp_path / "agent-library"
    _write_manifest(
        library,
        "claude-code",
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

    brief = compile_claude_turn(
        "Bourdon",
        cwd=tmp_path,
        library_path=library,
        projects_base=_available(tmp_path),
        max_items=2,
        max_chars=420,
    )

    assert len(brief.items) == 2
    assert len(brief.delivery["explicit_text"]) <= 420


def test_compile_turn_routes_high_confidence_brief_to_explicit_and_mcp(tmp_path):
    # Repo identity "bourdon" + a cross-agent "Bourdon" entity + a direct prompt
    # match stack to a high-confidence score (prompt + cross_agent + cwd_repo).
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

    brief = compile_claude_turn(
        "Bourdon recognition orchestration",
        cwd=repo,
        library_path=library,
        projects_base=_degraded(tmp_path),
    )

    assert brief.routing["mode"] == "inject"
    assert brief.routing["primary_surface"] == "explicit_pre_turn"
    assert "mcp" in brief.routing["surfaces"]
    assert "native_memory_primary" in brief.routing["suppressed_surfaces"]
    assert brief.routing["confidence"] == "high"


def test_compile_turn_renders_repo_overlay_candidate_when_repo_is_known(tmp_path):
    repo = tmp_path / "bourdon"
    (repo / ".git").mkdir(parents=True)
    library = tmp_path / "agent-library"
    _write_manifest(
        library,
        "claude-code",
        entities=[
            {
                "name": "Bourdon",
                "type": "project",
                "summary": "Recognition orchestration substrate.",
                "visibility": "team",
            }
        ],
    )

    brief = compile_claude_turn(
        "Bourdon recognition orchestration",
        cwd=repo,
        library_path=library,
        projects_base=_available(tmp_path),
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
        "claude-code",
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

    brief = compile_claude_turn(
        "Bourdon",
        cwd=tmp_path,
        library_path=library,
        projects_base=_available(tmp_path),
        max_items=1,
    )

    assert brief.trace["routing_decision"]["primary_surface"] == "explicit_pre_turn"
    assert brief.trace["surface_health"]["candidate_count"] >= 2
    assert brief.trace["surface_health"]["native_memory"] == "available"
    assert brief.trace["selected_items"][0]["name"] == "Bourdon"
    assert "prompt" in brief.trace["selected_items"][0]["dominant_components"]
    assert brief.trace["source_mix"]["ignored"]


def test_compile_turn_emits_claude_schema_version_and_health_key(tmp_path):
    library = tmp_path / "agent-library"
    _write_manifest(
        library,
        "claude-code",
        entities=[
            {
                "name": "Bourdon",
                "type": "project",
                "summary": "Recognition orchestration substrate.",
                "visibility": "team",
            }
        ],
    )

    brief = compile_claude_turn(
        "Bourdon",
        cwd=tmp_path,
        library_path=library,
        projects_base=_available(tmp_path),
    )

    data = brief.to_dict()
    assert data["schema_version"] == "claude-turn-brief/v1"
    # Claude serializes native memory health under "native_memory" (not the
    # Codex "native_stage1" key).
    assert "native_memory" in data["health"]
    assert "native_stage1" not in data["health"]
    assert data["delivery"]["mcp_payload"]["schema_version"] == "claude-turn-brief/v1"
    assert data["delivery"]["mcp_payload"]["native_memory"] == "available"


# -- Windows-shaped path coverage ---------------------------------------------


def test_decode_windows_and_posix_slugs():
    assert _decode_claude_slug(WINDOWS_SLUG) == WINDOWS_CWD
    assert _decode_claude_slug("D--work-app") == r"D:\work\app"
    assert _decode_claude_slug("-Users-foo-bar") == "/Users/foo/bar"


def test_local_session_record_with_windows_slug_and_cwd_matches(tmp_path):
    # A real repo dir whose path is the authoritative cwd in the transcript.
    repo = tmp_path / "bourdon"
    (repo / ".git").mkdir(parents=True)

    projects_base = _projects_base(
        tmp_path,
        slug=WINDOWS_SLUG,
        transcripts=[
            [
                {
                    "type": "user",
                    "cwd": str(repo),
                    "message": {
                        "role": "user",
                        "content": "Bourdon recognition compiler work",
                    },
                }
            ]
        ],
    )
    library = tmp_path / "agent-library"
    _write_manifest(library, "claude-code", entities=[])

    brief = compile_claude_turn(
        "Bourdon recognition",
        cwd=repo,
        library_path=library,
        projects_base=projects_base,
    )

    assert brief.items, "expected the local Claude session transcript to surface"
    top = brief.items[0]
    assert top.kind == "thread"
    assert top.source == "claude_session"
    assert top.source_agents == ["claude-code"]
    assert "Bourdon recognition compiler work" in top.name
    assert "cwd matched prior Claude session" in top.reason


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="GH Actions Windows runner latency is too variable for absolute-time perf assertion",
)
def test_compile_turn_latency_stays_small_for_fixture_library(tmp_path):
    library = tmp_path / "agent-library"
    _write_manifest(
        library,
        "claude-code",
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
    brief = compile_claude_turn(
        "Project 42",
        cwd=tmp_path,
        library_path=library,
        projects_base=_available(tmp_path),
    )
    elapsed_ms = (time.perf_counter() - start) * 1_000

    assert brief.items
    assert elapsed_ms < 200
