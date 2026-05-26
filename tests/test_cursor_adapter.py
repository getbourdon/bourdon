"""Tests for adapters.cursor."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from adapters.base import AdapterDiscoveryError, L5Manifest
from adapters.cursor import AGENT_ID, AGENT_TYPE, CursorAdapter

# ---- Helpers ----------------------------------------------------------------


def _seed_state_db(path: Path, records: list[tuple[str, dict]]) -> None:
    """Seed a Cursor-shaped state.vscdb with synthetic ItemTable rows."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("CREATE TABLE ItemTable (key TEXT PRIMARY KEY, value TEXT)")
        for key, value in records:
            conn.execute(
                "INSERT INTO ItemTable (key, value) VALUES (?, ?)",
                (key, json.dumps(value)),
            )
        conn.commit()
    finally:
        conn.close()


def _make_cursor_dir(tmp_path: Path) -> Path:
    """Set up a Cursor-shaped data directory under ``tmp_path``."""
    cursor_dir = tmp_path / "Cursor"
    (cursor_dir / "User" / "globalStorage").mkdir(parents=True)
    (cursor_dir / "User" / "workspaceStorage" / "abc123").mkdir(parents=True)
    return cursor_dir


# ---- discover() -------------------------------------------------------------


def test_discover_raises_when_dir_missing(tmp_path):
    adapter = CursorAdapter(cursor_dir=tmp_path / "does-not-exist")
    with pytest.raises(AdapterDiscoveryError):
        adapter.discover()


def test_discover_returns_agent_store_when_dir_exists(tmp_path):
    cursor_dir = _make_cursor_dir(tmp_path)
    adapter = CursorAdapter(cursor_dir=cursor_dir)
    store = adapter.discover()
    assert store.path == str(cursor_dir)
    assert "platform_default" in store.metadata


# ---- export_l5() ------------------------------------------------------------


def test_export_l5_empty_when_no_dbs(tmp_path):
    cursor_dir = _make_cursor_dir(tmp_path)
    adapter = CursorAdapter(cursor_dir=cursor_dir)
    manifest = adapter.export_l5()
    assert isinstance(manifest, L5Manifest)
    assert manifest.agent.id == AGENT_ID
    assert manifest.agent.type == AGENT_TYPE
    assert manifest.recent_sessions == []
    assert manifest.known_entities == []


def test_export_l5_extracts_session_and_project_entity(tmp_path):
    cursor_dir = _make_cursor_dir(tmp_path)
    db = cursor_dir / "User" / "workspaceStorage" / "abc123" / "state.vscdb"
    _seed_state_db(
        db,
        [
            (
                "composer.composerData",
                {
                    "workspacePath": "/Users/dev/projects/my-app",
                    "title": "Add user authentication",
                    "messages": [],
                    "lastUpdatedAt": "2026-04-30T10:00:00Z",
                },
            ),
        ],
    )

    adapter = CursorAdapter(cursor_dir=cursor_dir)
    manifest = adapter.export_l5()

    # At least one session extracted
    assert len(manifest.recent_sessions) >= 1
    session = manifest.recent_sessions[0]
    assert "/projects/my-app" in (session.cwd or "")

    # Project entity inferred from cwd
    project_names = {e.name for e in manifest.known_entities}
    assert "my-app" in project_names


def test_export_l5_filters_by_since(tmp_path):
    cursor_dir = _make_cursor_dir(tmp_path)
    db = cursor_dir / "state.vscdb"
    _seed_state_db(
        db,
        [
            (
                "composer.old",
                {
                    "workspacePath": "/p/old",
                    "title": "old work",
                    "messages": [],
                    "lastUpdatedAt": "2025-01-01T00:00:00Z",
                },
            ),
            (
                "composer.new",
                {
                    "workspacePath": "/p/new",
                    "title": "new work",
                    "messages": [],
                    "lastUpdatedAt": "2026-04-30T00:00:00Z",
                },
            ),
        ],
    )

    adapter = CursorAdapter(cursor_dir=cursor_dir)
    cutoff = datetime(2026, 1, 1, tzinfo=timezone.utc)
    manifest = adapter.export_l5(since=cutoff)
    dates = [s.date for s in manifest.recent_sessions]
    assert all(d >= "2026-01-01" for d in dates if d), dates
    assert len(manifest.recent_sessions) == 1
    assert manifest.recent_sessions[0].key_actions[0] == "new work"


# ---- export_sessions() ------------------------------------------------------


def test_export_sessions_respects_limit(tmp_path):
    cursor_dir = _make_cursor_dir(tmp_path)
    db = cursor_dir / "state.vscdb"
    records = [
        (
            f"composer.{i}",
            {
                "workspacePath": f"/p/proj{i}",
                "title": f"work {i}",
                "messages": [],
                "lastUpdatedAt": f"2026-04-{i + 10:02d}T00:00:00Z",
            },
        )
        for i in range(5)
    ]
    _seed_state_db(db, records)

    adapter = CursorAdapter(cursor_dir=cursor_dir)
    sessions = adapter.export_sessions(
        since=datetime(2020, 1, 1, tzinfo=timezone.utc), limit=3
    )
    assert len(sessions) == 3


# ---- health_check() ---------------------------------------------------------


def test_health_check_blocked_when_no_dir(tmp_path):
    adapter = CursorAdapter(cursor_dir=tmp_path / "missing")
    health = adapter.health_check()
    assert health.status == "blocked"
    assert health.proposed_fix is not None
    assert "Cursor" in health.proposed_fix


def test_health_check_degraded_when_no_dbs(tmp_path):
    cursor_dir = _make_cursor_dir(tmp_path)
    adapter = CursorAdapter(cursor_dir=cursor_dir)
    health = adapter.health_check()
    assert health.status == "degraded"
    assert "No Cursor SQLite stores" in (health.reason or "")
    assert health.proposed_fix is not None
    assert "cursor export" in health.proposed_fix


def test_health_check_ok_when_dbs_present(tmp_path):
    cursor_dir = _make_cursor_dir(tmp_path)
    db = cursor_dir / "state.vscdb"
    _seed_state_db(db, [])  # empty db is fine; just needs ItemTable
    adapter = CursorAdapter(cursor_dir=cursor_dir)
    health = adapter.health_check()
    assert health.status == "ok"
    assert health.details["databases_scanned"] >= 1
    assert health.proposed_fix is None


def test_health_check_does_not_raise_on_corrupt_db(tmp_path):
    cursor_dir = _make_cursor_dir(tmp_path)
    db = cursor_dir / "state.vscdb"
    db.write_bytes(b"not a real sqlite database")
    adapter = CursorAdapter(cursor_dir=cursor_dir)
    # Must not raise; status may be ok/degraded depending on whether the
    # corrupt file is iterable. The contract is "never raises".
    health = adapter.health_check()
    assert health.status in {"ok", "degraded", "blocked"}


# ---- Protocol conformance ---------------------------------------------------


def test_cursor_adapter_class_attrs():
    assert CursorAdapter.agent_id == "cursor"
    assert CursorAdapter.agent_type == "code-assistant"


def test_native_path_resolves(tmp_path):
    adapter = CursorAdapter(cursor_dir=tmp_path / "Cursor")
    assert adapter.native_path == str(tmp_path / "Cursor")
