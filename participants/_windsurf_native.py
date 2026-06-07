"""Native Windsurf on-disk state reader.

Reads Windsurf's local state to provide enrichment for the Cascade participant:

- **Global state DB** (``state.vscdb``): Windsurf spaces, Cascade editor sessions,
  recent folders, plan metadata.
- **Workspace storage**: per-workspace ``state.vscdb`` files with editor state.
- **Workspace enrichment**: ``.windsurf/plans/`` and ``.windsurf/workflows/`` in cwd.

All operations are **read-only** and degrade gracefully if data is missing or
inaccessible. The reader never writes files, spawns processes, or makes network
calls.

Platform paths
--------------
- macOS: ``~/Library/Application Support/Windsurf/User/``
- Linux: ``~/.config/Windsurf/User/``
- Windows: ``%APPDATA%/Windsurf/User/``
"""

from __future__ import annotations

import json
import logging
import platform
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

# -- Constants -----------------------------------------------------------------

_GLOBAL_STORAGE_SUBPATH = "globalStorage"
_WORKSPACE_STORAGE_SUBPATH = "workspaceStorage"
_STATE_DB_NAME = "state.vscdb"

_KEY_SPACE_METADATA = "windsurfSpace.metadata"
_KEY_EDITOR_STATES = "windsurfSpace.editorStates"
_KEY_RECENT_PATHS = "history.recentlyOpenedPathsList"
_KEY_PLAN_INFO = "windsurf.settings.cachedPlanInfo"

_CASCADE_EDITOR_PATTERN = re.compile(
    r'"id"\s*:\s*"(?:workbench\.input\.cascadeEditor|cascadePanel)"'
)
_TITLE_PATTERN = re.compile(r'"title"\s*:\s*"([^"]+)"')
_ESCAPED_TITLE_PATTERN = re.compile(r'\\"title\\"\s*:\s*\\"([^\\]+)\\"')


# -- Data classes --------------------------------------------------------------


@dataclass
class WindsurfSpace:
    """A Windsurf space (tab group / workspace partition)."""

    space_id: str
    last_accessed: int | None = None


@dataclass
class CascadeSession:
    """A Cascade editor session extracted from serialized editor state."""

    title: str
    space_id: str | None = None


@dataclass
class WorkspaceAssociation:
    """Maps a workspace storage hash to a folder URI."""

    hash_id: str
    folder_uri: str


@dataclass
class WindsurfPlan:
    """A .windsurf/plans/ file."""

    filename: str
    title: str
    content_preview: str


@dataclass
class WindsurfWorkflow:
    """A .windsurf/workflows/ file."""

    filename: str
    description: str


@dataclass
class NativeWindsurfState:
    """Complete native Windsurf state snapshot."""

    available: bool = False
    global_db_present: bool = False
    spaces: list[WindsurfSpace] = field(default_factory=list)
    cascade_sessions: list[CascadeSession] = field(default_factory=list)
    recent_folders: list[str] = field(default_factory=list)
    workspaces: list[WorkspaceAssociation] = field(default_factory=list)
    plans: list[WindsurfPlan] = field(default_factory=list)
    workflows: list[WindsurfWorkflow] = field(default_factory=list)
    plan_info: dict[str, Any] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "global_db_present": self.global_db_present,
            "spaces_count": len(self.spaces),
            "cascade_sessions_count": len(self.cascade_sessions),
            "workspaces_count": len(self.workspaces),
            "recent_folders": self.recent_folders[:5],
            "plans_count": len(self.plans),
            "workflows_count": len(self.workflows),
            "errors": self.errors,
        }


# -- Platform resolution -------------------------------------------------------


def _default_windsurf_data_dir() -> Path | None:
    """Resolve the Windsurf User data directory for the current platform."""
    system = platform.system()
    if system == "Darwin":
        return Path.home() / "Library" / "Application Support" / "Windsurf" / "User"
    if system == "Linux":
        return Path.home() / ".config" / "Windsurf" / "User"
    if system == "Windows":
        appdata = Path.home() / "AppData" / "Roaming"
        return appdata / "Windsurf" / "User"
    return None


