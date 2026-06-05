"""Tests for the top-level `bourdon` CLI."""

from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path

import pytest
import yaml

import cli.main as cli_main
from cli.main import main


@pytest.fixture(autouse=True)
def _clear_codex_home_env(monkeypatch):
    monkeypatch.delenv("CODEX_HOME", raising=False)


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


def _build_fake_codex_live_state_db(fake_home: Path) -> None:
    codex_home = fake_home / ".codex"
    with sqlite3.connect(codex_home / "state_5.sqlite") as conn:
        conn.execute(
            "CREATE TABLE threads ("
            "id TEXT PRIMARY KEY, "
            "title TEXT, "
            "first_user_message TEXT, "
            "cwd TEXT, "
            "memory_mode TEXT, "
            "archived INTEGER, "
            "updated_at TEXT)"
        )
        conn.execute(
            "INSERT INTO threads "
            "(id, title, first_user_message, cwd, memory_mode, archived, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "live-bourdon",
                "Bourdon recognition first runtime layer",
                "Continuo became Bourdon and needs runtime recognition.",
                "/workspace/bourdon",
                "enabled",
                0,
                "2026-05-13T12:00:00Z",
            ),
        )


def _write_l5_manifest(library: Path, agent_id: str, entities: list[dict]) -> Path:
    path = library / "agents" / f"{agent_id}.l5.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    manifest = {
        "spec_version": "0.1",
        "agent": {"id": agent_id, "type": "code-assistant"},
        "last_updated": "2026-05-08T12:00:00+00:00",
        "known_entities": entities,
        "recent_sessions": [],
    }
    path.write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")
    return path


def _build_fake_cursor_dir(tmp_path: Path) -> Path:
    cursor_dir = tmp_path / "Cursor"
    db_path = cursor_dir / "User" / "workspaceStorage" / "abc123" / "state.vscdb"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE ItemTable (key TEXT PRIMARY KEY, value TEXT)")
        conn.execute(
            "INSERT INTO ItemTable (key, value) VALUES (?, ?)",
            (
                "composer.composerData",
                json.dumps(
                    {
                        "workspacePath": "/Users/dev/projects/bourdon",
                        "title": "Wire Bourdon recognition",
                        "messages": [],
                        "lastUpdatedAt": "2026-05-08T12:00:00Z",
                    }
                ),
            ),
        )
    return cursor_dir


def _build_fake_automations_dir(tmp_path: Path) -> Path:
    automations_dir = tmp_path / "automations"
    automation = automations_dir / "radlab-mission-control-brief"
    automation.mkdir(parents=True)
    (automation / "automation.toml").write_text(
        """\
version = 1
id = "radlab-mission-control-brief"
kind = "cron"
name = "Mission Control Brief"
status = "ACTIVE"
rrule = "FREQ=WEEKLY;BYDAY=MO"
cwds = ["/Users/radman"]
""",
        encoding="utf-8",
    )
    (automation / "memory.md").write_text(
        """\
2026-06-03
- ShipStable launch gates are now human dashboard actions.
- Bourdon needs a codex-automations L5 publisher.
""",
        encoding="utf-8",
    )
    return automations_dir


def test_cli_prepare_turn_returns_l6_recognition_from_merged_agents(tmp_path, capsys):
    library = tmp_path / "agent-library"
    _write_l5_manifest(
        library,
        "claude-code",
        [
            {
                "name": "Bourdon",
                "type": "topic",
                "summary": "Claude planning context.",
                "visibility": "team",
            }
        ],
    )
    _write_l5_manifest(
        library,
        "codex",
        [
            {
                "name": "Bourdon",
                "type": "topic",
                "summary": "Codex implementation context.",
                "visibility": "team",
            }
        ],
    )

    exit_code = main(
        [
            "prepare-turn",
            "Can we keep working on Bourdon?",
            "--library",
            str(library),
        ]
    )
    report = yaml.safe_load(capsys.readouterr().out)

    assert exit_code == 0
    assert report["recognition"] == "Oh -- Bourdon, the topic."
    assert report["matched_entities"] == [
        {
            "name": "Bourdon",
            "type": "topic",
            "source_agents": ["claude-code", "codex"],
        }
    ]
    assert "Bourdon recognition context" in report["prompt_context"]
    assert "via claude-code, codex" in report["prompt_context"]


