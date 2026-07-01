"""Tests for core.presence (live-session registry) + its tray-contract join."""

from __future__ import annotations

import os
import stat
import sys
from datetime import timedelta

import pytest

from core import presence


@pytest.fixture(autouse=True)
def _isolated_bourdon_home(tmp_path, monkeypatch):
    """Point ~/.bourdon at a tmp dir so tests never touch the real presence dir."""
    monkeypatch.setenv("BOURDON_HOME", str(tmp_path / "dot-bourdon"))
    # Ensure a deterministic TTL regardless of the caller's environment.
    monkeypatch.delenv("BOURDON_PRESENCE_TTL", raising=False)
    yield


# --- register / list / deregister --------------------------------------------


def test_register_then_live():
    presence.register("claude-code", "s1", cwd="/x/bourdon")
    presence.register("claude-code", "s2", cwd="/x/iltt")
    presence.register("codex", "s3", cwd="/x/prun")

    assert len(presence.live_sessions()) == 3
    by_agent = presence.live_sessions_by_agent()
    assert {k: len(v) for k, v in by_agent.items()} == {"claude-code": 2, "codex": 1}


def test_project_is_cwd_basename():
    presence.register("claude-code", "s1", cwd="/Users/x/repos/ILTT")
    (session,) = presence.live_sessions()
    assert session["project"] == "ILTT"
    assert session["instance"] == "s1"


def test_deregister_removes_session():
    presence.register("codex", "s1")
    assert presence.deregister("codex", "s1") is True
    assert presence.live_sessions() == []
    # Idempotent: deregistering a gone session is not an error.
    assert presence.deregister("codex", "s1") is False


def test_heartbeat_preserves_started_at():
    presence.register("claude-code", "s1")
    path = presence._session_file("claude-code", "s1")
    started = presence._read_one(path)["started_at"]
    presence.heartbeat("claude-code", "s1")
    assert presence._read_one(path)["started_at"] == started


def test_heartbeat_self_heals_when_file_missing():
    # No prior register: a bare heartbeat should still create the session.
    presence.heartbeat("codex", "s-orphan", cwd="/x/prun")
    (session,) = presence.live_sessions()
    assert session["session_id"] == "s-orphan"
    assert session["project"] == "prun"


# --- TTL / liveness / reaping -------------------------------------------------


def test_ttl_zero_makes_nothing_live():
    presence.register("codex", "s1")
    assert presence.live_sessions(ttl_seconds=0, reap=False) == []


def test_stale_session_not_live_and_reaped():
    presence.register("codex", "s1")
    path = presence._session_file("codex", "s1")
    now = presence._now()
    # A read far in the future: heartbeat is well beyond TTL * REAP_MULTIPLIER.
    future = now + timedelta(seconds=presence.DEFAULT_TTL_SECONDS * presence.REAP_MULTIPLIER + 60)
    assert presence.live_sessions(now=future, reap=True) == []
    assert not path.exists()  # hard-stale file was reaped


def test_idle_session_within_ttl_stays_live():
    presence.register("claude-code", "s1")
    now = presence._now()
    # Idle but within the generous TTL (e.g. waiting on the user between turns).
    within = now + timedelta(seconds=presence.DEFAULT_TTL_SECONDS - 5)
    assert len(presence.live_sessions(now=within)) == 1


# --- security ----------------------------------------------------------------


def test_filename_sanitizes_path_traversal():
    path = presence._session_file("../evil", "a/b/c")
    assert "/" not in path.name.replace(".json", "")
    assert path.parent == presence.presence_dir()
    assert path.name == "..-evil__a-b-c.json"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file mode")
def test_session_file_is_0600():
    presence.register("codex", "s1")
    path = presence._session_file("codex", "s1")
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


# --- tray-contract join ------------------------------------------------------


def _write_manifest(agents_dir, agent_id):
    agents_dir.mkdir(parents=True, exist_ok=True)
    (agents_dir / f"{agent_id}.l5.yaml").write_text(
        "spec_version: '0.1'\n"
        f"agent:\n  id: {agent_id}\n  type: code-assistant\n"
        "last_updated: '2026-06-30T00:00:00Z'\n"
        "capabilities: []\n"
        "recent_sessions: []\n",
        encoding="utf-8",
    )


def test_export_local_agents_joins_live_count(tmp_path):
    from core.agents_export import export_local_agents

    agents_dir = tmp_path / "agent-library" / "agents"
    _write_manifest(agents_dir, "claude-code")
    _write_manifest(agents_dir, "codex")

    presence.register("claude-code", "s1", cwd="/x/bourdon")
    presence.register("claude-code", "s2", cwd="/x/iltt")

    report = export_local_agents(agents_dir, "test-machine")
    by_id = {a["id"]: a for a in report["agents"]}

    assert by_id["claude-code"]["live_count"] == 2
    assert {s["project"] for s in by_id["claude-code"]["live_sessions"]} == {"bourdon", "iltt"}
    # An agent with no live session still gets the fields, at zero.
    assert by_id["codex"]["live_count"] == 0
    assert by_id["codex"]["live_sessions"] == []


def test_join_is_best_effort_when_presence_absent(tmp_path):
    from core.agents_export import export_local_agents

    agents_dir = tmp_path / "agent-library" / "agents"
    _write_manifest(agents_dir, "codex")
    # No sessions registered → presence dir does not exist yet.
    report = export_local_agents(agents_dir, "test-machine")
    (agent,) = report["agents"]
    assert agent["live_count"] == 0
    assert agent["live_sessions"] == []
