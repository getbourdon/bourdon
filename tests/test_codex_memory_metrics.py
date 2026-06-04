"""Tests for the Codex native-memory metrics collector script."""

from __future__ import annotations

import importlib.util
import json
import sqlite3
from pathlib import Path

import yaml


def _load_metrics_module():
    script_path = Path(__file__).resolve().parents[1] / "scripts" / "codex_memory_metrics.py"
    spec = importlib.util.spec_from_file_location("codex_memory_metrics", script_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _build_codex_home(tmp_path: Path) -> Path:
    codex_home = tmp_path / ".codex"
    memories = codex_home / "memories"
    rollouts = memories / "rollout_summaries"
    rollouts.mkdir(parents=True)
    (memories / "MEMORY.md").write_text("# Memory\n\nBourdon.\n", encoding="utf-8")
    (memories / "raw_memories.md").write_text("raw one\nraw two\n", encoding="utf-8")
    (memories / "bourdon_fallback.md").write_text("# Fallback\n", encoding="utf-8")
    (rollouts / "2026-06-04-test.md").write_text("summary\n", encoding="utf-8")
    (codex_home / "sessions").mkdir()

    with sqlite3.connect(codex_home / "state_5.sqlite") as conn:
        conn.execute(
            "CREATE TABLE threads ("
            "id TEXT PRIMARY KEY, "
            "memory_mode TEXT NOT NULL, "
            "archived INTEGER NOT NULL)"
        )
        conn.execute(
            "CREATE TABLE agent_jobs ("
            "id TEXT PRIMARY KEY, "
            "name TEXT NOT NULL, "
            "status TEXT NOT NULL, "
            "instruction TEXT NOT NULL, "
            "input_headers_json TEXT NOT NULL, "
            "input_csv_path TEXT NOT NULL, "
            "output_csv_path TEXT NOT NULL, "
            "created_at INTEGER NOT NULL, "
            "updated_at INTEGER NOT NULL, "
            "last_error TEXT)"
        )
        conn.execute(
            "CREATE TABLE agent_job_items ("
            "job_id TEXT NOT NULL, "
            "item_id TEXT NOT NULL, "
            "row_index INTEGER NOT NULL, "
            "row_json TEXT NOT NULL, "
            "status TEXT NOT NULL, "
            "attempt_count INTEGER NOT NULL, "
            "created_at INTEGER NOT NULL, "
            "updated_at INTEGER NOT NULL, "
            "last_error TEXT, "
            "PRIMARY KEY (job_id, item_id))"
        )
        conn.execute(
            "INSERT INTO threads (id, memory_mode, archived) VALUES ('thread-1', 'enabled', 0)"
        )
    return codex_home


def _build_agent_library(tmp_path: Path) -> Path:
    library = tmp_path / "agent-library"
    agents = library / "agents"
    agents.mkdir(parents=True)
    manifest = {
        "spec_version": "0.1",
        "agent": {"id": "codex", "type": "code-assistant"},
        "last_updated": "2026-06-04T00:00:00+00:00",
        "known_entities": [{"name": "Bourdon", "type": "project", "visibility": "team"}],
        "recent_sessions": [{"date": "2026-06-04", "visibility": "team"}],
    }
    (agents / "codex.l5.yaml").write_text(
        yaml.safe_dump(manifest, sort_keys=False),
        encoding="utf-8",
    )
    return library


def test_metrics_script_writes_reports_dir_and_detects_current_schema(tmp_path, capsys):
    module = _load_metrics_module()
    codex_home = _build_codex_home(tmp_path)
    library = _build_agent_library(tmp_path)
    reports_dir = tmp_path / "reports"
    reports_dir.mkdir()
    (reports_dir / "latest.json").write_text(
        json.dumps(
            {
                "schema_version": "codex-memory-metrics/v1",
                "derived": {"raw_memories_bytes": 1, "codex_l5_entity_count": 0},
                "memory_files": {"raw_memories_md": {"bytes": 1}},
            }
        ),
        encoding="utf-8",
    )

    exit_code = module.main(
        [
            "--codex-home",
            str(codex_home),
            "--library-path",
            str(library),
            "--reports-dir",
            str(reports_dir),
            "--skip-mcp",
        ]
    )
    stdout = capsys.readouterr().out
    latest = json.loads((reports_dir / "latest.json").read_text(encoding="utf-8"))
    timestamped_reports = sorted(reports_dir.glob("codex-memory-metrics-*.json"))

    assert exit_code == 0
    assert json.loads(stdout)["reporting"]["latest_report_path"] == str(
        reports_dir / "latest.json"
    )
    assert latest["codex_state_db"]["schema"]["variant"] == "agent_jobs"
    assert latest["codex_state_db"]["schema"]["stage1_counters_available"] is False
    assert latest["trend"]["available"] is True
    assert latest["trend"]["raw_memories_bytes_delta"] > 0
    assert latest["agent_library"]["codex_l5"]["entity_count"] == 1
    assert len(timestamped_reports) == 1


def test_metrics_script_writes_html_dashboard_with_precise_readiness_language(
    tmp_path, capsys
):
    module = _load_metrics_module()
    codex_home = _build_codex_home(tmp_path)
    library = _build_agent_library(tmp_path)
    reports_dir = tmp_path / "reports"
    html_dir = tmp_path / "html"

    exit_code = module.main(
        [
            "--codex-home",
            str(codex_home),
            "--library-path",
            str(library),
            "--reports-dir",
            str(reports_dir),
            "--html-report-dir",
            str(html_dir),
            "--skip-mcp",
        ]
    )
    capsys.readouterr()
    html_reports = sorted(html_dir.glob("*.html"))
    latest_html = html_dir / "latest.html"
    rendered = latest_html.read_text(encoding="utf-8")

    assert exit_code == 0
    assert latest_html.exists()
    assert len(html_reports) == 2
    assert "Schema detection fixed" in rendered
    assert "Legacy Stage 1 metric continuity unavailable" in rendered
    assert "Native memory accumulation active" in rendered
    assert "Native-primary adoption blocked" in rendered
    assert "schema issue" not in rendered.lower()
    assert "data-filter-kind" in rendered
    assert "Exact Evidence" in rendered


def test_l5_staleness_threshold_is_parameterized(tmp_path, capsys):
    module = _load_metrics_module()
    codex_home = _build_codex_home(tmp_path)

    stale_library = tmp_path / "stale-library"
    (stale_library / "agents").mkdir(parents=True)
    stale_manifest = {
        "spec_version": "0.1",
        "agent": {"id": "codex", "type": "code-assistant"},
        "last_updated": "2020-01-01T00:00:00+00:00",
        "known_entities": [{"name": "Bourdon", "type": "project", "visibility": "team"}],
        "recent_sessions": [{"date": "2020-01-01", "visibility": "team"}],
    }
    (stale_library / "agents" / "codex.l5.yaml").write_text(
        yaml.safe_dump(stale_manifest, sort_keys=False),
        encoding="utf-8",
    )

    html_dir = tmp_path / "html-stale"
    exit_code = module.main(
        [
            "--codex-home",
            str(codex_home),
            "--library-path",
            str(stale_library),
            "--html-report-dir",
            str(html_dir),
            "--l5-staleness-days",
            "1",
            "--skip-mcp",
        ]
    )
    capsys.readouterr()
    rendered_stale = (html_dir / "latest.html").read_text(encoding="utf-8")

    assert exit_code == 0
    assert "Codex L5 publication stale" in rendered_stale

    fresh_library = tmp_path / "fresh-library"
    fresh_library = _build_agent_library(tmp_path)
    html_dir_fresh = tmp_path / "html-fresh"
    exit_code_fresh = module.main(
        [
            "--codex-home",
            str(codex_home),
            "--library-path",
            str(fresh_library),
            "--html-report-dir",
            str(html_dir_fresh),
            "--l5-staleness-days",
            "36500",
            "--skip-mcp",
        ]
    )
    capsys.readouterr()
    rendered_fresh = (html_dir_fresh / "latest.html").read_text(encoding="utf-8")

    assert exit_code_fresh == 0
    assert "Codex L5 publication stale" not in rendered_fresh
