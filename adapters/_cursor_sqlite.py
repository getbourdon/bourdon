from __future__ import annotations

import json
import os
import platform
import re
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_SENSITIVE_PATTERNS = (
    re.compile(r"\bapi[_-]?key\b", re.IGNORECASE),
    re.compile(r"\bapi[_-]?token\b", re.IGNORECASE),
    re.compile(r"\baccess[_-]?token\b", re.IGNORECASE),
    re.compile(r"\bbearer\s+token\b", re.IGNORECASE),
    re.compile(r"\bpassword\b", re.IGNORECASE),
    re.compile(r"\bsk_live_[A-Za-z0-9_]+\b"),
    re.compile(r"\bhf_[A-Za-z0-9_]{10,}\b", re.IGNORECASE),
)


def _scrub_text(value: str, limit: int = 256) -> str:
    """Redact credential-like content and cap length."""
    text = value.strip()
    if not text:
        return text
    if any(p.search(text) for p in _SENSITIVE_PATTERNS):
        return "[redacted credential-like text]"
    text = re.sub(r"https?://\S+", "[link]", text)
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


@dataclass(frozen=True)
class CursorSessionMemory:
    date: str
    cwd: str
    key_actions: tuple[str, ...]
    files_touched: tuple[str, ...]


@dataclass(frozen=True)
class CursorEntityMemory:
    name: str
    entity_type: str
    aliases: tuple[str, ...]
    summary: str
    tags: tuple[str, ...]


@dataclass(frozen=True)
class CursorSQLiteMemories:
    sessions: tuple[CursorSessionMemory, ...]
    entities: tuple[CursorEntityMemory, ...]
    databases_scanned: tuple[str, ...]
    malformed_records: int


def default_cursor_dir() -> Path | None:
    env_override = os.environ.get("CURSOR_DIR")
    if env_override:
        return Path(env_override)
    system = platform.system()
    if system == "Darwin":
        return Path.home() / "Library" / "Application Support" / "Cursor"
    if system == "Linux":
        return Path.home() / ".config" / "Cursor"
    if system == "Windows":
        appdata = os.environ.get("APPDATA")
        return Path(appdata) / "Cursor" if appdata else None
    return None


def extract_cursor_memories(cursor_dir: Path | None = None) -> CursorSQLiteMemories:
    root = cursor_dir or default_cursor_dir()
    if root is None or not root.is_dir():
        return CursorSQLiteMemories((), (), (), 0)

    sessions: list[CursorSessionMemory] = []
    entities_by_name: dict[str, CursorEntityMemory] = {}
    databases_scanned: list[str] = []
    malformed_records = 0

    for db_path in _iter_state_dbs(root):
        databases_scanned.append(str(db_path))
        records, bad_records = _read_item_table(db_path)
        malformed_records += bad_records
        for key, value in records:
            parsed_session = _record_to_session(key, value)
            if parsed_session is None:
                continue
            sessions.append(parsed_session)
            project_name = _project_name(parsed_session.cwd)
            if project_name:
                entities_by_name.setdefault(
                    project_name,
                    CursorEntityMemory(
                        name=_scrub_text(project_name, limit=120),
                        entity_type="project",
                        aliases=(parsed_session.cwd,),
                        summary=_scrub_text(
                            f"Cursor workspace inferred from {parsed_session.cwd}.",
                        ),
                        tags=("cursor", "workspace", "sqlite"),
                    ),
                )

    sessions.sort(key=lambda session: session.date, reverse=True)
    return CursorSQLiteMemories(
        sessions=tuple(sessions),
        entities=tuple(entities_by_name.values()),
        databases_scanned=tuple(databases_scanned),
        malformed_records=malformed_records,
    )


def _iter_state_dbs(root: Path) -> tuple[Path, ...]:
    candidates = [
        root / "state.vscdb",
        root / "User" / "globalStorage" / "state.vscdb",
    ]
    workspace_storage = root / "User" / "workspaceStorage"
    if workspace_storage.is_dir():
        for child in sorted(workspace_storage.iterdir()):
            candidates.append(child / "state.vscdb")
    return tuple(path for path in candidates if path.is_file())


