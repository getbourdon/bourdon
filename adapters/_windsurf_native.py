"""Native Windsurf state reader for the Cascade adapter.

Reads Windsurf's on-disk state to enrich the convention-file-based Cascade
adapter with native session metadata, workspace associations, and active
plan/workflow context.

Data sources (all optional — graceful fallback when missing):

1. Global state DB:
   ``~/Library/Application Support/Windsurf/User/globalStorage/state.vscdb``
   - ``windsurfSpace.metadata`` — workspace/space last-accessed timestamps
   - ``windsurfSpace.editorStates`` — Cascade editor session titles
   - ``history.recentlyOpenedPathsList`` — recent folders/files
   - ``windsurf.acp.eventLog.index`` — ACP event log entries

2. Workspace state DBs:
   ``~/Library/Application Support/Windsurf/User/workspaceStorage/<id>/``
   - ``workspace.json`` — maps workspace ID to folder URI
   - ``state.vscdb`` → workspace-level cascade/chat state

3. Workspace-level enrichment (per open project):
   - ``.windsurf/plans/*.md`` — active plans
   - ``.windsurf/workflows/*.md`` — workflow definitions

This module is **read-only** — it never writes to Windsurf's state. It also
never reads auth tokens or credentials from the state DB (those keys are
skipped explicitly).
"""

from __future__ import annotations

import json
import logging
import platform
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# -- Platform-specific paths ---------------------------------------------------

_SENSITIVE_KEY_PREFIXES = ("secret://",)


def _default_windsurf_data_dir() -> Path:
    """Return the platform-appropriate Windsurf application data directory."""
    system = platform.system()
    if system == "Darwin":
        return Path.home() / "Library" / "Application Support" / "Windsurf"
    elif system == "Windows":
        appdata = Path.home() / "AppData" / "Roaming"
        return appdata / "Windsurf"
    else:
        # Linux / other: follow XDG convention
        xdg = Path.home() / ".config"
        return xdg / "Windsurf"


def _default_global_state_db() -> Path:
    return _default_windsurf_data_dir() / "User" / "globalStorage" / "state.vscdb"


def _default_workspace_storage_dir() -> Path:
    return _default_windsurf_data_dir() / "User" / "workspaceStorage"


# -- Data classes --------------------------------------------------------------


@dataclass
class WindsurfSpace:
    """A Windsurf space/workspace with last-accessed timestamp."""

    space_id: str
    last_accessed_ms: int

    @property
    def last_accessed(self) -> datetime:
        return datetime.fromtimestamp(
            self.last_accessed_ms / 1000, tz=timezone.utc
        )


@dataclass
class WindsurfCascadeSession:
    """A Cascade editor session extracted from editor state."""

    title: str
    resource_uri: str
    space_id: str | None = None


@dataclass
class WindsurfWorkspace:
    """A workspace association (workspace ID → folder path)."""

    workspace_id: str
    folder_path: str


@dataclass
class WindsurfPlan:
    """An active plan from .windsurf/plans/."""

    filename: str
    title: str
    content_preview: str
    path: str


@dataclass
class WindsurfWorkflow:
    """A workflow definition from .windsurf/workflows/."""

    filename: str
    description: str
    path: str


@dataclass
class NativeWindsurfState:
    """Aggregated native Windsurf state snapshot."""

    available: bool = False
    global_db_present: bool = False
    spaces: list[WindsurfSpace] = field(default_factory=list)
    cascade_sessions: list[WindsurfCascadeSession] = field(default_factory=list)
    workspaces: list[WindsurfWorkspace] = field(default_factory=list)
    recent_folders: list[str] = field(default_factory=list)
    recent_files: list[str] = field(default_factory=list)
    plans: list[WindsurfPlan] = field(default_factory=list)
    workflows: list[WindsurfWorkflow] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "global_db_present": self.global_db_present,
            "spaces_count": len(self.spaces),
            "cascade_sessions_count": len(self.cascade_sessions),
            "workspaces_count": len(self.workspaces),
            "recent_folders": self.recent_folders[:10],
            "plans_count": len(self.plans),
            "workflows_count": len(self.workflows),
            "errors": self.errors[:5],
        }


# -- SQLite helpers ------------------------------------------------------------


def _open_readonly(db_path: Path) -> sqlite3.Connection | None:
    """Open a SQLite DB in read-only mode. Returns None on failure."""
    if not db_path.is_file():
        return None
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        return conn
    except sqlite3.Error as exc:
        logger.debug("Failed to open %s: %s", db_path, exc)
        return None


def _get_json_value(conn: sqlite3.Connection, key: str) -> Any:
    """Read a JSON value from the ItemTable by key. Returns None on miss."""
    if any(key.startswith(prefix) for prefix in _SENSITIVE_KEY_PREFIXES):
        return None
    try:
        row = conn.execute(
            "SELECT value FROM ItemTable WHERE key = ?", (key,)
        ).fetchone()
    except sqlite3.Error:
        return None
    if row is None:
        return None
    try:
        return json.loads(row[0])
    except (json.JSONDecodeError, TypeError):
        return None


