"""Tests for core.cascade_turn_compiler."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from core.cascade_turn_compiler import (
    DELIVERY_MODES,
    SCHEMA_VERSION,
    BriefItem,
    RepoIdentity,
    TurnBrief,
    _bounded_prompt,
    _Candidate,
    _contains_subsequence,
    _cwd_score,
    _dedupe_candidates,
    _parse_date,
    _penalty,
    _prompt_match_score,
    _rank_items,
    _recency_score,
    _render_explicit_text,
    _tokens,
    _validate_access_level,
    _validate_delivery,
    compile_cascade_turn,
)


def _make_candidate(**kwargs: Any) -> _Candidate:
    defaults: dict[str, Any] = {
        "kind": "entity",
        "name": "Bourdon",
        "summary": "Memory federation protocol",
        "source": "l6_federation",
        "source_agents": ["codex"],
    }
    defaults.update(kwargs)
    return _Candidate(**defaults)


def _empty_native():
    from participants._windsurf_native import NativeWindsurfState
    return patch(
        "core.cascade_turn_compiler.read_native_windsurf_state",
        return_value=NativeWindsurfState(),
    )


class TestValidation:
    def test_bounded_prompt_truncates(self):
        assert len(_bounded_prompt("x" * 10_000)) == 8_000

    def test_bounded_prompt_strips(self):
        assert _bounded_prompt("  hello  ") == "hello"

    def test_validate_access_level_rejects(self):
        with pytest.raises(ValueError, match="access_level"):
            _validate_access_level("admin")

    def test_validate_delivery_rejects(self):
        with pytest.raises(ValueError, match="delivery"):
            _validate_delivery("smoke-signal")

    def test_validate_delivery_accepts_all(self):
        for mode in DELIVERY_MODES:
            assert _validate_delivery(mode) == mode


class TestTokens:
    def test_basic(self):
        assert _tokens("hello world") == ["hello", "world"]

    def test_mixed_case(self):
        assert _tokens("Bourdon ShipStable") == [
            "bourdon", "shipstable",
        ]

    def test_subsequence(self):
        assert _contains_subsequence(
            ["a", "b", "c"], ["b", "c"],
        )
        assert not _contains_subsequence(
            ["a", "b", "c"], ["a", "c"],
        )


class TestScoring:
    def test_prompt_exact(self):
        c = _make_candidate(name="Bourdon")
        score, reason = _prompt_match_score(c, "Bourdon")
        assert score == 40.0
        assert "prompt matched" in reason

    def test_prompt_substring(self):
        c = _make_candidate(name="Bourdon")
        score, _ = _prompt_match_score(
            c, "Tell me about Bourdon",
        )
        assert score == 36.0

    def test_prompt_no_match(self):
        c = _make_candidate(name="Bourdon")
        score, reason = _prompt_match_score(
            c, "Completely unrelated xyz",
        )
        assert score == 0.0
        assert reason == ""

    def test_cwd_match(self):
        c = _make_candidate(cwd="/Users/test/bourdon")
        repo = RepoIdentity(
            name="bourdon", root="/Users/test/bourdon",
        )
        score, _ = _cwd_score(
            c, Path("/Users/test/bourdon"), repo,
        )
        assert score >= 22.0

    def test_recency_recent(self):
        from datetime import datetime, timezone
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        assert _recency_score(today) == 15.0

    def test_recency_none(self):
        assert _recency_score(None) == 0.0

    def test_penalty_generic(self):
        assert _penalty(_make_candidate(name="memory")) == -10.0

    def test_penalty_native_only(self):
        assert _penalty(_make_candidate(native_only=True)) == -8.0


class TestDedup:
    def test_by_kind_and_name(self):
        c1 = _make_candidate(
            name="Bourdon", source="l6_federation",
        )
        c2 = _make_candidate(
            name="Bourdon", source="cascade_l5",
        )
        assert len(
            _dedupe_candidates([c1, c2], "Bourdon")
        ) == 1


class TestRanking:
    def test_limits(self):
        scored = [
            (
                _make_candidate(name=f"item{i}"),
                float(100 - i), {}, f"reason {i}",
            )
            for i in range(10)
        ]
        items = _rank_items(scored, 3)
        assert len(items) == 3
        assert items[0].rank == 1
        assert items[2].rank == 3


class TestRendering:
    def test_contains_repo(self):
        item = BriefItem(
            rank=1, score=50.0, kind="entity",
            name="Bourdon", summary="Memory fed",
            reason="prompt match", source="l6_federation",
            source_agents=["codex"],
        )
        repo = RepoIdentity(name="bourdon")
        text = _render_explicit_text(
            [item], repo, "available", 2000,
        )
        assert "bourdon" in text.lower()

    def test_no_items(self):
        text = _render_explicit_text(
            [], RepoIdentity(), "unknown", 2000,
        )
        assert "No high-confidence" in text


class TestDateParsing:
    def test_iso_date(self):
        assert _parse_date("2025-06-01") is not None

    def test_iso_datetime(self):
        assert _parse_date("2025-06-01T12:00:00Z") is not None

    def test_empty(self):
        assert _parse_date("") is None

    def test_garbage(self):
        assert _parse_date("not-a-date") is None


class TestCompileCascadeTurn:
    def test_returns_brief(self, tmp_path: Path):
        lib = tmp_path / "agents"
        lib.mkdir()
        with _empty_native():
            brief = compile_cascade_turn(
                "Tell me about Bourdon",
                cwd=str(tmp_path),
                library_path=str(lib),
            )
        assert isinstance(brief, TurnBrief)
        d = brief.to_dict()
        assert d["schema_version"] == SCHEMA_VERSION

    def test_delivery_modes(self, tmp_path: Path):
        lib = tmp_path / "agents"
        lib.mkdir()
        for mode in DELIVERY_MODES:
            with _empty_native():
                brief = compile_cascade_turn(
                    "test",
                    cwd=str(tmp_path),
                    library_path=str(lib),
                    delivery=mode,
                )
            assert "delivery" in brief.to_dict()

    def test_to_yaml_json(self, tmp_path: Path):
        lib = tmp_path / "agents"
        lib.mkdir()
        with _empty_native():
            brief = compile_cascade_turn(
                "test",
                cwd=str(tmp_path),
                library_path=str(lib),
            )
        assert SCHEMA_VERSION in brief.to_yaml()
        parsed = json.loads(brief.to_json())
        assert parsed["schema_version"] == SCHEMA_VERSION

    def test_invalid_access_level(self, tmp_path: Path):
        lib = tmp_path / "agents"
        lib.mkdir()
        with _empty_native(), pytest.raises(ValueError):
            compile_cascade_turn(
                "test",
                cwd=str(tmp_path),
                library_path=str(lib),
                access_level="superadmin",
            )

    def test_cwd_recognition(self, tmp_path: Path):
        lib = tmp_path / "agents"
        lib.mkdir()
        (tmp_path / ".git").mkdir()
        with _empty_native():
            brief = compile_cascade_turn(
                "What should I work on?",
                cwd=str(tmp_path),
                library_path=str(lib),
            )
        assert brief.to_dict()["repo"]["name"] == tmp_path.name

    def test_latency(self, tmp_path: Path):
        lib = tmp_path / "agents"
        lib.mkdir()
        with _empty_native():
            t0 = time.perf_counter()
            compile_cascade_turn(
                "Test", cwd=str(tmp_path),
                library_path=str(lib),
            )
            elapsed_ms = (time.perf_counter() - t0) * 1000
        assert elapsed_ms < 200

    def test_plans_surfaced(self, tmp_path: Path):
        lib = tmp_path / "agents"
        lib.mkdir()
        plans_dir = tmp_path / ".windsurf" / "plans"
        plans_dir.mkdir(parents=True)
        (plans_dir / "feat.md").write_text(
            "# Build Feature\nDetails."
        )
        brief = compile_cascade_turn(
            "Tell me about Build Feature",
            cwd=str(tmp_path),
            library_path=str(lib),
        )
        items = brief.to_dict()["items"]
        plan_items = [i for i in items if i["kind"] == "plan"]
        assert len(plan_items) >= 1

    def test_workflows_surfaced(self, tmp_path: Path):
        lib = tmp_path / "agents"
        lib.mkdir()
        wf_dir = tmp_path / ".windsurf" / "workflows"
        wf_dir.mkdir(parents=True)
        (wf_dir / "deploy.md").write_text(
            "---\ndescription: Deploy the app\n---\nSteps."
        )
        brief = compile_cascade_turn(
            "How do I deploy the app?",
            cwd=str(tmp_path),
            library_path=str(lib),
        )
        items = brief.to_dict()["items"]
        wf_items = [
            i for i in items if i["kind"] == "workflow"
        ]
        assert len(wf_items) >= 1