def test_cli_prepare_turn_returns_empty_context_on_no_match(tmp_path, capsys):
    library = tmp_path / "agent-library"
    _write_l5_manifest(
        library,
        "codex",
        [{"name": "Bourdon", "type": "topic", "visibility": "team"}],
    )

    exit_code = main(
        [
            "prepare-turn",
            "What is the weather?",
            "--library",
            str(library),
        ]
    )
    report = yaml.safe_load(capsys.readouterr().out)

    assert exit_code == 0
    assert report["recognition"] == ""
    assert report["matched_entities"] == []
    assert report["prompt_context"] == ""


def test_cli_cursor_export_writes_schema_valid_manifest(tmp_path, capsys):
    import jsonschema

    cursor_dir = _build_fake_cursor_dir(tmp_path)
    out_path = tmp_path / "agent-library" / "agents" / "cursor.l5.yaml"

    exit_code = main(
        [
            "cursor",
            "export",
            "--cursor-dir",
            str(cursor_dir),
            "--out",
            str(out_path),
        ]
    )
    capsys.readouterr()
    manifest = yaml.safe_load(out_path.read_text(encoding="utf-8"))
    schema = json.loads(
        (Path(__file__).parent.parent / "spec" / "L5_schema.json").read_text(
            encoding="utf-8"
        )
    )

    assert exit_code == 0
    assert manifest["agent"]["id"] == "cursor"
    assert any(entity["name"] == "bourdon" for entity in manifest["known_entities"])
    jsonschema.validate(instance=manifest, schema=schema)


def test_cli_cursor_export_print_still_writes_file(tmp_path, capsys):
    cursor_dir = _build_fake_cursor_dir(tmp_path)
    out_path = tmp_path / "agent-library" / "agents" / "cursor.l5.yaml"

    exit_code = main(
        [
            "cursor",
            "export",
            "--cursor-dir",
            str(cursor_dir),
            "--out",
            str(out_path),
            "--print",
        ]
    )
    printed = yaml.safe_load(capsys.readouterr().out)

    assert exit_code == 0
    assert out_path.is_file()
    assert printed["agent"]["id"] == "cursor"


def test_cli_codex_automations_export_writes_schema_valid_manifest(tmp_path, capsys):
    import jsonschema

    automations_dir = _build_fake_automations_dir(tmp_path)
    out_path = tmp_path / "agent-library" / "agents" / "codex-automations.l5.yaml"

    exit_code = main(
        [
            "codex-automations",
            "export",
            "--automations-dir",
            str(automations_dir),
            "--out",
            str(out_path),
        ]
    )
    capsys.readouterr()
    manifest = yaml.safe_load(out_path.read_text(encoding="utf-8"))
    schema = json.loads(
        (Path(__file__).parent.parent / "spec" / "L5_schema.json").read_text(
            encoding="utf-8"
        )
    )

    assert exit_code == 0
    assert manifest["agent"]["id"] == "codex-automations"
    assert manifest["recent_sessions"][0]["date"] == "2026-06-03"
    assert any(
        entity["name"] == "radlab-mission-control-brief"
        for entity in manifest["known_entities"]
    )
    jsonschema.validate(instance=manifest, schema=schema)


def test_cli_codex_automations_export_print_still_writes_file(tmp_path, capsys):
    automations_dir = _build_fake_automations_dir(tmp_path)
    out_path = tmp_path / "agent-library" / "agents" / "codex-automations.l5.yaml"

    exit_code = main(
        [
            "codex-automations",
            "export",
            "--automations-dir",
            str(automations_dir),
            "--out",
            str(out_path),
            "--print",
        ]
    )
    printed = yaml.safe_load(capsys.readouterr().out)

    assert exit_code == 0
    assert out_path.is_file()
    assert printed["agent"]["id"] == "codex-automations"