# -- Extraction functions ------------------------------------------------------


def _extract_spaces(conn: sqlite3.Connection) -> list[WindsurfSpace]:
    """Extract space metadata from windsurfSpace.metadata."""
    data = _get_json_value(conn, "windsurfSpace.metadata")
    if not isinstance(data, dict):
        return []
    spaces = []
    for space_id, meta in data.items():
        if not isinstance(meta, dict):
            continue
        last_accessed = meta.get("lastAccessed")
        if not isinstance(last_accessed, (int, float)):
            continue
        spaces.append(WindsurfSpace(space_id=space_id, last_accessed_ms=int(last_accessed)))
    spaces.sort(key=lambda s: s.last_accessed_ms, reverse=True)
    return spaces


def _extract_cascade_sessions(conn: sqlite3.Connection) -> list[WindsurfCascadeSession]:
    """Extract Cascade editor session titles from windsurfSpace.editorStates."""
    data = _get_json_value(conn, "windsurfSpace.editorStates")
    if not isinstance(data, dict):
        return []

    sessions: list[WindsurfCascadeSession] = []
    title_re = re.compile(r'"title"\s*:\s*"([^"]+)"')
    resource_re = re.compile(r'"resource"\s*:\s*"([^"]+)"')

    for space_id, state in data.items():
        if not isinstance(state, dict):
            continue
        grid = state.get("serializedGrid")
        if not isinstance(grid, dict):
            continue
        # Walk the grid tree to find cascade editor entries
        _walk_grid_for_sessions(grid, space_id, sessions, title_re, resource_re)

    return sessions


def _walk_grid_for_sessions(
    node: Any,
    space_id: str,
    sessions: list[WindsurfCascadeSession],
    title_re: re.Pattern[str],
    resource_re: re.Pattern[str],
) -> None:
    """Recursively walk the editor grid tree to find cascade sessions."""
    if not isinstance(node, dict):
        return

    # Leaf node with editors
    if node.get("type") == "leaf":
        leaf_data = node.get("data")
        if isinstance(leaf_data, dict):
            for editor in leaf_data.get("editors") or []:
                if not isinstance(editor, dict):
                    continue
                editor_id = editor.get("id") or ""
                if "cascade" not in editor_id.lower():
                    continue
                value = editor.get("value") or ""
                title_match = title_re.search(value)
                resource_match = resource_re.search(value)
                if title_match:
                    sessions.append(
                        WindsurfCascadeSession(
                            title=title_match.group(1),
                            resource_uri=resource_match.group(1) if resource_match else "",
                            space_id=space_id,
                        )
                    )
        return

    # Branch node — recurse into children
    if node.get("type") == "branch":
        for child in node.get("data") or []:
            _walk_grid_for_sessions(child, space_id, sessions, title_re, resource_re)
        return

    # Root wrapper
    root = node.get("root")
    if root is not None:
        _walk_grid_for_sessions(root, space_id, sessions, title_re, resource_re)


def _extract_recent_paths(
    conn: sqlite3.Connection,
) -> tuple[list[str], list[str]]:
    """Extract recent folders and files from history."""
    data = _get_json_value(conn, "history.recentlyOpenedPathsList")
    if not isinstance(data, dict):
        return [], []
    entries = data.get("entries")
    if not isinstance(entries, list):
        return [], []

    folders: list[str] = []
    files: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        folder_uri = entry.get("folderUri")
        if isinstance(folder_uri, str) and folder_uri.startswith("file://"):
            folders.append(folder_uri.replace("file://", ""))
        file_uri = entry.get("fileUri")
        if isinstance(file_uri, str) and file_uri.startswith("file://"):
            files.append(file_uri.replace("file://", ""))

    return folders, files