def _read_item_table(db_path: Path) -> tuple[list[tuple[str, Any]], int]:
    records: list[tuple[str, Any]] = []
    malformed_records = 0
    with tempfile.TemporaryDirectory() as temp_dir:
        db_copy = Path(temp_dir) / "state.vscdb"
        shutil.copy2(db_path, db_copy)
        connection = sqlite3.connect(f"file:{db_copy}?mode=ro", uri=True)
        try:
            if not _has_item_table(connection):
                return records, malformed_records
            rows = connection.execute("SELECT key, value FROM ItemTable").fetchall()
        finally:
            connection.close()

    for key, raw_value in rows:
        try:
            value = json.loads(raw_value)
        except (TypeError, json.JSONDecodeError):
            malformed_records += 1
            continue
        records.append((str(key), value))
    return records, malformed_records


def _has_item_table(connection: sqlite3.Connection) -> bool:
    row = connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'ItemTable'"
    ).fetchone()
    return row is not None


def _record_to_session(key: str, value: Any) -> CursorSessionMemory | None:
    if not _looks_like_composer_record(key, value):
        return None
    if not isinstance(value, dict):
        return None

    cwd = _first_string(value, ("workspacePath", "workspace", "cwd", "folder", "rootPath"))
    if not cwd and isinstance(value.get("workspace"), dict):
        cwd = _first_string(value["workspace"], ("path", "folder", "cwd"))
    if not cwd:
        cwd = ""

    action = _first_string(value, ("title", "text", "prompt", "summary", "name"))
    if not action:
        action = _first_message_text(value.get("messages"))
    if not action:
        return None

    date = _date_from_record(value)
    files_touched = tuple(_extract_files(value))
    return CursorSessionMemory(
        date=date,
        cwd=cwd,
        key_actions=(_scrub_text(action),),
        files_touched=files_touched,
    )


def _looks_like_composer_record(key: str, value: Any) -> bool:
    lowered_key = key.lower()
    if "composer" in lowered_key or "aichat" in lowered_key or "chat" in lowered_key:
        return True
    if isinstance(value, dict):
        joined_keys = " ".join(value.keys()).lower()
        return "message" in joined_keys and ("workspace" in joined_keys or "file" in joined_keys)
    return False


def _first_string(value: dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        candidate = value.get(key)
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return ""


def _first_message_text(messages: Any) -> str:
    if not isinstance(messages, list):
        return ""
    for message in messages:
        if not isinstance(message, dict):
            continue
        content = message.get("content") or message.get("text")
        if isinstance(content, str) and content.strip():
            return content.strip()
    return ""


def _date_from_record(value: dict[str, Any]) -> str:
    for key in ("createdAt", "timestamp", "updatedAt", "lastUpdatedAt", "time"):
        candidate = value.get(key)
        parsed = _parse_date(candidate)
        if parsed:
            return parsed
    return datetime.now(timezone.utc).date().isoformat()


def _parse_date(value: Any) -> str:
    if isinstance(value, str):
        normalized = value.replace("Z", "+00:00")
        try:
            return datetime.fromisoformat(normalized).date().isoformat()
        except ValueError:
            return ""
    if isinstance(value, int | float):
        timestamp = value / 1000 if value > 10_000_000_000 else value
        try:
            return datetime.fromtimestamp(timestamp, timezone.utc).date().isoformat()
        except (OverflowError, OSError, ValueError):
            return ""
    return ""


def _extract_files(value: dict[str, Any]) -> list[str]:
    files: list[str] = []
    for key in ("files", "filePaths", "filesTouched", "references"):
        candidate = value.get(key)
        if isinstance(candidate, list):
            files.extend(_strings_from_list(candidate))
    for message in value.get("messages", []):
        if isinstance(message, dict):
            files.extend(_extract_files(message))
    return _dedupe(files)


def _strings_from_list(values: list[Any]) -> list[str]:
    strings: list[str] = []
    for item in values:
        if isinstance(item, str) and item.strip():
            strings.append(item.strip())
        elif isinstance(item, dict):
            path = _first_string(item, ("path", "file", "uri", "relativePath"))
            if path:
                strings.append(path)
    return strings


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    deduped: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            deduped.append(value)
    return deduped


def _project_name(cwd: str) -> str:
    if not cwd:
        return ""
    name = Path(cwd).name.strip()
    return name if name and name not in {".", "/"} else ""
