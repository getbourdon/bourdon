"""Tests for core.agents_export -- the single shared L5-manifest summarizer.

Covers the per-agent shape (incl. the source/source_kind attribution fields),
that the canonical credential redaction is applied, partial-failure handling,
sort order, and local-name resolution.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from core.agents_export import (
    AGENTS_SCHEMA,
    error_agent_entry,
    export_local_agents,
    resolve_local_name,
    summarize_agent_manifest,
)

_WELL_FORMED = {
    "spec_version": "0.1",
    "agent": {
        "id": "codex",
        "type": "code-assistant",
        "instance": "DeskOp",
        "role_narrative": "Lead code-assistant.",
    },
    "last_updated": "2026-05-26T15:57:38+00:00",
    "capabilities": ["codex_home", "memory_md", "state_db"],
    "recent_sessions": [
        {"date": "2026-05-13", "project_focus": ["Bourdon"], "visibility": "team"},
        {"date": "2026-05-24", "project_focus": ["Other"], "visibility": "private"},
    ],
}

_SECRETS = {
    "spec_version": "0.1",
    "agent": {
        "id": "copilot",
        "type": "code-assistant",
        "role_narrative": "Helper. password=hunter2 do not leak.",
    },
    "last_updated": "2026-05-20T00:00:00+00:00",
    "recent_sessions": [
        {
            "date": "2026-05-20",
            "project_focus": ["Pay sk_live_DEADBEEF1234567890 flow"],
            "key_actions": ["Set Authorization to bearer token abc.def.ghi"],
            "visibility": "team",
        }
    ],
}


def _write(agents_dir: Path, name: str, data: dict) -> None:
    (agents_dir / f"{name}.l5.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")


@pytest.fixture
def agents_dir(tmp_path: Path) -> Path:
    d = tmp_path / "agent-library" / "agents"
    d.mkdir(parents=True)
    return d


# -- summarize_agent_manifest --------------------------------------------------


def test_summarize_shape_includes_source_fields():
    summary = summarize_agent_manifest(_WELL_FORMED, source="pc")
    # Canonical per-agent contract.
    expected_keys = {
        "id",
        "type",
        "instance",
        "role_narrative",
        "last_updated",
        "capability_count",
        "session_count",
        "freshest_session_date",
        "recent_activity",
        "parse_error",
        "source",
        "source_kind",
    }
    assert set(summary) == expected_keys
    assert summary["id"] == "codex"
    assert summary["capability_count"] == 3
    assert summary["session_count"] == 2
    assert summary["freshest_session_date"] == "2026-05-24"
    assert summary["source"] == "pc"
    assert summary["source_kind"] == "local"


def test_summarize_recent_activity_sorted_and_keeps_visibility():
    summary = summarize_agent_manifest(_WELL_FORMED, source="pc")
    dates = [s["date"] for s in summary["recent_activity"]]
    assert dates == ["2026-05-24", "2026-05-13"]
    assert {s["visibility"] for s in summary["recent_activity"]} == {"team", "private"}


def test_summarize_applies_redaction():
    summary = summarize_agent_manifest(_SECRETS, source="pc")
    blob = repr(summary)
    assert "hunter2" not in blob
    assert "sk_live_DEADBEEF1234567890" not in blob
    assert "abc.def.ghi" not in blob
    assert summary["role_narrative"] == "[redacted credential-like text]"


def test_error_entry_carries_source():
    entry = error_agent_entry("broken", "boom", source="pc")
    assert entry["id"] == "broken"
    assert entry["parse_error"] == "boom"
    assert entry["last_updated"] is None
    assert entry["source"] == "pc"
    assert entry["source_kind"] == "local"


# -- export_local_agents -------------------------------------------------------


def test_export_local_agents_envelope_and_attribution(agents_dir: Path):
    _write(agents_dir, "codex", _WELL_FORMED)
    report = export_local_agents(agents_dir, "pc")
    assert report["schema"] == AGENTS_SCHEMA
    assert report["machine"] == "pc"
    assert report["generated_from"] == str(agents_dir)
    assert len(report["agents"]) == 1
    assert report["agents"][0]["source"] == "pc"
    assert report["agents"][0]["source_kind"] == "local"


def test_export_local_agents_redaction_end_to_end(agents_dir: Path):
    _write(agents_dir, "copilot", _SECRETS)
    report = export_local_agents(agents_dir, "pc")
    blob = repr(report)
    assert "hunter2" not in blob
    assert "sk_live_DEADBEEF1234567890" not in blob
    assert "[redacted credential-like text]" in blob


def test_export_local_agents_sorted_by_last_updated_desc(agents_dir: Path):
    _write(agents_dir, "codex", _WELL_FORMED)  # 2026-05-26
    fresh = {
        **_WELL_FORMED,
        "agent": {"id": "cascade"},
        "last_updated": "2026-06-01T00:00:00+00:00",
    }
    _write(agents_dir, "cascade", fresh)
    report = export_local_agents(agents_dir, "pc")
    assert [a["id"] for a in report["agents"]] == ["cascade", "codex"]


def test_export_local_agents_partial_failure_inline(agents_dir: Path):
    _write(agents_dir, "codex", _WELL_FORMED)
    (agents_dir / "broken.l5.yaml").write_text(
        "agent: {id: oops\n  : : not valid yaml :::\n", encoding="utf-8"
    )
    (agents_dir / "scalar.l5.yaml").write_text("just a string\n", encoding="utf-8")
    report = export_local_agents(agents_dir, "pc")
    by_id = {a["id"]: a for a in report["agents"]}
    assert by_id["codex"]["parse_error"] is None
    assert by_id["broken"]["parse_error"]
    assert by_id["scalar"]["parse_error"] == "manifest is not a YAML mapping"
    # Even broken entries are source-attributed.
    assert by_id["broken"]["source"] == "pc"


def test_export_local_agents_missing_dir_returns_empty(tmp_path: Path):
    report = export_local_agents(tmp_path / "nope", "pc")
    assert report["agents"] == []
    assert report["machine"] == "pc"


# -- resolve_local_name --------------------------------------------------------


def test_resolve_local_name_prefers_env(monkeypatch):
    monkeypatch.setenv("BOURDON_LOCAL_NAME", "  my-box  ")
    assert resolve_local_name() == "my-box"


def test_resolve_local_name_falls_back_to_hostname(monkeypatch):
    monkeypatch.delenv("BOURDON_LOCAL_NAME", raising=False)
    monkeypatch.setattr("core.agents_export.socket.gethostname", lambda: "host-xyz")
    assert resolve_local_name() == "host-xyz"