def test_cli_codex_automations_doctor_reports_counts(tmp_path, capsys):
    automations_dir = _build_fake_automations_dir(tmp_path)

    exit_code = main(
        [
            "codex-automations",
            "doctor",
            "--automations-dir",
            str(automations_dir),
        ]
    )
    report = yaml.safe_load(capsys.readouterr().out)

    assert exit_code == 0
    assert report["health"]["status"] == "ok"
    assert report["health"]["details"]["automation_count"] == 1
    assert report["health"]["details"]["runs_extracted"] == 1


def test_cli_deeper_context_returns_empty_when_l2_disabled(monkeypatch, capsys):
    async def disabled_query_l2(prompt):
        return ""

    monkeypatch.setattr(cli_main, "query_l2", disabled_query_l2)

    exit_code = main(["deeper-context", "Tell me about Bourdon"])
    report = yaml.safe_load(capsys.readouterr().out)

    assert exit_code == 0
    assert report["context"] == ""
    assert report["context_chars"] == 0


def test_cli_deeper_context_returns_l2_text_with_fake_query(monkeypatch, capsys):
    async def fake_query_l2(prompt):
        return f"Hydrated detail for {prompt}."

    monkeypatch.setattr(cli_main, "query_l2", fake_query_l2)

    exit_code = main(["deeper-context", "Bourdon"])
    report = yaml.safe_load(capsys.readouterr().out)

    assert exit_code == 0
    assert report["context"] == "Hydrated detail for Bourdon."
    assert report["context_chars"] == len("Hydrated detail for Bourdon.")


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


def test_cli_codex_recognize_uses_live_sqlite_threads(tmp_path, monkeypatch, capsys):
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    _build_fake_codex_home(fake_home)
    _build_fake_codex_live_state_db(fake_home)

    exit_code = main(
        [
            "codex",
            "recognize",
            "Bourdon recognition first runtime layer",
            "--prompt-context",
        ]
    )
    report = yaml.safe_load(capsys.readouterr().out)

    assert exit_code == 0
    assert report["recognition"]
    matched_names = {entity["name"] for entity in report["matched_entities"]}
    assert {
        "Bourdon recognition first runtime layer",
        "Continuo",
        "runtime recognition",
    }.issubset(matched_names)
    assert "Bourdon recognition context" in report["prompt_context"]


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
    assert report["l5_quality"]["status"] == "ok"
    assert report["l5_quality"]["oversized_key_actions"] == 0
    assert report["l5_quality"]["duplicated_name_summary_entities"] == 0


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
    assert "Bourdon" in report["recognition"]
    assert "runtime recognition" in report["recognition"]
    matched_names = {entity["name"] for entity in report["matched_entities"]}
    assert {"Bourdon", "Continuo", "runtime recognition"}.issubset(matched_names)
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