# -- SQLite helpers ------------------------------------------------------------


def _query_db(db_path: Path, key: str) -> str | None:
    """Read a single value from a Windsurf state.vscdb ItemTable."""
    if not db_path.is_file():
        return None
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        cursor = conn.execute(
            "SELECT value FROM ItemTable WHERE key = ? LIMIT 1", (key,)
        )
        row = cursor.fetchone()
        conn.close()
        return row[0] if row else None
    except (sqlite3.Error, OSError):
        return None


# -- Readers -------------------------------------------------------------------


def _read_spaces(db_path: Path) -> list[WindsurfSpace]:
    """Extract Windsurf space metadata from the global DB."""
    raw = _query_db(db_path, _KEY_SPACE_METADATA)
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []
    spaces: list[WindsurfSpace] = []
    if isinstance(data, dict):
        for space_id, meta in data.items():
            last_accessed = (
                meta.get("lastAccessed") if isinstance(meta, dict) else None
            )
            spaces.append(
                WindsurfSpace(space_id=space_id, last_accessed=last_accessed)
            )
    return spaces


def _read_cascade_sessions(db_path: Path) -> list[CascadeSession]:
    """Extract Cascade editor session titles from serialized grid data."""
    raw = _query_db(db_path, _KEY_EDITOR_STATES)
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []
    sessions: list[CascadeSession] = []
    if not isinstance(data, dict):
        return sessions
    for space_id, state in data.items():
        grid_str = json.dumps(state) if isinstance(state, dict) else str(state)
        for match in _CASCADE_EDITOR_PATTERN.finditer(grid_str):
            start = max(0, match.start() - 200)
            window = grid_str[start : match.end() + 200]
            # Try unescaped title first, then escaped (nested JSON)
            title_match = _TITLE_PATTERN.search(window)
            if title_match:
                title = title_match.group(1).strip()
                if title and title != "Cascade":
                    sessions.append(
                        CascadeSession(title=title, space_id=space_id)
                    )
                    continue
            esc_match = _ESCAPED_TITLE_PATTERN.search(window)
            if esc_match:
                title = esc_match.group(1).strip()
                if title and title != "Cascade":
                    sessions.append(
                        CascadeSession(title=title, space_id=space_id)
                    )
    return sessions


def _read_recent_folders(db_path: Path) -> list[str]:
    """Extract recently opened folder paths."""
    raw = _query_db(db_path, _KEY_RECENT_PATHS)
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []
    folders: list[str] = []
    for entry in data.get("entries") or []:
        uri = entry.get("folderUri") or ""
        if uri.startswith("file://"):
            path = uri[7:]
            if path:
                folders.append(path)
    return folders


def _read_plan_info(db_path: Path) -> dict[str, Any]:
    """Extract cached plan/subscription info."""
    raw = _query_db(db_path, _KEY_PLAN_INFO)
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}


def _read_workspace_associations(
    data_dir: Path,
) -> list[WorkspaceAssociation]:
    """Read workspace.json files to map storage hashes to folder URIs."""
    ws_storage = data_dir / _WORKSPACE_STORAGE_SUBPATH
    if not ws_storage.is_dir():
        return []
    associations: list[WorkspaceAssociation] = []
    try:
        for ws_dir in ws_storage.iterdir():
            if not ws_dir.is_dir():
                continue
            ws_json = ws_dir / "workspace.json"
            if not ws_json.is_file():
                continue
            try:
                ws_data = json.loads(
                    ws_json.read_text(encoding="utf-8")
                )
                folder = ws_data.get("folder") or ""
                if folder:
                    associations.append(
                        WorkspaceAssociation(
                            hash_id=ws_dir.name, folder_uri=folder
                        )
                    )
            except (json.JSONDecodeError, OSError):
                continue
    except OSError:
        pass
    return associations


