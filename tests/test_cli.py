"""Tests for the top-level `bourdon` CLI."""

from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path

import yaml

from cli.main import main


def _build_fake_codex_home(fake_home: Path) -> None:
    codex_home = fake_home / ".codex"
    memories = codex_home / "memories"
    sessions_dir = codex_home / "sessions" / "2026" / "04" / "19"
    memories.mkdir(parents=True)
    (memories / "rollout_summaries").mkdir(parents=True, exist_ok=True)
    sessions_dir.mkdir(parents=True, exist_ok=True)

    (codex_home / "session_index.jsonl").write_text(
        json.dumps(
            {
                "id": "sess1",
                "thread_name": "Ship Coolculator context",
                "updated_at": "2026-04-19T12:00:00Z",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    rollout_path = sessions_dir / "rollout-2026-04-19T12-00-00Z-sess1.jsonl"
    rollout_path.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "timestamp": "2026-04-19T12:00:00Z",
                        "type": "session_meta",
                        "payload": {
                            "id": "sess1",
                            "timestamp": "2026-04-19T12:00:00Z",
                            "cwd": "/workspace/coolculator",
                            "model_provider": "openai",
                            "cli_version": "0.200.0",
                        },
                    }
                ),
                json.dumps(
                    {
                        "timestamp": "2026-04-19T12:01:00Z",
                        "type": "response_item",
                        "payload": {
                            "type": "function_call",
                            "name": "apply_patch",
                            "arguments": (
                                "*** Begin Patch\n"
                                "*** Update File: apps/api/src/app.ts\n"
                                "@@\n"
                                "-old\n"
                                "+new\n"
                                "*** End Patch\n"
                            ),
                        },
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    (memories / "MEMORY.md").write_text(
        """# Task Group: Coolculator monorepo bootstrap
scope: generic

## Task 1: Build the API

### keywords

- Coolculator
- Fastify

## User preferences

- prefer backend-first delivery
""",
        encoding="utf-8",
    )
    (memories / "raw_memories.md").write_text(
        """# Raw Memories

## Thread `sess1`
updated_at: 2026-04-19T12:00:00+00:00
cwd: /workspace/coolculator
rollout_path: /tmp/sess1.jsonl

---
description: Coolculator session.
task: build-api
task_group: coolculator-monorepo
keywords: Coolculator, Fastify, Mac handoff
---

Preference signals:
- keep backend-first delivery
""",
        encoding="utf-8",
    )
    (memories / "rollout_summaries" / "2026-04-19-coolculator.md").write_text(
        """thread_id: sess1
updated_at: 2026-04-19T12:00:00+00:00

# Coolculator rollout

## Task 1: Mac handoff
Outcome: success
""",
        encoding="utf-8",
    )

    codex_brain = fake_home / "codex-brain" / "LOG"
    codex_brain.mkdir(parents=True, exist_ok=True)
    (codex_brain / "2026-04-19.md").write_text(
        "# Coolculator handoff\n\nKeep the handoff crisp.\n",
        encoding="utf-8",
    )


def _build_fake_codex_state_db(fake_home: Path) -> None:
    codex_home = fake_home / ".codex"
    with sqlite3.connect(codex_home / "state_5.sqlite") as conn:
        conn.execute(
            "CREATE TABLE threads ("
            "id TEXT PRIMARY KEY, "
            "memory_mode TEXT NOT NULL, "
            "archived INTEGER NOT NULL)"
        )
        conn.execute(
            "CREATE TABLE stage1_outputs ("
            "thread_id TEXT PRIMARY KEY, "
            "raw_memory TEXT NOT NULL, "
            "rollout_summary TEXT NOT NULL)"
        )
        conn.execute(
            "CREATE TABLE jobs ("
            "kind TEXT NOT NULL, "
            "job_key TEXT NOT NULL, "
            "status TEXT NOT NULL, "
            "retry_remaining INTEGER NOT NULL, "
            "last_error TEXT)"
        )
        conn.execute(
            "INSERT INTO threads (id, memory_mode, archived) VALUES ('sess1', 'enabled', 0)"
        )
        conn.execute(
            "INSERT INTO jobs "
            "(kind, job_key, status, retry_remaining, last_error) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                "memory_stage1",
                "sess1",
                "error",
                2,
                "You've hit your usage limit.",
            ),
        )


def test_cli_codex_export_writes_manifest(tmp_path, monkeypatch):
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    _build_fake_codex_home(fake_home)

    out_path = tmp_path / "codex.l5.yaml"
    exit_code = main(["codex", "export", "--out", str(out_path)])

    assert exit_code == 0
    manifest = yaml.safe_load(out_path.read_text(encoding="utf-8"))
    assert manifest["agent"]["id"] == "codex"
    assert manifest["recent_sessions"][0]["visibility"] == "team"


def test_cli_codex_build_context_writes_l0_and_l1(tmp_path, monkeypatch):
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    _build_fake_codex_home(fake_home)

    out_dir = tmp_path / "context"
    exit_code = main(["codex", "build-context", "--out-dir", str(out_dir)])

    assert exit_code == 0
    l0 = yaml.safe_load((out_dir / "l0" / "hot_cache.yaml").read_text(encoding="utf-8"))
    l1_files = list((out_dir / "l1").glob("*.md"))

    assert l0["current_focus"]["last_session"] == "2026-04-19"
    assert any(entity["keyword"] == "Coolculator" for entity in l0["entities"])
    assert l1_files
    assert any(
        "Coolculator" in l1_file.read_text(encoding="utf-8")
        for l1_file in l1_files
    )


def test_cli_codex_doctor_reports_sqlite_memory_health(tmp_path, monkeypatch, capsys):
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    _build_fake_codex_home(fake_home)
    _build_fake_codex_state_db(fake_home)

    report_path = tmp_path / "doctor.yaml"
    exit_code = main(["codex", "doctor", "--report-out", str(report_path)])
    stdout = capsys.readouterr().out
    report = yaml.safe_load(report_path.read_text(encoding="utf-8"))

    assert exit_code == 0
    assert "codex_state_db" in stdout
    assert report["source_coverage"]["status"] == "ok"
    assert report["codex_state_db"]["stage1_outputs"]["total"] == 0
    assert report["codex_state_db"]["threads"]["memory_enabled"] == 1
    assert report["codex_state_db"]["memory_stage1_jobs"]["by_status"] == {"error": 1}
    assert (
        report["codex_state_db"]["memory_stage1_jobs"]["errors"][0]["last_error"]
        == "You've hit your usage limit."
    )
    assert report["fallback_recall"]["status"] == "available"
    assert report["fallback_recall"]["active"] is False
    assert report["fallback_recall"]["session_records"] == 1
    assert report["fallback_recall"]["rollout_records"] == 1


def test_cli_codex_doctor_marks_fallback_active_when_distilled_memory_empty(
    tmp_path, monkeypatch, capsys
):
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    _build_fake_codex_home(fake_home)
    memories = fake_home / ".codex" / "memories"
    (memories / "MEMORY.md").unlink()
    (memories / "rollout_summaries" / "2026-04-19-coolculator.md").unlink()
    shutil.rmtree(fake_home / "codex-brain")
    (memories / "raw_memories.md").write_text(
        "# Raw Memories\n\nNo raw memories yet.\n",
        encoding="utf-8",
    )

    report_path = tmp_path / "doctor.yaml"
    exit_code = main(["codex", "doctor", "--report-out", str(report_path)])
    capsys.readouterr()
    report = yaml.safe_load(report_path.read_text(encoding="utf-8"))

    assert exit_code == 0
    assert report["fallback_recall"]["status"] == "available"
    assert report["fallback_recall"]["active"] is True
    assert report["fallback_recall"]["reason"] == "codex_distilled_memory_empty"
    assert report["fallback_recall"]["fallback_memory_items"] >= 1
    assert report["fallback_recall"]["project_candidates"] == ["Coolculator"]


def test_cli_codex_sync_native_dry_run_does_not_write_memory_file(
    tmp_path, monkeypatch, capsys
):
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    _build_fake_codex_home(fake_home)

    target = fake_home / ".codex" / "memories" / "bourdon_fallback.md"
    exit_code = main(["codex", "sync-native", "--dry-run"])
    stdout = capsys.readouterr().out
    report = yaml.safe_load(stdout)

    assert exit_code == 0
    assert target.exists() is False
    assert report["mode"] == "dry-run"
    assert report["target"] == str(target)
    assert report["would_write"] is True
    assert report["written"] is False
    assert "Coolculator" in report["preview"]


def test_cli_codex_sync_native_write_creates_bourdon_owned_memory_file(
    tmp_path, monkeypatch, capsys
):
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    _build_fake_codex_home(fake_home)

    target = fake_home / ".codex" / "memories" / "bourdon_fallback.md"
    exit_code = main(["codex", "sync-native", "--write"])
    stdout = capsys.readouterr().out
    report = yaml.safe_load(stdout)

    assert exit_code == 0
    assert target.is_file()
    content = target.read_text(encoding="utf-8")
    assert report["mode"] == "write"
    assert report["written"] is True
    assert report["target"] == str(target)
    assert "# Bourdon Fallback Memory" in content
    assert "Coolculator" in content


def test_cli_codex_sync_native_memory_md_preserves_existing_content_with_markers(
    tmp_path, monkeypatch, capsys
):
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    _build_fake_codex_home(fake_home)

    target = fake_home / ".codex" / "memories" / "MEMORY.md"
    target.write_text("# Existing Codex Memory\n\nKeep this.\n", encoding="utf-8")

    exit_code = main(["codex", "sync-native", "--write", "--memory-md"])
    stdout = capsys.readouterr().out
    report = yaml.safe_load(stdout)

    assert exit_code == 0
    content = target.read_text(encoding="utf-8")
    assert report["target"] == str(target)
    assert report["target_kind"] == "memory_md"
    assert "# Existing Codex Memory" in content
    assert "Keep this." in content
    assert "<!-- BEGIN BOURDON FALLBACK MEMORY -->" in content
    assert "<!-- END BOURDON FALLBACK MEMORY -->" in content
    assert "Coolculator" in content


def test_cli_codex_recognize_returns_immediate_fallback_concept_match(
    tmp_path, monkeypatch, capsys
):
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    _build_fake_codex_home(fake_home)
    memories = fake_home / ".codex" / "memories"
    (memories / "MEMORY.md").unlink()
    (memories / "rollout_summaries" / "2026-04-19-coolculator.md").unlink()
    shutil.rmtree(fake_home / "codex-brain")
    (memories / "raw_memories.md").write_text(
        "# Raw Memories\n\nNo raw memories yet.\n",
        encoding="utf-8",
    )
    rollout_path = next((fake_home / ".codex" / "sessions").rglob("rollout-*.jsonl"))
    with open(rollout_path, "a", encoding="utf-8") as f:
        f.write(
            json.dumps(
                {
                    "timestamp": "2026-04-19T12:02:00Z",
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [
                            {
                                "type": "input_text",
                                "text": (
                                    "Continuo became Bourdon and needs runtime "
                                    "recognition."
                                ),
                            }
                        ],
                    },
                }
            )
            + "\n"
        )

    exit_code = main(
        ["codex", "recognize", "Can we keep working on Bourdon runtime recognition?"]
    )
    stdout = capsys.readouterr().out
    report = yaml.safe_load(stdout)

    assert exit_code == 0
    assert (
        report["recognition"]
        == "You're asking about Bourdon and runtime recognition -- I have both."
    )
    assert report["matched_entities"] == [
        {"name": "Bourdon", "type": "topic"},
        {"name": "runtime recognition", "type": "topic"},
    ]
    assert report["hydration_scheduled"] is True


def test_cli_codex_recognize_prompt_context_includes_matched_entity_summaries(
    tmp_path, monkeypatch, capsys
):
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    _build_fake_codex_home(fake_home)
    memories = fake_home / ".codex" / "memories"
    (memories / "MEMORY.md").unlink()
    (memories / "rollout_summaries" / "2026-04-19-coolculator.md").unlink()
    shutil.rmtree(fake_home / "codex-brain")
    (memories / "raw_memories.md").write_text(
        "# Raw Memories\n\nNo raw memories yet.\n",
        encoding="utf-8",
    )
    rollout_path = next((fake_home / ".codex" / "sessions").rglob("rollout-*.jsonl"))
    with open(rollout_path, "a", encoding="utf-8") as f:
        f.write(
            json.dumps(
                {
                    "timestamp": "2026-04-19T12:02:00Z",
                    "type": "event_msg",
                    "payload": {
                        "type": "user_message",
                        "message": (
                            "Continuo became Bourdon and needs run time "
                            "recognition."
                        ),
                    },
                }
            )
            + "\n"
        )

    exit_code = main(
        [
            "codex",
            "recognize",
            "--prompt-context",
            "Can we keep working on Bourdon runtime recognition?",
        ]
    )
    stdout = capsys.readouterr().out
    report = yaml.safe_load(stdout)

    assert exit_code == 0
    assert "Bourdon recognition context" in report["prompt_context"]
    assert report["recognition"] in report["prompt_context"]
    assert "- Bourdon (topic):" in report["prompt_context"]
    assert "- runtime recognition (topic):" in report["prompt_context"]
    assert "Codex fallback concept recovered" in report["prompt_context"]


def test_cli_codex_eval_fixtures_writes_report(tmp_path, capsys):
    report_path = tmp_path / "report.yaml"

    exit_code = main(
        ["codex", "eval", "--fixtures", "--report-out", str(report_path)]
    )

    assert exit_code == 0
    report = yaml.safe_load(report_path.read_text(encoding="utf-8"))
    stdout = capsys.readouterr().out

    assert report["mode"] == "fixtures"
    assert report["entity_counts"]["total"] >= 1
    assert report["context_generation"]["l0_generated"] is True
    assert "mode: fixtures" in stdout


# ---- claude-code export (SessionEnd hook target) ----------------------------


def _build_fake_claude_code_home(fake_home: Path) -> None:
    """Set up a minimal ~/claude-brain/ tree the Claude Code adapter can parse."""
    brain = fake_home / "claude-brain"
    projects = brain / "PROJECTS" / "ILTT"
    projects.mkdir(parents=True)
    (brain / "CURRENT.md").write_text("# Current focus\n", encoding="utf-8")
    (projects / "OVERVIEW.md").write_text(
        "# ILTT -- if_lift then_that\n\nAI fitness automation.\n",
        encoding="utf-8",
    )
    log_dir = brain / "LOG"
    log_dir.mkdir()
    (log_dir / "2026-04-27-pc.md").write_text(
        "# Session Log -- 2026-04-27 (PC)\n\n## Headline\nShipped role_narrative.\n",
        encoding="utf-8",
    )


def test_cli_claude_code_export_no_sources_silent_and_zero(tmp_path, monkeypatch, capsys):
    """Hook contract: no sources -> exit 0, no stderr output by default."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    monkeypatch.delenv("CLAUDE_BRAIN", raising=False)

    exit_code = main(["claude-code", "export"])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert captured.out == ""
    assert captured.err == ""


def test_cli_claude_code_export_no_sources_verbose_logs_to_stderr(
    tmp_path, monkeypatch, capsys
):
    """--verbose surfaces 'no sources found' to stderr but still exits 0."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    monkeypatch.delenv("CLAUDE_BRAIN", raising=False)

    exit_code = main(["claude-code", "export", "--verbose"])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "no Claude Code memory sources" in captured.err


def test_cli_claude_code_export_writes_to_default_path(tmp_path, monkeypatch):
    """With sources, writes to ~/agent-library/agents/claude-code.l5.yaml."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    monkeypatch.delenv("CLAUDE_BRAIN", raising=False)
    _build_fake_claude_code_home(fake_home)

    exit_code = main(["claude-code", "export"])

    assert exit_code == 0
    expected = fake_home / "agent-library" / "agents" / "claude-code.l5.yaml"
    assert expected.is_file()
    manifest = yaml.safe_load(expected.read_text(encoding="utf-8"))
    assert manifest["agent"]["id"] == "claude-code"


def test_cli_claude_code_export_out_override(tmp_path, monkeypatch):
    """--out path takes precedence over default."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    monkeypatch.delenv("CLAUDE_BRAIN", raising=False)
    _build_fake_claude_code_home(fake_home)

    out_path = tmp_path / "custom" / "claude-code.l5.yaml"
    exit_code = main(["claude-code", "export", "--out", str(out_path)])

    assert exit_code == 0
    assert out_path.is_file()
    # Default path should NOT exist
    default_path = fake_home / "agent-library" / "agents" / "claude-code.l5.yaml"
    assert not default_path.exists()


def test_cli_claude_code_export_silent_on_success_by_default(
    tmp_path, monkeypatch, capsys
):
    """Default behavior is silent (no stdout, no stderr) on successful export."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    monkeypatch.delenv("CLAUDE_BRAIN", raising=False)
    _build_fake_claude_code_home(fake_home)

    exit_code = main(["claude-code", "export"])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert captured.out == ""
    assert captured.err == ""


def test_cli_claude_code_export_print_dumps_manifest_to_stdout(
    tmp_path, monkeypatch, capsys
):
    """--print emits the filtered manifest YAML to stdout."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    monkeypatch.delenv("CLAUDE_BRAIN", raising=False)
    _build_fake_claude_code_home(fake_home)

    exit_code = main(["claude-code", "export", "--print"])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "agent:" in captured.out
    assert "claude-code" in captured.out


def test_cli_claude_code_export_includes_role_narrative(tmp_path, monkeypatch):
    """The exported manifest carries Claude Code's role_narrative."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    monkeypatch.delenv("CLAUDE_BRAIN", raising=False)
    _build_fake_claude_code_home(fake_home)

    out_path = tmp_path / "out.yaml"
    exit_code = main(["claude-code", "export", "--out", str(out_path)])

    assert exit_code == 0
    manifest = yaml.safe_load(out_path.read_text(encoding="utf-8"))
    role_narrative = manifest["agent"].get("role_narrative", "")
    assert "manager" in role_narrative.lower()


# ---- codex eval --recognition (Stream C harness) ----------------------------


def test_cli_codex_eval_recognition_flag_attaches_recognition_section(tmp_path):
    """Passing --recognition adds a 'recognition' key to the report."""
    report_path = tmp_path / "report.yaml"
    exit_code = main(
        [
            "codex",
            "eval",
            "--fixtures",
            "--recognition",
            "--report-out",
            str(report_path),
        ]
    )
    assert exit_code == 0
    report = yaml.safe_load(report_path.read_text(encoding="utf-8"))
    assert "recognition" in report


def test_cli_codex_eval_recognition_report_shape(tmp_path):
    """The recognition section has the expected aggregate + per-prompt keys."""
    report_path = tmp_path / "report.yaml"
    exit_code = main(
        [
            "codex",
            "eval",
            "--fixtures",
            "--recognition",
            "--report-out",
            str(report_path),
        ]
    )
    assert exit_code == 0
    rec = yaml.safe_load(report_path.read_text(encoding="utf-8"))["recognition"]

    # Aggregate keys
    for k in (
        "prompts_tested",
        "recognition_hits",
        "recognition_hit_rate",
        "avg_recognition_latency_us",
        "avg_hydration_latency_ms",
        "results",
    ):
        assert k in rec, f"missing aggregate key: {k}"

    # Per-prompt keys
    assert isinstance(rec["results"], list) and rec["results"]
    sample = rec["results"][0]
    for k in (
        "prompt",
        "recognition",
        "matched_entities",
        "recognition_latency_us",
        "hydration_latency_ms",
        "hydration_chars",
    ):
        assert k in sample, f"missing per-prompt key: {k}"


def test_cli_codex_eval_recognition_fixture_produces_at_least_one_hit(tmp_path):
    """Against the codex fixtures (which include Coolculator), at least one
    canonical prompt should produce a non-empty recognition string."""
    report_path = tmp_path / "report.yaml"
    exit_code = main(
        [
            "codex",
            "eval",
            "--fixtures",
            "--recognition",
            "--report-out",
            str(report_path),
        ]
    )
    assert exit_code == 0
    rec = yaml.safe_load(report_path.read_text(encoding="utf-8"))["recognition"]
    assert rec["recognition_hits"] >= 1
    assert rec["recognition_hit_rate"] > 0.0


def test_cli_codex_eval_recognition_negative_control_no_match(tmp_path):
    """The 'What's the weather like?' canonical prompt must produce no match
    against the fixtures -- guards against over-eager substring matching."""
    report_path = tmp_path / "report.yaml"
    exit_code = main(
        [
            "codex",
            "eval",
            "--fixtures",
            "--recognition",
            "--report-out",
            str(report_path),
        ]
    )
    assert exit_code == 0
    rec = yaml.safe_load(report_path.read_text(encoding="utf-8"))["recognition"]
    weather_results = [
        r for r in rec["results"] if "weather" in r["prompt"].lower()
    ]
    assert weather_results, "negative control prompt missing from results"
    weather = weather_results[0]
    assert weather["recognition"] == ""
    assert weather["matched_entities"] == []


def test_cli_codex_eval_recognition_latency_below_template_budget(tmp_path):
    """Template-based recognition should be sub-millisecond. Sanity-check
    the design claim that recognition is instant: < 1000us avg."""
    report_path = tmp_path / "report.yaml"
    exit_code = main(
        [
            "codex",
            "eval",
            "--fixtures",
            "--recognition",
            "--report-out",
            str(report_path),
        ]
    )
    assert exit_code == 0
    rec = yaml.safe_load(report_path.read_text(encoding="utf-8"))["recognition"]
    assert rec["avg_recognition_latency_us"] < 1000.0, (
        f"recognition avg latency {rec['avg_recognition_latency_us']}us "
        "is above the 1ms (1000us) template-based budget"
    )


def test_cli_codex_eval_without_recognition_flag_omits_recognition_section(
    tmp_path,
):
    """Existing eval behavior must be unchanged when --recognition is absent."""
    report_path = tmp_path / "report.yaml"
    exit_code = main(
        [
            "codex",
            "eval",
            "--fixtures",
            "--report-out",
            str(report_path),
        ]
    )
    assert exit_code == 0
    report = yaml.safe_load(report_path.read_text(encoding="utf-8"))
    assert "recognition" not in report
