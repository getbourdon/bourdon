"""Tests for ``bourdon demo`` (cli.demo)."""

from __future__ import annotations

import argparse
import io
from pathlib import Path

import pytest
import yaml

from cli.demo import (
    handle_demo,
    run_demo,
    stage_synthetic_library,
)


# ---------------------------------------------------------------------------
# stage_synthetic_library
# ---------------------------------------------------------------------------


def test_stage_synthetic_library_writes_three_manifests(tmp_path):
    library = stage_synthetic_library(tmp_path)
    agents = sorted((library / "agents").glob("*.l5.yaml"))
    assert [p.name for p in agents] == [
        "claude-code.l5.yaml",
        "codex.l5.yaml",
        "cursor.l5.yaml",
    ]


def test_synthetic_claude_code_manifest_has_shared_entity(tmp_path):
    library = stage_synthetic_library(tmp_path)
    cc = yaml.safe_load((library / "agents" / "claude-code.l5.yaml").read_text(encoding="utf-8"))
    names = {e["name"] for e in cc["known_entities"]}
    assert "DemoProject" in names
    # The private-tagged entity that should be filtered at access=public.
    assert "PrivatePersonal" in names


def test_synthetic_codex_manifest_shares_demoproject(tmp_path):
    library = stage_synthetic_library(tmp_path)
    cx = yaml.safe_load((library / "agents" / "codex.l5.yaml").read_text(encoding="utf-8"))
    names = {e["name"] for e in cx["known_entities"]}
    assert "DemoProject" in names  # shared with claude-code -> drives dedup demo


# ---------------------------------------------------------------------------
# run_demo end-to-end
# ---------------------------------------------------------------------------


def test_run_demo_renders_nonempty_memory_md(tmp_path, monkeypatch):
    """Smoke: demo runs end-to-end without crashing and produces a non-trivial file."""
    out = io.StringIO()
    result = run_demo(access_level="public", keep_tempdir=True, stream=out)
    rendered_path = Path(result["rendered_path"])
    assert rendered_path.is_file()
    text = rendered_path.read_text(encoding="utf-8")
    assert "Bourdon Fallback Memory" in text
    assert int(result["bytes"]) > 200
    # Cleanup (we set keep_tempdir=True to inspect the file, so we own the cleanup).
    import shutil
    shutil.rmtree(Path(result["tempdir"]), ignore_errors=True)


def test_run_demo_public_filters_private_and_team(tmp_path):
    """At public access, team + private entities should NOT appear in the rendered text."""
    out = io.StringIO()
    result = run_demo(access_level="public", keep_tempdir=True, stream=out)
    text = Path(result["rendered_path"]).read_text(encoding="utf-8")
    # Private entity gone.
    assert "PrivatePersonal" not in text
    # Team-only entities gone.
    assert "ResearchSpike" not in text
    assert "TeamArchitecture" not in text
    # Public entities present.
    assert "DemoProject" in text
    assert "ClientCRM" in text
    import shutil
    shutil.rmtree(Path(result["tempdir"]), ignore_errors=True)


def test_run_demo_team_includes_team_entities(tmp_path):
    """At team access, both public + team entries appear; private still hidden."""
    out = io.StringIO()
    result = run_demo(access_level="team", keep_tempdir=True, stream=out)
    text = Path(result["rendered_path"]).read_text(encoding="utf-8")
    assert "ResearchSpike" in text or "TeamArchitecture" in text
    assert "PrivatePersonal" not in text
    import shutil
    shutil.rmtree(Path(result["tempdir"]), ignore_errors=True)


def test_run_demo_shows_multi_agent_attribution(tmp_path):
    """DemoProject is in both claude-code and codex; dedup should preserve both sources."""
    out = io.StringIO()
    result = run_demo(access_level="public", keep_tempdir=True, stream=out)
    text = Path(result["rendered_path"]).read_text(encoding="utf-8")
    # The (via claude-code, codex) attribution string must appear somewhere
    # in the DemoProject line.
    demo_lines = [line for line in text.splitlines() if "DemoProject" in line]
    assert demo_lines, "DemoProject must appear in rendered output"
    assert any("(via" in line and "claude-code" in line and "codex" in line for line in demo_lines), (
        f"expected multi-agent attribution on DemoProject line; got: {demo_lines!r}"
    )
    import shutil
    shutil.rmtree(Path(result["tempdir"]), ignore_errors=True)


def test_run_demo_no_keep_removes_tempdir(tmp_path):
    out = io.StringIO()
    result = run_demo(access_level="public", keep_tempdir=False, stream=out)
    assert not Path(result["tempdir"]).exists()


def test_run_demo_keep_leaves_tempdir(tmp_path):
    out = io.StringIO()
    result = run_demo(access_level="public", keep_tempdir=True, stream=out)
    assert Path(result["tempdir"]).is_dir()
    import shutil
    shutil.rmtree(Path(result["tempdir"]), ignore_errors=True)


# ---------------------------------------------------------------------------
# Console output
# ---------------------------------------------------------------------------


def test_run_demo_prints_banner_and_summary(tmp_path):
    out = io.StringIO()
    result = run_demo(access_level="public", keep_tempdir=False, stream=out)
    text = out.getvalue()
    assert "cross-machine recognition demo" in text.lower()
    assert "DemoProject" in text  # via the first-30-lines preview
    assert "source-attribution strings" in text


def test_run_demo_no_keep_does_not_advertise_paths(tmp_path):
    """When the tempdir is deleted, don't tell the user to inspect it."""
    out = io.StringIO()
    run_demo(access_level="public", keep_tempdir=False, stream=out)
    text = out.getvalue()
    assert "Inspect the rendered file" not in text


def test_run_demo_keep_advertises_paths(tmp_path):
    out = io.StringIO()
    result = run_demo(access_level="public", keep_tempdir=True, stream=out)
    text = out.getvalue()
    assert "Inspect the rendered file" in text
    import shutil
    shutil.rmtree(Path(result["tempdir"]), ignore_errors=True)


# ---------------------------------------------------------------------------
# handle_demo (CLI handler)
# ---------------------------------------------------------------------------


def test_handle_demo_returns_zero_on_success(tmp_path, capsys):
    args = argparse.Namespace(access_level="public", no_keep=True)
    rc = handle_demo(args)
    assert rc == 0
