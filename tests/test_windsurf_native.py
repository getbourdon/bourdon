"""Tests for participants._windsurf_native -- native Windsurf state reader."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from participants._windsurf_native import (
    NativeWindsurfState,
    _read_cascade_sessions,
    _read_plans,
    _read_recent_folders,
    _read_spaces,
    _read_workflows,
    read_native_windsurf_state,
)


def _create_state_db(db_path: Path, items: dict[str, str]) -> None:
    """Create a minimal state.vscdb with given key-value pairs."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE IF NOT EXISTS ItemTable "
        "(key TEXT PRIMARY KEY, value TEXT)"
    )
    for key, value in items.items():
        conn.execute(
            "INSERT OR REPLACE INTO ItemTable (key, value) "
            "VALUES (?, ?)",
            (key, value),
        )
    conn.commit()
    conn.close()


class TestReadSpaces:
    def test_parses_spaces(self, tmp_path: Path):
        db_path = tmp_path / "state.vscdb"
        metadata = {
            "space-1": {"lastAccessed": 1000},
            "space-2": {"lastAccessed": 2000},
        }
        _create_state_db(
            db_path,
            {"windsurfSpace.metadata": json.dumps(metadata)},
        )
        spaces = _read_spaces(db_path)
        assert len(spaces) == 2
        names = {s.space_id for s in spaces}
        assert names == {"space-1", "space-2"}

    def test_missing_db(self, tmp_path: Path):
        db_path = tmp_path / "nonexistent.vscdb"
        assert _read_spaces(db_path) == []

    def test_malformed_json(self, tmp_path: Path):
        db_path = tmp_path / "state.vscdb"
        _create_state_db(
            db_path,
            {"windsurfSpace.metadata": "not json"},
        )
        assert _read_spaces(db_path) == []


class TestReadCascadeSessions:
    def test_extracts_from_escaped_value(self, tmp_path: Path):
        """Title is in a doubly-escaped JSON value field."""
        db_path = tmp_path / "state.vscdb"
        # This mirrors the real Windsurf format: `value` is a
        # JSON-encoded string inside the outer JSON.
        inner_value = json.dumps({"title": "MCP Registry"})
        editor_states = {
            "space-1": {
                "serializedGrid": {
                    "root": {
                        "data": {
                            "editors": [
                                {
                                    "id": (
                                        "workbench.input"
                                        ".cascadeEditor"
                                    ),
                                    "value": inner_value,
                                }
                            ]
                        }
                    }
                }
            }
        }
        _create_state_db(
            db_path,
            {
                "windsurfSpace.editorStates": json.dumps(
                    editor_states
                )
            },
        )
        sessions = _read_cascade_sessions(db_path)
        # Should find the title via escaped or unescaped pattern
        assert len(sessions) >= 1
        assert sessions[0].title == "MCP Registry"
        assert sessions[0].space_id == "space-1"

    def test_skips_generic_cascade_title(self, tmp_path: Path):
        db_path = tmp_path / "state.vscdb"
        inner_value = json.dumps({"title": "Cascade"})
        editor_states = {
            "space-1": {
                "serializedGrid": {
                    "root": {
                        "data": {
                            "editors": [
                                {
                                    "id": (
                                        "workbench.input"
                                        ".cascadeEditor"
                                    ),
                                    "value": inner_value,
                                }
                            ]
                        }
                    }
                }
            }
        }
        _create_state_db(
            db_path,
            {
                "windsurfSpace.editorStates": json.dumps(
                    editor_states
                )
            },
        )
        sessions = _read_cascade_sessions(db_path)
        assert len(sessions) == 0

    def test_empty_db(self, tmp_path: Path):
        db_path = tmp_path / "state.vscdb"
        _create_state_db(db_path, {})
        assert _read_cascade_sessions(db_path) == []