def _read_plans(cwd: Path | None) -> list[WindsurfPlan]:
    """Read .windsurf/plans/ markdown files in the current workspace."""
    if cwd is None:
        return []
    plans_dir = cwd / ".windsurf" / "plans"
    if not plans_dir.is_dir():
        return []
    plans: list[WindsurfPlan] = []
    try:
        for md_file in sorted(plans_dir.glob("*.md")):
            try:
                text = md_file.read_text(encoding="utf-8")
                lines = text.strip().splitlines()
                title = ""
                for line in lines:
                    stripped = line.strip()
                    if stripped.startswith("#"):
                        title = stripped.lstrip("# ").strip()
                        break
                if not title:
                    title = md_file.stem
                preview = text[:500].strip()
                plans.append(
                    WindsurfPlan(
                        filename=md_file.name,
                        title=title,
                        content_preview=preview,
                    )
                )
            except OSError:
                continue
    except OSError:
        pass
    return plans


def _read_workflows(cwd: Path | None) -> list[WindsurfWorkflow]:
    """Read .windsurf/workflows/ markdown files in the current workspace."""
    if cwd is None:
        return []
    wf_dir = cwd / ".windsurf" / "workflows"
    if not wf_dir.is_dir():
        return []
    workflows: list[WindsurfWorkflow] = []
    try:
        for md_file in sorted(wf_dir.glob("*.md")):
            try:
                text = md_file.read_text(encoding="utf-8")
                description = ""
                if text.startswith("---"):
                    end = text.find("---", 3)
                    if end != -1:
                        yaml_block = text[3:end].strip()
                        try:
                            meta = yaml.safe_load(yaml_block)
                            if isinstance(meta, dict):
                                description = str(
                                    meta.get("description") or ""
                                )
                        except yaml.YAMLError:
                            pass
                if not description:
                    description = md_file.stem
                workflows.append(
                    WindsurfWorkflow(
                        filename=md_file.name, description=description
                    )
                )
            except OSError:
                continue
    except OSError:
        pass
    return workflows


# -- Public API ----------------------------------------------------------------


def read_native_windsurf_state(
    *,
    windsurf_data_dir: Path | None = None,
    cwd: Path | None = None,
) -> NativeWindsurfState:
    """Read Windsurf native on-disk state.

    Parameters
    ----------
    windsurf_data_dir : Path, optional
        Override the Windsurf User data directory.
    cwd : Path, optional
        Current workspace directory for plan/workflow enrichment.

    Returns
    -------
    NativeWindsurfState
        Snapshot of available state.
    """
    state = NativeWindsurfState()

    data_dir = windsurf_data_dir or _default_windsurf_data_dir()
    if data_dir is None or not data_dir.is_dir():
        return state

    global_db = data_dir / _GLOBAL_STORAGE_SUBPATH / _STATE_DB_NAME
    if not global_db.is_file():
        return state

    state.global_db_present = True
    state.available = True

    try:
        state.spaces = _read_spaces(global_db)
    except Exception as exc:
        state.errors.append(f"spaces: {exc}")

    try:
        state.cascade_sessions = _read_cascade_sessions(global_db)
    except Exception as exc:
        state.errors.append(f"cascade_sessions: {exc}")

    try:
        state.recent_folders = _read_recent_folders(global_db)
    except Exception as exc:
        state.errors.append(f"recent_folders: {exc}")

    try:
        state.plan_info = _read_plan_info(global_db)
    except Exception as exc:
        state.errors.append(f"plan_info: {exc}")

    try:
        state.workspaces = _read_workspace_associations(data_dir)
    except Exception as exc:
        state.errors.append(f"workspaces: {exc}")

    try:
        state.plans = _read_plans(cwd)
    except Exception as exc:
        state.errors.append(f"plans: {exc}")

    try:
        state.workflows = _read_workflows(cwd)
    except Exception as exc:
        state.errors.append(f"workflows: {exc}")

    return state