def test_cli_codex_prepare_turn_writes_bridge_l5_and_prompt_context(
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
    l5_path = tmp_path / "agent-library" / "agents" / "codex.l5.yaml"

    exit_code = main(
        [
            "codex",
            "prepare-turn",
            "--write",
            "--memory-md",
            "--l5-out",
            str(l5_path),
            "Can we keep working on Bourdon runtime recognition?",
        ]
    )
    stdout = capsys.readouterr().out
    report = yaml.safe_load(stdout)

    memory_md = memories / "MEMORY.md"
    assert exit_code == 0
    assert report["mode"] == "write"
    assert report["recognition"]["recognition"]
    assert "Bourdon recognition context" in report["prompt_context"]
    assert report["writes"]["native_memory"]["written"] is True
    assert report["writes"]["native_memory"]["target"] == str(memory_md)
    assert report["writes"]["l5"]["written"] is True
    assert report["writes"]["l5"]["target"] == str(l5_path)
    assert "<!-- BEGIN BOURDON FALLBACK MEMORY -->" in memory_md.read_text(
        encoding="utf-8"
    )
    l5_manifest = yaml.safe_load(l5_path.read_text(encoding="utf-8"))
    l5_entities = {entity["name"] for entity in l5_manifest["known_entities"]}
    assert "Bourdon" in l5_entities
    assert "runtime recognition" in l5_entities


def test_cli_codex_prepare_turn_dry_run_does_not_write(tmp_path, monkeypatch, capsys):
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    _build_fake_codex_home(fake_home)
    l5_path = tmp_path / "agent-library" / "agents" / "codex.l5.yaml"
    native_path = fake_home / ".codex" / "memories" / "bourdon_fallback.md"

    exit_code = main(
        [
            "codex",
            "prepare-turn",
            "--native-out",
            str(native_path),
            "--l5-out",
            str(l5_path),
            "Tell me about Coolculator",
        ]
    )
    stdout = capsys.readouterr().out
    report = yaml.safe_load(stdout)

    assert exit_code == 0
    assert report["mode"] == "dry-run"
    assert report["writes"]["native_memory"]["written"] is False
    assert report["writes"]["l5"]["written"] is False
    assert native_path.exists() is False
    assert l5_path.exists() is False


def test_cli_codex_prepare_turn_turn_compiled_strategy_returns_router_trace(
    tmp_path, monkeypatch, capsys
):
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    _build_fake_codex_home(fake_home)
    library = tmp_path / "agent-library"
    _write_l5_manifest(
        library,
        "claude-code",
        [
            {
                "name": "Bourdon",
                "type": "project",
                "summary": "Active recognition orchestration across agent surfaces.",
                "visibility": "team",
            }
        ],
    )
    native_path = tmp_path / "native.md"
    l5_path = tmp_path / "codex.l5.yaml"

    exit_code = main(
        [
            "codex",
            "prepare-turn",
            "--strategy",
            "turn-compiled",
            "--library-path",
            str(library),
            "--codex-home",
            str(fake_home / ".codex"),
            "--native-out",
            str(native_path),
            "--l5-out",
            str(l5_path),
            "Can we keep working on Bourdon recognition orchestration?",
        ]
    )
    report = yaml.safe_load(capsys.readouterr().out)

    assert exit_code == 0
    assert report["mode"] == "dry-run"
    assert report["strategy"] == "turn-compiled"
    assert report["writes"]["native_memory"]["written"] is False
    assert report["writes"]["l5"]["written"] is False
    assert native_path.exists() is False
    assert l5_path.exists() is False
    assert "compiled_turn" in report
    assert report["prompt_context"] == report["compiled_turn"]["delivery"][
        "explicit_text"
    ]
    assert report["compiled_turn"]["schema_version"] == "codex-turn-brief/v1"
    assert report["compiled_turn"]["routing"]["mode"] == "inject"
    assert report["compiled_turn"]["routing"]["primary_surface"] == (
        "explicit_pre_turn"
    )
    assert report["compiled_turn"]["trace"]["routing_decision"][
        "primary_surface"
    ] == "explicit_pre_turn"


def test_cli_compile_turn_outputs_yaml_schema(tmp_path, capsys):
    library = tmp_path / "agent-library"
    _write_l5_manifest(
        library,
        "codex",
        [
            {
                "name": "Bourdon",
                "type": "project",
                "summary": "Turn compiler context.",
                "visibility": "team",
            }
        ],
    )
    codex_home = tmp_path / ".codex"
    codex_home.mkdir()

    exit_code = main(
        [
            "codex",
            "compile-turn",
            "Bourdon recognition",
            "--library-path",
            str(library),
            "--codex-home",
            str(codex_home),
        ]
    )
    report = yaml.safe_load(capsys.readouterr().out)

    assert exit_code == 0
    assert report["schema_version"] == "codex-turn-brief/v1"
    assert report["health"]["strategy"] == "turn_compiled"
    assert report["items"][0]["name"] == "Bourdon"
    assert "Bourdon turn recognition brief" in report["delivery"]["explicit_text"]


def test_cli_compile_turn_report_out_writes_requested_report(tmp_path, capsys):
    library = tmp_path / "agent-library"
    _write_l5_manifest(
        library,
        "codex",
        [
            {
                "name": "Bourdon",
                "type": "project",
                "summary": "Turn compiler context.",
                "visibility": "team",
            }
        ],
    )
    codex_home = tmp_path / ".codex"
    codex_home.mkdir()
    report_out = tmp_path / "compile-turn.yaml"

    exit_code = main(
        [
            "codex",
            "compile-turn",
            "Bourdon recognition",
            "--library-path",
            str(library),
            "--codex-home",
            str(codex_home),
            "--report-out",
            str(report_out),
            "--format",
            "json",
        ]
    )
    stdout_report = json.loads(capsys.readouterr().out)
    written_report = yaml.safe_load(report_out.read_text(encoding="utf-8"))

    assert exit_code == 0
    assert stdout_report["schema_version"] == "codex-turn-brief/v1"
    assert written_report["schema_version"] == "codex-turn-brief/v1"
    assert written_report["items"][0]["name"] == "Bourdon"


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
    """Set up a minimal ~/claude-brain/ tree the Claude Code participant can parse."""
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


# ---- claude-code-automations export -----------------------------------------


def _write_fake_automation(
    home: Path,
    automation_id: str = "weekly-pr-digest",
    memory: str = "2026-06-03\n- ShipStable launch gate verified.\n",
) -> Path:
    """Build a ~/.claude/automations/<id>/{automation.toml, memory.md} fixture."""
    automation_dir = home / ".claude" / "automations" / automation_id
    automation_dir.mkdir(parents=True)
    (automation_dir / "automation.toml").write_text(
        f"""\
version = 1
id = "{automation_id}"
kind = "loop"
name = "Weekly PR Digest"
status = "ACTIVE"
rrule = "FREQ=WEEKLY;BYDAY=MO"
cwds = ["/Users/radman/claudework"]
""",
        encoding="utf-8",
    )
    (automation_dir / "memory.md").write_text(memory, encoding="utf-8")
    return automation_dir


def test_cli_claude_code_automations_doctor_blocked_missing_dir(
    tmp_path, monkeypatch, capsys
):
    """No automations dir -> doctor reports blocked + proposed_fix."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    monkeypatch.delenv("CLAUDE_HOME", raising=False)

    exit_code = main(["claude-code-automations", "doctor"])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "blocked" in captured.out


def test_cli_claude_code_automations_export_writes_to_default_path(
    tmp_path, monkeypatch
):
    """With automations, writes to ~/agent-library/agents/claude-code-automations.l5.yaml."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    monkeypatch.delenv("CLAUDE_HOME", raising=False)
    _write_fake_automation(fake_home)

    exit_code = main(["claude-code-automations", "export"])

    assert exit_code == 0
    expected = (
        fake_home / "agent-library" / "agents" / "claude-code-automations.l5.yaml"
    )
    assert expected.is_file()
    manifest = yaml.safe_load(expected.read_text(encoding="utf-8"))
    assert manifest["agent"]["id"] == "claude-code-automations"
    assert manifest["agent"]["type"] == "other"
    entity_names = {entity["name"] for entity in manifest["known_entities"]}
    assert "weekly-pr-digest" in entity_names


def test_cli_claude_code_automations_export_out_override(tmp_path, monkeypatch):
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    monkeypatch.delenv("CLAUDE_HOME", raising=False)
    _write_fake_automation(fake_home)

    out_path = tmp_path / "custom" / "claude-code-automations.l5.yaml"
    exit_code = main(
        ["claude-code-automations", "export", "--out", str(out_path)]
    )

    assert exit_code == 0
    assert out_path.is_file()


def test_cli_claude_code_automations_export_no_dir_silent(
    tmp_path, monkeypatch, capsys
):
    """Hook contract: missing automations dir -> exit 0, no stderr."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    monkeypatch.delenv("CLAUDE_HOME", raising=False)

    exit_code = main(["claude-code-automations", "export"])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert captured.err == ""


def test_cli_claude_code_automations_export_verbose_logs_missing_dir(
    tmp_path, monkeypatch, capsys
):
    """--verbose surfaces 'no automations directory' but still exits 0."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    monkeypatch.delenv("CLAUDE_HOME", raising=False)

    exit_code = main(["claude-code-automations", "export", "--verbose"])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "no automations" in captured.err.lower()


# ---- claude-code-automations ingest-github (Path B) -------------------------


def _write_ci_artifact_source(root: Path, automation_id: str = "gh-pr-digest") -> Path:
    """Build the shape of an extracted workflow artifact: automations/<id>/..."""
    src = root / "automations" / automation_id
    src.mkdir(parents=True)
    (src / "automation.toml").write_text(
        f'id = "{automation_id}"\nname = "GH PR Digest"\n'
        f'status = "ACTIVE"\nkind = "github-action"\nrrule = ""\ncwds = []\n',
        encoding="utf-8",
    )
    (src / "memory.md").write_text(
        "2026-06-03\n- Ran PR digest in CI run 42.\n- Found 2 flaky tests.\n",
        encoding="utf-8",
    )
    return root / "automations"


def test_cli_ingest_github_from_source_dir(tmp_path, monkeypatch, capsys):
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    monkeypatch.delenv("CLAUDE_HOME", raising=False)
    src = _write_ci_artifact_source(tmp_path / "ci")

    exit_code = main(
        ["claude-code-automations", "ingest-github", "--source", str(src)]
    )
    captured = capsys.readouterr()

    assert exit_code == 0
    import json as _json

    report = _json.loads(captured.out)
    assert report["automations_seen"] == 1
    assert report["automations_created"] == 1
    assert report["bullets_added"] == 2
    dest = fake_home / ".claude" / "automations" / "gh-pr-digest" / "memory.md"
    assert dest.is_file()
    assert "Ran PR digest in CI run 42." in dest.read_text(encoding="utf-8")


def test_cli_ingest_github_requires_a_mode(tmp_path, monkeypatch, capsys):
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    monkeypatch.delenv("CLAUDE_HOME", raising=False)

    exit_code = main(["claude-code-automations", "ingest-github"])
    captured = capsys.readouterr()

    assert exit_code == 2
    assert "must specify one of" in captured.err.lower()


def test_cli_ingest_github_artifact_zip(tmp_path, monkeypatch, capsys):
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    monkeypatch.delenv("CLAUDE_HOME", raising=False)

    # Build a workflow-artifact-shaped zip
    payload = tmp_path / "payload"
    _write_ci_artifact_source(payload)
    zip_base = tmp_path / "artifact"
    import shutil

    shutil.make_archive(str(zip_base), "zip", root_dir=str(payload))
    zip_path = Path(str(zip_base) + ".zip")
    assert zip_path.is_file()

    exit_code = main(
        ["claude-code-automations", "ingest-github", "--artifact-zip", str(zip_path)]
    )
    captured = capsys.readouterr()

    assert exit_code == 0
    import json as _json

    report = _json.loads(captured.out)
    assert report["automations_created"] == 1
    dest = fake_home / ".claude" / "automations" / "gh-pr-digest" / "memory.md"
    assert dest.is_file()


def test_cli_ingest_github_missing_artifact_zip(tmp_path, monkeypatch, capsys):
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    monkeypatch.delenv("CLAUDE_HOME", raising=False)

    exit_code = main(
        [
            "claude-code-automations",
            "ingest-github",
            "--artifact-zip",
            str(tmp_path / "nope.zip"),
        ]
    )
    captured = capsys.readouterr()

    assert exit_code == 2
    assert "artifact zip not found" in captured.err.lower()


# ---- claude-code-automations ingest --gh-issue (Path C) ---------------------


def _fake_gh_runner(payload_by_cmd):
    """Build a subprocess.run stand-in keyed on the full argv tuple."""
    from types import SimpleNamespace

    def runner(cmd, capture_output=False, text=False):
        key = tuple(cmd)
        if key not in payload_by_cmd:
            return SimpleNamespace(returncode=1, stdout="", stderr=f"unmocked: {key}")
        rc, stdout, stderr = payload_by_cmd[key]
        return SimpleNamespace(returncode=rc, stdout=stdout, stderr=stderr)

    return runner


def test_cli_ingest_gh_issue_merges_comments(tmp_path, monkeypatch, capsys):
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    monkeypatch.delenv("CLAUDE_HOME", raising=False)

    # Stub `gh issue view` to return a synthetic payload covering body + 2 comments.
    import json as _json

    payload = {
        "title": "Routine: Weekly PR Audit",
        "body": "- Initial summary on 2026-06-01.\n- Two PRs reviewed.",
        "createdAt": "2026-06-01T15:00:00Z",
        "comments": [
            {
                "body": "- 2026-06-08 run: 4 PRs reviewed.\n- ShipStable PR #213 needs follow-up.",
                "createdAt": "2026-06-08T15:00:00Z",
            },
            {
                "body": "Plain-text comment without bullets.",
                "createdAt": "2026-06-15T15:00:00Z",
            },
        ],
    }
    expected_cmd = (
        "gh", "issue", "view", "42",
        "--repo", "foo/bar",
        "--json", "title,body,comments,createdAt",
    )
    runner = _fake_gh_runner({expected_cmd: (0, _json.dumps(payload), "")})

    # Patch the subprocess.run callable that the handler uses.
    import cli.main as cli_main

    monkeypatch.setattr(cli_main.subprocess, "run", runner, raising=False)
    # Also pretend gh is installed
    monkeypatch.setattr(cli_main.shutil, "which", lambda name: "/usr/local/bin/gh", raising=False)

    exit_code = main(
        [
            "claude-code-automations",
            "ingest-github",
            "--gh-issue",
            "foo/bar#42",
            "--automation-id",
            "weekly-pr-audit",
        ]
    )
    captured = capsys.readouterr()

    assert exit_code == 0
    report = _json.loads(captured.out)
    assert report["automations_created"] == 1
    # 3 source bullets (issue body has 2) + 2 from comment1 + 1 from comment2 = 5
    assert report["bullets_added"] >= 5

    memory = (
        fake_home / ".claude" / "automations" / "weekly-pr-audit" / "memory.md"
    ).read_text(encoding="utf-8")
    assert "Two PRs reviewed." in memory
    assert "ShipStable PR #213" in memory
    assert "Plain-text comment without bullets." in memory
    # Three distinct date sections
    assert "2026-06-01" in memory
    assert "2026-06-08" in memory
    assert "2026-06-15" in memory


def test_cli_ingest_gh_issue_requires_automation_id(tmp_path, monkeypatch, capsys):
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    monkeypatch.delenv("CLAUDE_HOME", raising=False)

    import cli.main as cli_main

    monkeypatch.setattr(cli_main.shutil, "which", lambda name: "/usr/local/bin/gh", raising=False)

    exit_code = main(
        ["claude-code-automations", "ingest-github", "--gh-issue", "foo/bar#42"]
    )
    captured = capsys.readouterr()

    assert exit_code == 2
    assert "--automation-id" in captured.err


def test_cli_ingest_gh_issue_rejects_bad_format(tmp_path, monkeypatch, capsys):
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    monkeypatch.delenv("CLAUDE_HOME", raising=False)

    import cli.main as cli_main

    monkeypatch.setattr(cli_main.shutil, "which", lambda name: "/usr/local/bin/gh", raising=False)

    exit_code = main(
        [
            "claude-code-automations",
            "ingest-github",
            "--gh-issue",
            "no-hash-here",
            "--automation-id",
            "anything",
        ]
    )
    captured = capsys.readouterr()

    assert exit_code == 2
    assert "owner/repo#N" in captured.err


def test_cli_ingest_gh_issue_idempotent_on_repeat(tmp_path, monkeypatch, capsys):
    """Re-ingesting the same issue is a no-op (merge dedupes by bullet)."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    monkeypatch.delenv("CLAUDE_HOME", raising=False)

    import json as _json

    payload = {
        "body": "- Only line.",
        "createdAt": "2026-06-01T00:00:00Z",
        "comments": [],
    }
    expected_cmd = (
        "gh", "issue", "view", "1",
        "--repo", "x/y",
        "--json", "title,body,comments,createdAt",
    )
    runner = _fake_gh_runner({expected_cmd: (0, _json.dumps(payload), "")})

    import cli.main as cli_main

    monkeypatch.setattr(cli_main.subprocess, "run", runner, raising=False)
    monkeypatch.setattr(cli_main.shutil, "which", lambda name: "/usr/local/bin/gh", raising=False)

    args = ["claude-code-automations", "ingest-github",
            "--gh-issue", "x/y#1", "--automation-id", "only"]
    assert main(args) == 0
    capsys.readouterr()  # drain
    assert main(args) == 0
    report = _json.loads(capsys.readouterr().out)
    # Second pass: no new bullets, no new automations created, no new sections.
    assert report["automations_created"] == 0
    assert report["bullets_added"] == 0
    assert report["sections_created"] == 0


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


def test_cli_codex_eval_turn_compiler_flag_attaches_routing_metrics(tmp_path):
    report_path = tmp_path / "report.yaml"

    exit_code = main(
        [
            "codex",
            "eval",
            "--fixtures",
            "--turn-compiler",
            "--report-out",
            str(report_path),
        ]
    )
    report = yaml.safe_load(report_path.read_text(encoding="utf-8"))
    turn_compiler = report["turn_compiler"]

    assert exit_code == 0
    assert turn_compiler["prompts_tested"] == 5
    assert turn_compiler["compiled_hits"] >= 1
    assert turn_compiler["compiled_hit_rate"] > 0.0
    assert turn_compiler["avg_latency_us"] < 100_000.0
    assert "explicit_pre_turn" in turn_compiler["primary_surfaces"]
    assert turn_compiler["results"][0]["prompt"] == "Tell me about Coolculator"
    assert "top_score" in turn_compiler["results"][0]


# -- serve: peer federation wiring ---------------------------------------------


def _patch_serve_runtime(monkeypatch) -> dict:
    """Patch create_l6_server + run_l6_server so `bourdon serve` exercises its
    peer-wiring path without actually starting an MCP server. Returns a dict
    capturing the store the server was built from and the run() kwargs."""
    captured: dict = {}

    def fake_create(store, name="bourdon-l6"):
        captured["store"] = store
        return object()

    def fake_run(server, **kwargs):
        captured["run_kwargs"] = kwargs

    monkeypatch.setattr("core.l6_server.create_l6_server", fake_create)
    monkeypatch.setattr("core.l6_server.run_l6_server", fake_run)
    return captured


def _serve_library(tmp_path: Path) -> Path:
    library = tmp_path / "agent-library"
    _write_l5_manifest(
        library,
        "claude-code",
        [{"name": "Bourdon", "type": "topic", "summary": "x", "visibility": "team"}],
    )
    return library


def test_cli_serve_wires_peers_from_config(tmp_path, monkeypatch):
    library = _serve_library(tmp_path)
    cfg = tmp_path / "peers.yaml"
    cfg.write_text(
        yaml.safe_dump({"peers": [{"name": "pc", "url": "http://pc.tailnet:7500"}]}),
        encoding="utf-8",
    )
    captured = _patch_serve_runtime(monkeypatch)

    exit_code = main(
        ["serve", "--library", str(library), "--peers-config", str(cfg), "--quiet"]
    )

    assert exit_code == 0
    store = captured["store"]
    assert [p.name for p in store.peers] == ["pc"]
    assert store.peers[0].url == "http://pc.tailnet:7500/mcp"
    assert captured["run_kwargs"]["transport"] == "stdio"


def test_cli_serve_wires_inline_peer_flag(tmp_path, monkeypatch):
    library = _serve_library(tmp_path)
    captured = _patch_serve_runtime(monkeypatch)

    exit_code = main(
        [
            "serve",
            "--library", str(library),
            "--peer", "http://localhost:7501",
            "--peers-config", str(tmp_path / "absent.yaml"),
            "--quiet",
        ]
    )

    assert exit_code == 0
    store = captured["store"]
    assert [p.url for p in store.peers] == ["http://localhost:7501/mcp"]


def test_cli_serve_no_peers_by_default(tmp_path, monkeypatch):
    library = _serve_library(tmp_path)
    captured = _patch_serve_runtime(monkeypatch)

    # Point --peers-config at a guaranteed-absent path so a real ~/.bourdon/
    # peers.yaml on the dev machine can't leak into this hermetic test.
    exit_code = main(
        [
            "serve",
            "--library", str(library),
            "--peers-config", str(tmp_path / "absent.yaml"),
            "--quiet",
        ]
    )

    assert exit_code == 0
    assert captured["store"].peers == []
    assert captured["run_kwargs"] == {
        "transport": "stdio",
        "port": 7500,
        "host": "0.0.0.0",
        "allow_unauthenticated": False,
    }
