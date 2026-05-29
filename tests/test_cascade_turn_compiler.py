"""Tests for core.cascade_turn_compiler -- Cascade turn-scoped recognition compiler."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from core.cascade_turn_compiler import (
    SCHEMA_VERSION,
    BriefHealth,
    BriefItem,
    BriefRouting,
    RepoIdentity,
    TurnBrief,
    _Candidate,
    _build_evidence,
    _cwd_affinity_score,
    _dedupe_candidates,
    _detect_repo,
    _find_git_root,
    _gather_from_convention_file,
    _gather_from_federation,
    _gather_from_native_state,
    _recency_score,
    _render_convention_file_block,
    _render_explicit_text,
    _resolve_cwd,
    _safe_summary,
    _score_candidate,
    _token_overlap_score,
    _tokenize,
    compile_cascade_turn,
)


# -- Fixtures / helpers --------------------------------------------------------

_POPULATED_MEMORY = """\
---
entities:
  - name: Bourdon
    type: project
    summary: Cross-agent memory federation runtime
    tags: [project, active]
    last_touched: "2026-05-28"
    aliases: [bourdon-protocol]
  - name: ILTT
    type: project
    summary: AI fitness business platform
    tags: [project, active]
    last_touched: "2026-05-20"
  - name: Ryan
    type: person
    summary: Founder and developer
    tags: [personal]
sessions:
  - date: "2026-05-28"
    cwd: /Users/radman/bourdon
    key_actions:
      - Implemented Cascade turn compiler
      - Added native Windsurf state reader
    files_touched:
      - core/cascade_turn_compiler.py
      - adapters/_windsurf_native.py
    project_focus:
      - bourdon
  - date: "2026-05-25"
    cwd: /projects/iltt
    key_actions:
      - Fixed marketplace bug
    project_focus:
      - iltt
---

# Cascade Memory