def _extract_workspaces(ws_dir: Path) -> list[WindsurfWorkspace]:
    """Scan workspace storage for workspace → folder mappings."""
    if not ws_dir.is_dir():
        return []
    workspaces: list[WindsurfWorkspace] = []
    try:
        for child in ws_dir.iterdir():
            if not child.is_dir():
                continue
            ws_json = child / "workspace.json"
            if not ws_json.is_file():
                continue
            try:
                data = json.loads(ws_json.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            folder = data.get("folder") or ""
            if folder.startswith("file://"):
                folder = folder.replace("file://", "")
            if folder:
                workspaces.append(
                    WindsurfWorkspace(workspace_id=child.name, folder_path=folder)
                )
    except OSError:
        pass
    return workspaces


# -- Workspace-level enrichment ------------------------------------------------


def _extract_plans(cwd: Path | None) -> list[WindsurfPlan]:
    """Read .windsurf/plans/*.md from the given workspace directory."""
    if cwd is None:
        return []
    plans_dir = cwd / ".windsurf" / "plans"
    if not plans_dir.is_dir():
        return []

    plans: list[WindsurfPlan] = []
    try:
        for md_file in sorted(plans_dir.glob("*.md")):
            if not md_file.is_file():
                continue
            try:
                text = md_file.read_text(encoding="utf-8")
            except OSError:
                continue
            title = _extract_md_title(text) or md_file.stem
            preview = text[:300].strip()
            plans.append(
                WindsurfPlan(
                    filename=md_file.name,
                    title=title,
                    content_preview=preview,
                    path=str(md_file),
                )
            )
    except OSError:
        pass
    return plans


def _extract_workflows(cwd: Path | None) -> list[WindsurfWorkflow]:
    """Read .windsurf/workflows/*.md from the given workspace directory."""
    if cwd is None:
        return []
    workflows_dir = cwd / ".windsurf" / "workflows"
    if not workflows_dir.is_dir():
        return []

    workflows: list[WindsurfWorkflow] = []
    try:
        for md_file in sorted(workflows_dir.glob("*.md")):
            if not md_file.is_file():
                continue
            try:
                text = md_file.read_text(encoding="utf-8")
            except OSError:
                continue
            description = _extract_workflow_description(text) or md_file.stem
            workflows.append(
                WindsurfWorkflow(
                    filename=md_file.name,
                    description=description,
                    path=str(md_file),
                )
            )
    except OSError:
        pass
    return workflows


def _extract_md_title(text: str) -> str:
    """Extract first # heading from markdown text."""
    for line in text.splitlines()[:10]:
        stripped = line.strip()
        if stripped.startswith("# "):
            return stripped[2:].strip()
    return ""


def _extract_workflow_description(text: str) -> str:
    """Extract description from YAML front-matter in a workflow file."""
    if not text.startswith("---"):
        return ""
    end = text.find("---", 3)
    if end == -1:
        return ""
    yaml_block = text[3:end].strip()
    # Simple extraction without importing yaml (avoid import-time cost)
    for line in yaml_block.splitlines():
        if line.startswith("description:"):
            return line[len("description:"):].strip().strip("'\"")
    return ""


# -- Public API ----------------------------------------------------------------


def read_native_windsurf_state(
    *,
    windsurf_data_dir: Path | None = None,
    cwd: Path | None = None,
) -> NativeWindsurfState:
    """Read the native Windsurf state from disk.

    Parameters
    ----------
    windsurf_data_dir : Path, optional
        Override the Windsurf application data directory.
    cwd : Path, optional
        Current working directory to probe for .windsurf/ workspace enrichment.

    Returns
    -------
    NativeWindsurfState
        Aggregated state snapshot. ``available=True`` if any data was extracted.
    """
    state = NativeWindsurfState()
    data_dir = windsurf_data_dir or _default_windsurf_data_dir()

    # 1. Global state DB
    global_db = data_dir / "User" / "globalStorage" / "state.vscdb"
    conn = _open_readonly(global_db)
    if conn is not None:
        state.global_db_present = True
        try:
            state.spaces = _extract_spaces(conn)
            state.cascade_sessions = _extract_cascade_sessions(conn)
            folders, files = _extract_recent_paths(conn)
            state.recent_folders = folders
            state.recent_files = files
        except Exception as exc:  # noqa: BLE001
            state.errors.append(f"global state read: {exc}")
        finally:
            conn.close()

    # 2. Workspace associations
    ws_dir = data_dir / "User" / "workspaceStorage"
    try:
        state.workspaces = _extract_workspaces(ws_dir)
    except Exception as exc:  # noqa: BLE001
        state.errors.append(f"workspace scan: {exc}")

    # 3. Workspace-level enrichment
    try:
        state.plans = _extract_plans(cwd)
    except Exception as exc:  # noqa: BLE001
        state.errors.append(f"plans read: {exc}")

    try:
        state.workflows = _extract_workflows(cwd)
    except Exception as exc:  # noqa: BLE001
        state.errors.append(f"workflows read: {exc}")

    # Mark as available if we got anything useful
    state.available = (
        state.global_db_present
        or bool(state.plans)
        or bool(state.workflows)
        or bool(state.workspaces)
    )

    return state


def inspect_native_windsurf(
    *,
    windsurf_data_dir: Path | None = None,
    cwd: Path | None = None,
) -> dict[str, Any]:
    """Return a diagnostic dict summarizing native Windsurf state availability.

    Designed for use in ``health_check()`` and ``bourdon cascade doctor``.
    """
    state = read_native_windsurf_state(
        windsurf_data_dir=windsurf_data_dir, cwd=cwd
    )
    return state.to_dict()
