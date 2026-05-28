"""Tests for the Codex native-memory metrics snapshot script."""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import subprocess
from datetime import datetime, timezone
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
    rollout_summaries = memories / "rollout_summaries"
    rollout_summaries.mkdir(parents=True)
    (memories / "MEMORY.md").write_text("# Memory\n\nBourdon.\n", encoding="utf-8")
    (memories / "raw_memories.md").write_text("raw memory one\nraw memory two\n", encoding="utf-8")
    (memories / "bourdon_fallback.md").write_text("# Bourdon Fallback\n", encoding="utf-8")
    (rollout_summaries / "2026-05-24-test.md").write_text("summary\n", encoding="utf-8")
    (codex_home / "session_index.jsonl").write_text(
        json.dumps(
            {
                "id": "thread-1",
                "thread_name": "Bourdon",
                "updated_at": "2026-05-24T00:00:00Z",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (codex_home / "sessions").mkdir()

    with sqlite3.connect(codex_home / "state_5.sqlite") as conn:
        conn.execute(
            "CREATE TABLE threads (id TEXT PRIMARY KEY, memory_mode TEXT, archived INTEGER)"
        )
        conn.execute(
            "CREATE TABLE stage1_outputs "
            "(thread_id TEXT PRIMARY KEY, raw_memory TEXT, rollout_summary TEXT)"
        )
        conn.execute(
            "CREATE TABLE jobs "
            "(kind TEXT, job_key TEXT, status TEXT, retry_remaining INTEGER, last_error TEXT)"
        )
        conn.execute("INSERT INTO threads VALUES ('thread-1', 'enabled', 0)")
        conn.execute("INSERT INTO stage1_outputs VALUES ('thread-1', 'raw', 'summary')")
        conn.execute(
            "INSERT INTO jobs VALUES ('memory_stage1', 'thread-1', 'done', 0, NULL)"
        )
        conn.execute(
            "INSERT INTO jobs VALUES (?, ?, ?, ?, ?)",
            (
                "memory_stage1",
                "thread-2",
                "error",
                2,
                "You've hit your usage limit.",
            ),
        )
        conn.execute(
            "INSERT INTO jobs VALUES (?, ?, ?, ?, ?)",
            (
                "memory_stage1",
                "thread-3",
                "error",
                0,
                "Codex ran out of room in the model's context window.",
            ),
        )
    return codex_home


def _build_agent_library(tmp_path: Path) -> Path:
    library = tmp_path / "agent-library"
    agents = library / "agents"
    agents.mkdir(parents=True)
    codex_manifest = {
        "spec_version": "0.1",
        "agent": {"id": "codex", "type": "code-assistant"},
        "last_updated": "2026-05-24T00:00:00+00:00",
        "known_entities": [
            {"name": "Bourdon", "type": "project", "visibility": "team"},
            {"name": "Codex native memory", "type": "concept", "visibility": "team"},
        ],
        "recent_sessions": [
            {"date": "2026-05-24", "visibility": "team", "key_actions": ["Measured memory."]}
        ],
    }
    claude_manifest = {
        "spec_version": "0.1",
        "agent": {"id": "claude-code", "type": "code-assistant"},
        "last_updated": "2026-05-23T00:00:00+00:00",
        "known_entities": [{"name": "Bourdon", "type": "project", "visibility": "team"}],
        "recent_sessions": [],
    }
    (agents / "codex.l5.yaml").write_text(
        yaml.safe_dump(codex_manifest, sort_keys=False), encoding="utf-8"
    )
    (agents / "claude-code.l5.yaml").write_text(
        yaml.safe_dump(claude_manifest, sort_keys=False), encoding="utf-8"
    )
    return library


def test_snapshot_collects_native_fallback_federation_and_graph_metrics(tmp_path):
    module = _load_metrics_module()
    codex_home = _build_codex_home(tmp_path)
    library = _build_agent_library(tmp_path)

    def fake_run(command: list[str]) -> subprocess.CompletedProcess[str]:
        assert command == ["codex", "mcp", "get", "bourdon"]
        return subprocess.CompletedProcess(command, 0, "bourdon\nenabled: true\n", "")

    snapshot = module.build_snapshot(
        codex_home=codex_home,
        library_path=library,
        collected_at=datetime(2026, 5, 24, tzinfo=timezone.utc),
        run_codex_mcp=fake_run,
    )

    assert snapshot["schema_version"] == "codex-memory-metrics/v1"
    assert snapshot["codex_state_db"]["stage1_outputs"]["total"] == 1
    assert snapshot["derived"]["stage1_jobs_total"] == 3
    assert snapshot["derived"]["stage1_jobs_done"] == 1
    assert snapshot["derived"]["stage1_jobs_error"] == 2
    assert snapshot["derived"]["stage1_failure_ratio"] == 2 / 3
    assert snapshot["derived"]["stage1_error_classes"] == {
        "context_window": 1,
        "usage_limit": 1,
    }
    state_db_json = json.dumps(snapshot["codex_state_db"])
    assert "usage limit" not in state_db_json.lower()
    assert "job_key" not in state_db_json
    assert {
        "status": "error",
        "retry_remaining": 2,
        "error_class": "usage_limit",
    } in snapshot["codex_state_db"]["memory_stage1_jobs"]["errors"]
    assert snapshot["memory_files"]["raw_memories_md"]["exists"] is True
    assert snapshot["memory_files"]["rollout_summaries"]["count"] == 1
    assert snapshot["agent_library"]["codex_l5"]["entity_count"] == 2
    assert snapshot["agent_library"]["totals"]["agent_count"] == 2
    assert snapshot["codex_mcp"]["installed"] is True
    assert "codex.native.stage1" in {node["id"] for node in snapshot["graph"]["nodes"]}
    assert {
        "source": "codex.native.stage1",
        "target": "codex.distilled.raw_memories",
        "relation": "produces",
    } in snapshot["graph"]["edges"]


def test_snapshot_compares_previous_metrics(tmp_path):
    module = _load_metrics_module()
    codex_home = _build_codex_home(tmp_path)
    library = _build_agent_library(tmp_path)
    previous = {
        "derived": {
            "stage1_outputs_total": 0,
            "distilled_memory_items": 3,
            "fallback_memory_items": 10,
            "codex_l5_entity_count": 1,
        },
        "memory_files": {"raw_memories_md": {"bytes": 5}},
    }

    snapshot = module.build_snapshot(
        codex_home=codex_home,
        library_path=library,
        collected_at=datetime(2026, 5, 24, tzinfo=timezone.utc),
        run_codex_mcp=lambda command: subprocess.CompletedProcess(command, 1, "", "missing"),
        previous_snapshot=previous,
    )

    assert snapshot["codex_mcp"]["installed"] is False
    assert snapshot["trend"]["stage1_outputs_total_delta"] == 1
    assert snapshot["trend"]["codex_l5_entity_count_delta"] == 1
    assert snapshot["trend"]["raw_memories_bytes_delta"] > 0


def test_cli_writes_json_report(tmp_path, capsys):
    module = _load_metrics_module()
    codex_home = _build_codex_home(tmp_path)
    library = _build_agent_library(tmp_path)
    output_path = tmp_path / "metrics.json"

    exit_code = module.main(
        [
            "--codex-home",
            str(codex_home),
            "--library-path",
            str(library),
            "--out",
            str(output_path),
            "--skip-mcp",
        ]
    )
    stdout = capsys.readouterr().out
    written = json.loads(output_path.read_text(encoding="utf-8"))

    assert exit_code == 0
    assert json.loads(stdout)["schema_version"] == "codex-memory-metrics/v1"
    assert written["agent_library"]["codex_l5"]["session_count"] == 1


def test_cli_reports_dir_loads_latest_and_writes_next_snapshot(tmp_path, capsys):
    module = _load_metrics_module()
    codex_home = _build_codex_home(tmp_path)
    library = _build_agent_library(tmp_path)
    reports_dir = tmp_path / "reports"
    reports_dir.mkdir()
    previous_latest = {
        "schema_version": "codex-memory-metrics/v1",
        "derived": {
            "stage1_outputs_total": 0,
            "stage1_jobs_done": 0,
            "stage1_jobs_error": 1,
            "distilled_memory_items": 3,
            "fallback_memory_items": 10,
            "codex_l5_entity_count": 1,
            "codex_l5_session_count": 0,
        },
        "memory_files": {"raw_memories_md": {"bytes": 5}},
    }
    latest_path = reports_dir / "latest.json"
    latest_path.write_text(json.dumps(previous_latest), encoding="utf-8")

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
    latest = json.loads(latest_path.read_text(encoding="utf-8"))
    timestamped_reports = sorted(reports_dir.glob("codex-memory-metrics-*.json"))

    assert exit_code == 0
    assert json.loads(stdout)["reporting"]["latest_report_path"] == str(latest_path)
    assert latest["trend"]["available"] is True
    assert latest["trend"]["stage1_outputs_total_delta"] == 1
    assert latest["reporting"]["previous_snapshot_path"] == str(latest_path)
    assert len(timestamped_reports) == 1
    assert json.loads(timestamped_reports[0].read_text(encoding="utf-8")) == latest


def test_cli_reports_dir_first_run_has_no_previous_snapshot(tmp_path, capsys):
    module = _load_metrics_module()
    codex_home = _build_codex_home(tmp_path)
    library = _build_agent_library(tmp_path)
    reports_dir = tmp_path / "reports"

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
    capsys.readouterr()
    latest = json.loads((reports_dir / "latest.json").read_text(encoding="utf-8"))
    timestamped_reports = sorted(reports_dir.glob("codex-memory-metrics-*.json"))

    assert exit_code == 0
    assert latest["trend"] == {"available": False}
    assert latest["reporting"]["previous_snapshot_path"] is None
    assert len(timestamped_reports) == 1