This is the convention file for Bourdon federation.
"""


def _make_cascade_dir(tmp_path: Path) -> Path:
    cascade_dir = tmp_path / ".cascade-bourdon"
    cascade_dir.mkdir()
    (cascade_dir / "memory.md").write_text(_POPULATED_MEMORY, encoding="utf-8")
    return cascade_dir


def _make_library(tmp_path: Path) -> Path:
    """Create a minimal agent-library with an L5 manifest."""
    import yaml

    lib_dir = tmp_path / "agent-library"
    agents_dir = lib_dir / "agents"
    agents_dir.mkdir(parents=True)

    manifest = {
        "spec_version": "bourdon-l5/v1",
        "agent": {"id": "test-agent", "type": "code-assistant"},
        "known_entities": [
            {
                "name": "Fastify",
                "type": "topic",
                "summary": "Fast web framework for Node.js",
                "source_agents": ["codex"],
            },
            {
                "name": "Coolculator",
                "type": "project",
                "summary": "Calculator project",
                "source_agents": ["codex"],
            },
        ],
        "recent_sessions": [
            {
                "date": "2026-05-27",
                "agent": "codex",
                "key_actions": ["Built REST API with Fastify"],
                "cwd": "/projects/fastify-app",
            },
        ],
    }
    (agents_dir / "test-agent.l5.yaml").write_text(
        yaml.dump(manifest, default_flow_style=False), encoding="utf-8"
    )
    return lib_dir


# -- Unit tests: helpers -------------------------------------------------------


class TestTokenize:
    def test_basic(self):
        tokens = _tokenize("Tell me about Bourdon")
        assert "tell" in tokens
        assert "bourdon" in tokens

    def test_special_chars(self):
        tokens = _tokenize("foo-bar_baz 123")
        assert "foo-bar_baz" in tokens
        assert "123" in tokens

    def test_empty(self):
        assert _tokenize("") == set()


class TestSafeSummary:
    def test_short_text(self):
        assert _safe_summary("Hello") == "Hello"

    def test_truncation(self):
        long_text = "word " * 100
        result = _safe_summary(long_text, limit=50)
        assert len(result) <= 51  # limit + ellipsis char
        assert result.endswith("…")


class TestResolveCwd:
    def test_none_returns_cwd(self):
        result = _resolve_cwd(None)
        assert result is not None
        assert result.is_absolute()

    def test_explicit_path(self, tmp_path):
        result = _resolve_cwd(str(tmp_path))
        assert result == tmp_path.resolve()

    def test_empty_string(self):
        assert _resolve_cwd("") is None


class TestDetectRepo:
    def test_none_cwd(self):
        repo = _detect_repo(None)
        assert repo.name is None

    def test_in_git_repo(self, tmp_path):
        (tmp_path / ".git").mkdir()
        repo = _detect_repo(tmp_path)
        assert repo.name == tmp_path.name
        assert repo.root == str(tmp_path)

    def test_not_in_repo(self, tmp_path):
        subdir = tmp_path / "not-a-repo"
        subdir.mkdir()
        repo = _detect_repo(subdir)
        assert repo.name == "not-a-repo"
        assert repo.root is None


# -- Unit tests: scoring -------------------------------------------------------


class TestTokenOverlapScore:
    def test_no_overlap(self):
        tokens = _tokenize("weather forecast")
        candidate = _Candidate(
            kind="project", name="Bourdon", summary="memory runtime",
            source="convention_file",
        )
        assert _token_overlap_score(tokens, candidate) == 0.0

    def test_name_overlap(self):
        tokens = _tokenize("Tell me about Bourdon")
        candidate = _Candidate(
            kind="project", name="Bourdon", summary="memory runtime",
            source="convention_file",
        )
        score = _token_overlap_score(tokens, candidate)
        assert score > 3.0  # name bonus kicks in

    def test_empty_prompt(self):
        candidate = _Candidate(
            kind="project", name="Test", summary="test", source="convention_file",
        )
        assert _token_overlap_score(set(), candidate) == 0.0


class TestCwdAffinityScore:
    def test_project_focus_match(self):
        repo = RepoIdentity(name="bourdon", root="/Users/radman/bourdon")
        candidate = _Candidate(
            kind="session", name="test", summary="", source="convention_file",
            project_focus=["bourdon"],
        )
        assert _cwd_affinity_score(repo, candidate) == 10.0

    def test_no_match(self):
        repo = RepoIdentity(name="other-project", root="/tmp/other")
        candidate = _Candidate(
            kind="session", name="test", summary="", source="convention_file",
            project_focus=["bourdon"],
        )
        assert _cwd_affinity_score(repo, candidate) == 0.0


class TestRecencyScore:
    def test_no_date(self):
        candidate = _Candidate(
            kind="topic", name="test", summary="", source="convention_file",
        )
        assert _recency_score(candidate) == 5.0

    def test_recent_date(self):
        from datetime import datetime, timezone

        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        candidate = _Candidate(
            kind="topic", name="test", summary="", source="convention_file",
            date_text=today,
        )
        assert _recency_score(candidate) >= 8.0


class TestSourceConfidence:
    def test_score_candidate_integrates(self):
        prompt_tokens = _tokenize("Bourdon memory")
        repo = RepoIdentity(name="bourdon", root="/Users/radman/bourdon")
        candidate = _Candidate(
            kind="project", name="Bourdon", summary="Cross-agent memory federation",
            source="convention_file", project_focus=["bourdon"],
            date_text="2026-05-28",
        )
        score = _score_candidate(candidate, prompt_tokens, repo)
        assert score > _score_candidate(
            _Candidate(
                kind="topic", name="Unrelated", summary="Something else",
                source="l6_federation",
            ),
            prompt_tokens,
            repo,
        )


# -- Unit tests: deduplication -------------------------------------------------


class TestDedupeCandidates:
    def test_keeps_higher_priority(self):
        c1 = _Candidate(kind="project", name="Bourdon", summary="v1", source="l6_federation")
        c2 = _Candidate(kind="project", name="Bourdon", summary="v2", source="convention_file")
        result = _dedupe_candidates([c1, c2])
        assert len(result) == 1
        assert result[0].source == "convention_file"

    def test_no_duplicates(self):
        c1 = _Candidate(kind="project", name="A", summary="", source="convention_file")
        c2 = _Candidate(kind="project", name="B", summary="", source="l6_federation")
        result = _dedupe_candidates([c1, c2])
        assert len(result) == 2


# -- Unit tests: rendering -----------------------------------------------------


class TestRenderExplicitText:
    def test_empty_items(self):
        assert _render_explicit_text([], 1800) == ""

    def test_renders_items(self):
        items = [
            BriefItem(rank=1, score=8.0, kind="project", name="Bourdon",
                      summary="Memory runtime", reason="test", source="convention_file"),
        ]
        text = _render_explicit_text(items, 1800)
        assert "Bourdon" in text
        assert "[Bourdon Cascade Recognition Brief]" in text

    def test_respects_max_chars(self):
        items = [
            BriefItem(rank=i, score=8.0, kind="project", name=f"Item{i}",
                      summary="A" * 100, reason="test", source="convention_file")
            for i in range(20)
        ]
        text = _render_explicit_text(items, 200)
        assert len(text) <= 300  # some margin for header


class TestRenderConventionFileBlock:
    def test_empty(self):
        assert _render_convention_file_block([]) == ""

    def test_renders(self):
        items = [
            BriefItem(rank=1, score=8.0, kind="project", name="Bourdon",
                      summary="Memory runtime", reason="test", source="convention_file"),
        ]
        block = _render_convention_file_block(items)
        assert "**Bourdon**" in block


# -- Unit tests: candidate gathering -------------------------------------------


class TestGatherFromConventionFile:
    def test_populated(self, tmp_path):
        cascade_dir = _make_cascade_dir(tmp_path)
        candidates = _gather_from_convention_file(cascade_dir)
        assert len(candidates) >= 3  # 3 entities + 2 sessions
        names = [c.name for c in candidates]
        assert "Bourdon" in names
        assert "ILTT" in names

    def test_missing_dir(self, tmp_path):
        candidates = _gather_from_convention_file(tmp_path / "nonexistent")
        assert candidates == []


class TestGatherFromNativeState:
    def test_with_plans(self, tmp_path):
        plans_dir = tmp_path / ".windsurf" / "plans"
        plans_dir.mkdir(parents=True)
        (plans_dir / "test-plan.md").write_text("# My Plan\nDo stuff", encoding="utf-8")
        candidates = _gather_from_native_state(tmp_path)
        plan_candidates = [c for c in candidates if c.kind == "plan"]
        assert len(plan_candidates) == 1
        assert plan_candidates[0].name == "My Plan"

    def test_with_workflows(self, tmp_path):
        wf_dir = tmp_path / ".windsurf" / "workflows"
        wf_dir.mkdir(parents=True)
        (wf_dir / "deploy.md").write_text(
            "---\ndescription: Deploy the application\n---\nSteps here",
            encoding="utf-8",
        )
        candidates = _gather_from_native_state(tmp_path)
        wf_candidates = [c for c in candidates if c.kind == "workflow"]
        assert len(wf_candidates) == 1
        assert wf_candidates[0].summary == "Deploy the application"


class TestGatherFromFederation:
    def test_with_library(self, tmp_path):
        lib_dir = _make_library(tmp_path)
        candidates = _gather_from_federation(lib_dir, "team")
        assert len(candidates) >= 2
        names = [c.name for c in candidates]
        assert "Fastify" in names


# -- Integration tests: compile_cascade_turn -----------------------------------


class TestCompileCascadeTurn:
    def test_basic_compilation(self, tmp_path):
        cascade_dir = _make_cascade_dir(tmp_path)
        lib_dir = _make_library(tmp_path)

        brief = compile_cascade_turn(
            "Tell me about Bourdon",
            cwd=str(tmp_path),
            cascade_dir=cascade_dir,
            library_path=lib_dir,
            max_items=6,
            max_chars=1800,
            delivery="all",
        )

        assert brief.schema_version == SCHEMA_VERSION
        assert brief.prompt == "Tell me about Bourdon"
        assert isinstance(brief.items, list)
        assert brief.health.convention_file == "available"
        assert brief.routing.confidence in ("high", "medium", "low")

    def test_bourdon_prompt_matches(self, tmp_path):
        cascade_dir = _make_cascade_dir(tmp_path)
        lib_dir = _make_library(tmp_path)

        brief = compile_cascade_turn(
            "Tell me about Bourdon",
            cwd=str(tmp_path),
            cascade_dir=cascade_dir,
            library_path=lib_dir,
        )

        # Should find "Bourdon" entity
        item_names = [item.name for item in brief.items]
        assert "Bourdon" in item_names

    def test_no_match_prompt(self, tmp_path):
        cascade_dir = _make_cascade_dir(tmp_path)
        lib_dir = _make_library(tmp_path)

        brief = compile_cascade_turn(
            "What's the weather like?",
            cwd=str(tmp_path),
            cascade_dir=cascade_dir,
            library_path=lib_dir,
        )

        # Negative control — should not match well
        if brief.items:
            assert brief.items[0].score < 5.0

    def test_delivery_explicit_only(self, tmp_path):
        cascade_dir = _make_cascade_dir(tmp_path)

        brief = compile_cascade_turn(
            "Bourdon",
            cwd=str(tmp_path),
            cascade_dir=cascade_dir,
            library_path=tmp_path / "empty-lib",
            delivery="explicit",
        )

        assert brief.delivery.explicit_text != ""
        assert brief.delivery.mcp_payload == {}
        assert brief.delivery.convention_file_block == ""

    def test_delivery_mcp_only(self, tmp_path):
        cascade_dir = _make_cascade_dir(tmp_path)

        brief = compile_cascade_turn(
            "Bourdon",
            cwd=str(tmp_path),
            cascade_dir=cascade_dir,
            library_path=tmp_path / "empty-lib",
            delivery="mcp",
        )

        assert brief.delivery.explicit_text == ""
        assert brief.delivery.mcp_payload != {}

    def test_max_items_respected(self, tmp_path):
        cascade_dir = _make_cascade_dir(tmp_path)
        lib_dir = _make_library(tmp_path)

        brief = compile_cascade_turn(
            "Bourdon ILTT Fastify Coolculator",
            cwd=str(tmp_path),
            cascade_dir=cascade_dir,
            library_path=lib_dir,
            max_items=2,
        )

        assert len(brief.items) <= 2

    def test_invalid_delivery_raises(self, tmp_path):
        cascade_dir = _make_cascade_dir(tmp_path)
        with pytest.raises(ValueError, match="delivery"):
            compile_cascade_turn(
                "test",
                cwd=str(tmp_path),
                cascade_dir=cascade_dir,
                delivery="invalid",
            )

    def test_invalid_max_items_raises(self, tmp_path):
        cascade_dir = _make_cascade_dir(tmp_path)
        with pytest.raises(ValueError, match="max_items"):
            compile_cascade_turn(
                "test",
                cwd=str(tmp_path),
                cascade_dir=cascade_dir,
                max_items=0,
            )

    def test_schema_version_in_output(self, tmp_path):
        cascade_dir = _make_cascade_dir(tmp_path)
        brief = compile_cascade_turn(
            "test",
            cwd=str(tmp_path),
            cascade_dir=cascade_dir,
            library_path=tmp_path / "empty-lib",
        )
        data = brief.to_dict()
        assert data["schema_version"] == SCHEMA_VERSION

    def test_to_dict_structure(self, tmp_path):
        cascade_dir = _make_cascade_dir(tmp_path)
        brief = compile_cascade_turn(
            "Bourdon",
            cwd=str(tmp_path),
            cascade_dir=cascade_dir,
            library_path=tmp_path / "empty-lib",
        )
        data = brief.to_dict()
        assert "schema_version" in data
        assert "prompt" in data
        assert "cwd" in data
        assert "repo" in data
        assert "health" in data
        assert "routing" in data
        assert "items" in data
        assert "delivery" in data
        assert "trace" in data
        assert "diagnostics" in data

    def test_deterministic(self, tmp_path):
        cascade_dir = _make_cascade_dir(tmp_path)
        lib_dir = _make_library(tmp_path)

        brief1 = compile_cascade_turn(
            "Tell me about Bourdon",
            cwd=str(tmp_path),
            cascade_dir=cascade_dir,
            library_path=lib_dir,
        )
        brief2 = compile_cascade_turn(
            "Tell me about Bourdon",
            cwd=str(tmp_path),
            cascade_dir=cascade_dir,
            library_path=lib_dir,
        )

        # Same input → same output
        assert brief1.to_dict()["items"] == brief2.to_dict()["items"]
        assert brief1.to_dict()["routing"] == brief2.to_dict()["routing"]

    def test_workspace_plans_enrichment(self, tmp_path):
        cascade_dir = _make_cascade_dir(tmp_path)
        plans_dir = tmp_path / ".windsurf" / "plans"
        plans_dir.mkdir(parents=True)
        (plans_dir / "bourdon-update.md").write_text(
            "# Bourdon Update Plan\nUpdate to v0.8",
            encoding="utf-8",
        )

        brief = compile_cascade_turn(
            "Bourdon Update Plan",
            cwd=str(tmp_path),
            cascade_dir=cascade_dir,
            library_path=tmp_path / "empty-lib",
        )

        # Should find the plan as a candidate
        sources = [item.source for item in brief.items]
        assert "workspace_context" in sources or "convention_file" in sources
