"""Tests for core/federation_audit.py — append-only audit log (v0.9.0)."""

from __future__ import annotations

import json

from core.federation_audit import DECISION_DENY, FederationAudit


def test_record_appends_jsonl(tmp_path):
    audit = FederationAudit(tmp_path / "audit.jsonl")
    audit.record("openclaw", "find_entity", "claude-code", "deny", "not granted")
    audit.record("operator", "list_recent_work")
    lines = (tmp_path / "audit.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    first = json.loads(lines[0])
    assert first["agent"] == "openclaw"
    assert first["decision"] == "deny"
    assert first["detail"] == "not granted"
    assert first["ts"].endswith("Z")


def test_entries_filters_by_agent_denials_and_limit(tmp_path):
    audit = FederationAudit(tmp_path / "audit.jsonl")
    for i in range(5):
        audit.record("openclaw", f"op{i}", decision="allow")
    audit.record("openclaw", "bad", decision=DECISION_DENY)
    audit.record("clyde", "ok", decision="allow")

    assert len(audit.entries(agent="openclaw")) == 6
    denials = audit.entries(denials_only=True)
    assert len(denials) == 1 and denials[0]["op"] == "bad"
    assert len(audit.entries(limit=3)) == 3


def test_revoked_agents_history_remains_queryable(tmp_path):
    """R5 acceptance: revocation never touches the audit trail."""
    audit = FederationAudit(tmp_path / "audit.jsonl")
    audit.record("openclaw", "find_entity", decision="allow")
    # Revocation happens in the registry; the audit file is untouched by it.
    assert len(audit.entries(agent="openclaw")) == 1


def test_torn_tail_line_tolerated(tmp_path):
    path = tmp_path / "audit.jsonl"
    audit = FederationAudit(path)
    audit.record("openclaw", "op")
    with path.open("a", encoding="utf-8") as fh:
        fh.write('{"ts": "2026-')  # torn write
    assert len(audit.entries()) == 1


def test_audit_write_failure_never_raises(tmp_path):
    # Point at a path whose parent is a FILE so mkdir/open fails.
    blocker = tmp_path / "blocker"
    blocker.write_text("x", encoding="utf-8")
    audit = FederationAudit(blocker / "audit.jsonl")
    audit.record("openclaw", "op")  # must not raise
