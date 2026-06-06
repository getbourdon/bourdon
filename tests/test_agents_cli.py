"""Tests for the top-level `bourdon agents --json` CLI verb.

This is the read foundation for the Phase 0 desktop tray: it enumerates the
local L5 manifests, applies the project's canonical credential redaction, and
emits a stable JSON object the tray consumes. Redaction and partial-failure
representation are the load-bearing behaviors locked in here.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from cli.main import main

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

_WELL_FORMED = {
    "spec_version": "0.1",
    "agent": {
        "id": "codex",
        "type": "code-assistant",
        "instance": "DeskOp",
        "role_narrative": "Lead code-assistant. Organizes project code.",
    },
    "last_updated": "2026-05-26T15:57:38+00:00",
    "capabilities": ["codex_home", "memory_md", "state_db"],
    "recent_sessions": [
        {
            "date": "2026-05-13",
            "cwd": "/tmp/proj",
            "project_focus": ["Bourdon"],
            "key_actions": ["Wire recognition layer"],
            "visibility": "team",
        },
        {
            "date": "2026-05-24",
            "cwd": "/tmp/proj",
            "project_focus": ["New Project 2"],
            "key_actions": ["Refactor app"],
            "visibility": "private",
        },
    ],
}

# A manifest with a fresher last_updated -- must sort ahead of _WELL_FORMED.
_FRESH = {
    "spec_version": "0.1",
    "agent": {
        "id": "cascade",
        "type": "ide-assistant",
        "instance": "Windsurf",
        "role_narrative": "IDE pair assistant.",
    },
    "last_updated": "2026-06-01T09:00:00+00:00",
    "capabilities": ["memory_md"],
    "recent_sessions": [
        {
            "date": "2026-06-01",
            "project_focus": ["ILTT"],
            "key_actions": ["Ship marketplace"],
            "visibility": "team",
        }
    ],
}

# A manifest whose role_narrative + a key_action carry fake credentials.
_SECRETS = {
    "spec_version": "0.1",
    "agent": {
        "id": "copilot",
        "type": "code-assistant",
        "instance": "VSCode",
        "role_narrative": "Helper. password=hunter2 do not leak.",
    },
    "last_updated": "2026-05-20T00:00:00+00:00",
    "capabilities": [],
    "recent_sessions": [
        {
            "date": "2026-05-20",
            "project_focus": ["Pay sk_live_DEADBEEF1234567890 flow"],
            "key_actions": [
                "Rotate api_key for prod",
                "Set Authorization to bearer token abc.def.ghi",
            ],
            "visibility": "team",
        }
    ],
}


def _write_manifest(agents_dir: Path, name: str, data: dict) -> None:
    (agents_dir / f"{name}.l5.yaml").write_text(
        yaml.safe_dump(data, sort_keys=False), encoding="utf-8"
    )


@pytest.fixture
def agents_dir(tmp_path: Path) -> Path:
    d = tmp_path / "agent-library" / "agents"
    d.mkdir(parents=True)
    # A subdirectory that must be ignored (mirrors the real claude-code/ dir).
    (d / "claude-code").mkdir()
    return d


def _run_agents(agents_dir: Path, capsys) -> dict:
    exit_code = main(["agents", "--json", "--agents-dir", str(agents_dir)])
    out = capsys.readouterr().out
    assert exit_code == 0
    return json.loads(out)


# ---------------------------------------------------------------------------
# Shape / schema
# ---------------------------------------------------------------------------


def test_agents_emits_schema_envelope(agents_dir: Path, capsys):
    _write_manifest(agents_dir, "codex", _WELL_FORMED)
    report = _run_agents(agents_dir, capsys)

    assert report["schema"] == "bourdon.agents/v1"
    assert report["generated_from"] == str(agents_dir)
    assert isinstance(report["agents"], list)
    assert len(report["agents"]) == 1


def test_agents_well_formed_fields_and_counts(agents_dir: Path, capsys):
    _write_manifest(agents_dir, "codex", _WELL_FORMED)
    agent = _run_agents(agents_dir, capsys)["agents"][0]

    assert agent["id"] == "codex"
    assert agent["type"] == "code-assistant"
    assert agent["instance"] == "DeskOp"
    assert agent["role_narrative"].startswith("Lead code-assistant")
    assert agent["last_updated"] == "2026-05-26T15:57:38+00:00"
    assert agent["capability_count"] == 3
    assert agent["session_count"] == 2
    # Freshest of 2026-05-13 / 2026-05-24.
    assert agent["freshest_session_date"] == "2026-05-24"
    assert agent["parse_error"] is None


def test_agents_recent_activity_sorted_desc_and_keeps_visibility(
    agents_dir: Path, capsys
):
    _write_manifest(agents_dir, "codex", _WELL_FORMED)
    agent = _run_agents(agents_dir, capsys)["agents"][0]

    dates = [s["date"] for s in agent["recent_activity"]]
    assert dates == ["2026-05-24", "2026-05-13"]
    # Private sessions are NOT dropped -- just tagged.
    visibilities = {s["visibility"] for s in agent["recent_activity"]}
    assert visibilities == {"team", "private"}


def test_agents_sorted_by_last_updated_desc(agents_dir: Path, capsys):
    _write_manifest(agents_dir, "codex", _WELL_FORMED)
    _write_manifest(agents_dir, "cascade", _FRESH)
    report = _run_agents(agents_dir, capsys)

    ids = [a["id"] for a in report["agents"]]
    # cascade (2026-06-01) is fresher than codex (2026-05-26).
    assert ids == ["cascade", "codex"]


def test_agents_ignores_subdirectories(agents_dir: Path, capsys):
    _write_manifest(agents_dir, "codex", _WELL_FORMED)
    # claude-code/ subdir exists (from fixture) but holds no *.l5.yaml file.
    report = _run_agents(agents_dir, capsys)
    assert [a["id"] for a in report["agents"]] == ["codex"]


# ---------------------------------------------------------------------------
# Redaction (the whole point)
# ---------------------------------------------------------------------------


def test_agents_redacts_credentials_in_all_string_fields(agents_dir: Path, capsys):
    _write_manifest(agents_dir, "copilot", _SECRETS)
    report = _run_agents(agents_dir, capsys)
    blob = json.dumps(report)

    # No raw secret survives anywhere in the output.
    assert "hunter2" not in blob
    assert "sk_live_DEADBEEF1234567890" not in blob
    assert "abc.def.ghi" not in blob
    assert "[redacted credential-like text]" in blob

    agent = report["agents"][0]
    assert agent["role_narrative"] == "[redacted credential-like text]"
    session = agent["recent_activity"][0]
    assert session["project_focus"] == ["[redacted credential-like text]"]
    assert "[redacted credential-like text]" in session["key_actions"]


# ---------------------------------------------------------------------------
# Partial failure
# ---------------------------------------------------------------------------


def test_agents_malformed_manifest_yields_parse_error_and_keeps_good_ones(
    agents_dir: Path, capsys
):
    _write_manifest(agents_dir, "codex", _WELL_FORMED)
    (agents_dir / "broken.l5.yaml").write_text(
        "agent: {id: oops\n  : : not valid yaml :::\n", encoding="utf-8"
    )

    report = _run_agents(agents_dir, capsys)
    by_id = {a["id"]: a for a in report["agents"]}

    # Good agent still present and intact.
    assert by_id["codex"]["parse_error"] is None
    assert by_id["codex"]["session_count"] == 2

    # Broken manifest represented inline, keyed by filename stem, with nulls.
    assert "broken" in by_id
    assert by_id["broken"]["parse_error"]
    assert by_id["broken"]["last_updated"] is None
    assert by_id["broken"]["recent_activity"] == []


def test_agents_non_mapping_manifest_is_a_parse_error(agents_dir: Path, capsys):
    (agents_dir / "scalar.l5.yaml").write_text("just a string\n", encoding="utf-8")
    report = _run_agents(agents_dir, capsys)
    entry = report["agents"][0]
    assert entry["id"] == "scalar"
    assert entry["parse_error"] == "manifest is not a YAML mapping"


# ---------------------------------------------------------------------------
# Dir-level failure vs. no-data
# ---------------------------------------------------------------------------


def test_agents_empty_dir_is_exit_zero_empty_list(agents_dir: Path, capsys):
    report = _run_agents(agents_dir, capsys)
    assert report["agents"] == []


def test_agents_missing_dir_exits_nonzero(tmp_path: Path, capsys):
    missing = tmp_path / "nope" / "agents"
    exit_code = main(["agents", "--json", "--agents-dir", str(missing)])
    assert exit_code != 0


def test_agents_default_dir_resolves_under_home(tmp_path, monkeypatch, capsys):
    fake_home = tmp_path / "home"
    (fake_home / "agent-library" / "agents").mkdir(parents=True)
    _write_manifest(fake_home / "agent-library" / "agents", "codex", _WELL_FORMED)
    monkeypatch.setattr(Path, "home", lambda: fake_home)

    exit_code = main(["agents", "--json"])
    report = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert [a["id"] for a in report["agents"]] == ["codex"]