class TestReadRecentFolders:
    def test_extracts_folders(self, tmp_path: Path):
        db_path = tmp_path / "state.vscdb"
        data = {
            "entries": [
                {"folderUri": "file:///Users/test/project1"},
                {"folderUri": "file:///Users/test/project2"},
                {"fileUri": "file:///Users/test/some-file.py"},
            ]
        }
        _create_state_db(
            db_path,
            {"history.recentlyOpenedPathsList": json.dumps(data)},
        )
        folders = _read_recent_folders(db_path)
        assert folders == [
            "/Users/test/project1", "/Users/test/project2",
        ]


class TestReadPlans:
    def test_reads_plan_files(self, tmp_path: Path):
        plans_dir = tmp_path / ".windsurf" / "plans"
        plans_dir.mkdir(parents=True)
        (plans_dir / "feature.md").write_text(
            "# Build Auth\nImplement OAuth flow."
        )
        plans = _read_plans(tmp_path)
        assert len(plans) == 1
        assert plans[0].title == "Build Auth"
        assert plans[0].filename == "feature.md"

    def test_no_plans_dir(self, tmp_path: Path):
        assert _read_plans(tmp_path) == []

    def test_none_cwd(self):
        assert _read_plans(None) == []

    def test_title_falls_back_to_stem(self, tmp_path: Path):
        plans_dir = tmp_path / ".windsurf" / "plans"
        plans_dir.mkdir(parents=True)
        (plans_dir / "my-plan.md").write_text("No heading here")
        plans = _read_plans(tmp_path)
        assert plans[0].title == "my-plan"


class TestReadWorkflows:
    def test_reads_workflow_description(self, tmp_path: Path):
        wf_dir = tmp_path / ".windsurf" / "workflows"
        wf_dir.mkdir(parents=True)
        (wf_dir / "deploy.md").write_text(
            "---\ndescription: Deploy to prod\n---\nsteps"
        )
        workflows = _read_workflows(tmp_path)
        assert len(workflows) == 1
        assert workflows[0].description == "Deploy to prod"

    def test_no_frontmatter(self, tmp_path: Path):
        wf_dir = tmp_path / ".windsurf" / "workflows"
        wf_dir.mkdir(parents=True)
        (wf_dir / "test.md").write_text("Plain markdown")
        workflows = _read_workflows(tmp_path)
        assert len(workflows) == 1
        assert workflows[0].description == "test"


class TestReadNativeWindsurfState:
    def test_available_when_db_exists(self, tmp_path: Path):
        data_dir = tmp_path / "User"
        global_db = (
            data_dir / "globalStorage" / "state.vscdb"
        )
        _create_state_db(
            global_db,
            {"windsurfSpace.metadata": json.dumps({})},
        )
        state = read_native_windsurf_state(
            windsurf_data_dir=data_dir,
        )
        assert state.available is True
        assert state.global_db_present is True

    def test_unavailable_when_no_dir(self, tmp_path: Path):
        state = read_native_windsurf_state(
            windsurf_data_dir=tmp_path / "nonexistent",
        )
        assert state.available is False

    def test_includes_workspace_plans(self, tmp_path: Path):
        data_dir = tmp_path / "User"
        global_db = (
            data_dir / "globalStorage" / "state.vscdb"
        )
        _create_state_db(
            global_db,
            {"windsurfSpace.metadata": json.dumps({})},
        )
        cwd = tmp_path / "project"
        cwd.mkdir()
        plans_dir = cwd / ".windsurf" / "plans"
        plans_dir.mkdir(parents=True)
        (plans_dir / "foo.md").write_text("# Foo Plan\nStuff.")
        state = read_native_windsurf_state(
            windsurf_data_dir=data_dir, cwd=cwd,
        )
        assert len(state.plans) == 1
        assert state.plans[0].title == "Foo Plan"

    def test_to_dict(self):
        state = NativeWindsurfState(
            available=True, global_db_present=True,
        )
        d = state.to_dict()
        assert d["available"] is True
        assert "spaces_count" in d
        assert "errors" in d
