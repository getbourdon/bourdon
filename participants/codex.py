"""Codex participant -- normalize Codex memories + sessions into L5 manifests."""

from __future__ import annotations

import json
import logging
import os
import re
import socket
import sqlite3
from collections import Counter
from collections.abc import Iterator
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from participants.base import (
    SPEC_VERSION,
    ParticipantDiscoveryError,
    AgentInfo,
    AgentStore,
    BourdonParticipant,
    Entity,
    HealthStatus,
    L5Manifest,
    Session,
    Visibility,
    VisibilityPolicy,
    filter_for_federation,
)
from core.l6_store import L6Store

logger = logging.getLogger(__name__)

AGENT_ID = "codex"
AGENT_TYPE = "code-assistant"
ROLE_NARRATIVE = (
    "Lead code-assistant. Organizes project code and executes prime code. "
    "Consults with Claude on solutions, problems, and issues via PR or the "
    "Slack #agents channel."
)
BOURDON_NATIVE_MEMORY_FILENAME = "bourdon_fallback.md"
BOURDON_MEMORY_MD_BEGIN = "<!-- BEGIN BOURDON FALLBACK MEMORY -->"
BOURDON_MEMORY_MD_END = "<!-- END BOURDON FALLBACK MEMORY -->"

DEFAULT_POLICY = VisibilityPolicy(
    default=Visibility.TEAM,
    private_tags=[
        "personal",
        "financial",
        "credential",
        "health",
        "family",
        "legal",
    ],
    team_tags=["codex-memory", "workspace"],
)

_GENERIC_PROJECT_NAMES = {
    "access",
    "add",
    "after",
    "align",
    "and",
    "android",
    "app",
    "apps",
    "api",
    "archive",
    "architecture",
    "assess",
    "assistant",
    "back",
    "backend",
    "backup",
    "backups",
    "billing",
    "bin",
    "build",
    "building",
    "bootstrap",
    "camerax",
    "chat",
    "check",
    "clean",
    "claude",
    "code",
    "codex",
    "commit",
    "compose",
    "config",
    "console",
    "contracts",
    "create",
    "current",
    "data",
    "decide",
    "desktop",
    "determine",
    "dev",
    "diagnose",
    "dns",
    "doc",
    "docs",
    "documents",
    "drive",
    "editor",
    "env",
    "exact",
    "fastify",
    "file",
    "files",
    "find",
    "finish",
    "fixtures",
    "folder",
    "folders",
    "for",
    "foundation",
    "fresh",
    "from",
    "full",
    "get",
    "git",
    "github",
    "google",
    "gradle",
    "handoff",
    "home",
    "improve",
    "implement",
    "install",
    "into",
    "ios",
    "inventory",
    "java",
    "jetpack",
    "kit",
    "kotlin",
    "layout",
    "letters",
    "local",
    "locate",
    "mac",
    "memory",
    "machine",
    "map",
    "model",
    "move",
    "migration",
    "monorepo",
    "native",
    "new",
    "new-project",
    "node",
    "notes",
    "npm",
    "off",
    "old",
    "onedrive",
    "parity",
    "play",
    "plan",
    "prepare",
    "preserve",
    "project",
    "projects",
    "push",
    "read",
    "react",
    "readiness",
    "recover",
    "recovery",
    "rebuild",
    "reinstall",
    "repo",
    "repos",
    "reset",
    "restore",
    "robocopy",
    "review",
    "root",
    "scaffold",
    "session",
    "sdk",
    "src",
    "state",
    "storage",
    "strategy",
    "studio",
    "swiftui",
    "the",
    "tooling",
    "triage",
    "update",
    "user",
    "users",
    "web",
    "with",
    "windows",
    "workspace",
    "save",
    "small",
    "merge",
    "inspect",
}

_MEMORY_SECTION_KEYS = (
    "task_groups",
    "task_titles",
    "preferences",
    "keywords",
    "descriptions",
)
_NATIVE_MEMORY_SENSITIVE_PATTERNS = (
    re.compile(r"\bapi[_-]?key\b", re.IGNORECASE),
    re.compile(r"\bapi[_-]?token\b", re.IGNORECASE),
    re.compile(r"\baccess[_-]?token\b", re.IGNORECASE),
    re.compile(r"\bbearer\s+token\b", re.IGNORECASE),
    re.compile(r"\bpassword\b", re.IGNORECASE),
    re.compile(r"\bsk_live_[A-Za-z0-9_]+\b"),
    re.compile(r"\bhf_[A-Za-z0-9_]{10,}\b", re.IGNORECASE),
)
_MAX_STRUCTURED_ROLLOUT_SCAN_BYTES = 1_000_000
_MAX_ROLLOUT_CONCEPT_SCAN_CHARS = 2_000_000
_MAX_L5_KEY_ACTION_CHARS = 300
_MAX_L5_TOPIC_NAME_CHARS = 120
_MAX_L5_PREFERENCE_NAME_CHARS = 80
_MAX_L5_PREFERENCE_SUMMARY_CHARS = 320
_FALLBACK_CONCEPT_PATTERNS = (
    (
        "Bourdon recognition first runtime layer",
        re.compile(r"\bbourdon recognition[- ]first runtime layer\b", re.IGNORECASE),
    ),
    ("Bourdon", re.compile(r"\bbourdon\b", re.IGNORECASE)),
    ("Continuo", re.compile(r"\bcontinuo\b", re.IGNORECASE)),
    (
        "runtime recognition",
        re.compile(r"\brun[- ]?time[- ]recognition\b", re.IGNORECASE),
    ),
    (
        "recognition first runtime layer",
        re.compile(r"\brecognition[- ]first runtime layer\b", re.IGNORECASE),
    ),
    (
        "recognition timing layer",
        re.compile(r"\brecognition timing layer\b", re.IGNORECASE),
    ),
    (
        "natural AI communication",
        re.compile(r"\bnatural ai communication\b", re.IGNORECASE),
    ),
    (
        "native Codex memory",
        re.compile(r"\bnative codex memor(?:y|ies)\b", re.IGNORECASE),
    ),
)


def _normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip())


def _safe_native_memory_text(value: str, limit: int = 180) -> str:
    text = _normalize_text(value)
    if any(pattern.search(text) for pattern in _NATIVE_MEMORY_SENSITIVE_PATTERNS):
        return "[redacted credential-like text]"
    text = re.sub(r"https?://\S+", "[link]", text)
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def _bounded_l5_text(value: str, limit: int) -> str:
    return _safe_native_memory_text(value, limit=limit)


def _split_csv(value: str) -> list[str]:
    return [
        part.strip().strip("`")
        for part in value.split(",")
        if part.strip().strip("`")
    ]


def _dedupe_preserve(
    items: list[str], key: callable | None = None
) -> list[str]:
    key = key or (lambda item: item)
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        marker = key(item)
        if marker in seen:
            continue
        seen.add(marker)
        result.append(item)
    return result


def _normalize_local_path(path_value: str | Path, os_name: str | None = None) -> Path:
    """Normalize Windows/WSL path spellings for the current Python runtime."""
    runtime_os_name = os.name if os_name is None else os_name
    raw = str(path_value).strip()
    if runtime_os_name == "nt":
        match = re.match(r"^/mnt/([A-Za-z])(?:/(.*))?$", raw)
        if match:
            drive = match.group(1).upper()
            rest = (match.group(2) or "").replace("/", "\\")
            return Path(f"{drive}:\\{rest}" if rest else f"{drive}:\\")
        return Path(raw)

    match = re.match(r"^([A-Za-z]):[\\/](.*)$", raw)
    if match:
        drive = match.group(1).lower()
        rest = match.group(2).replace("\\", "/")
        return Path(f"/mnt/{drive}/{rest}")
    return Path(raw)


def _path_compare_text(path_value: str | Path) -> str:
    raw = str(path_value).strip()
    if raw.startswith("\\\\?\\") or raw.startswith("//?/"):
        raw = raw[4:]
    return str(_normalize_local_path(raw)).replace("\\", "/").rstrip("/").lower()


def _is_codex_memory_cwd(cwd: str | None, codex_home: Path | None) -> bool:
    if not cwd or codex_home is None:
        return False
    cwd_text = _path_compare_text(cwd)
    memories_text = _path_compare_text(codex_home / "memories")
    return cwd_text == memories_text or cwd_text.startswith(f"{memories_text}/")


def _looks_like_codex_memories_path(cwd: str | None) -> bool:
    if not isinstance(cwd, str) or not cwd.strip():
        return False
    cwd_text = str(cwd).replace("\\", "/").rstrip("/").lower()
    return cwd_text.endswith("/.codex/memories") or "/.codex/memories/" in cwd_text


def _friendly_label(identifier: str) -> str:
    label = identifier.replace("_", " ").replace("-", " ").strip()
    if label.islower():
        return label.title()
    return label


def _empty_memory_sections() -> dict[str, list[str]]:
    return {key: [] for key in _MEMORY_SECTION_KEYS}


def _merge_visibility(
    current: Visibility | None, other: Visibility | None
) -> Visibility | None:
    order = {Visibility.PRIVATE: 0, Visibility.TEAM: 1, Visibility.PUBLIC: 2, None: 3}
    return other if order[other] < order[current] else current


def _merge_entity(into: Entity, other: Entity) -> None:
    if other.summary and not into.summary:
        into.summary = other.summary
    if other.last_touched and (
        into.last_touched is None or other.last_touched > into.last_touched
    ):
        into.last_touched = other.last_touched
    into.visibility = _merge_visibility(into.visibility, other.visibility)
    for alias in other.aliases:
        if alias not in into.aliases:
            into.aliases.append(alias)
    for tag in other.tags:
        if tag not in into.tags:
            into.tags.append(tag)


# -- Junk-entity filter (issue #78) --------------------------------------------
#
# The codex participant synthesizes entities from session titles and cwd-derived
# project keys. When a real signal is present, these become useful federation
# anchors. When it isn't, they become low-value noise: thread titles like
# "Project 2" or "memories", placeholder summaries from default templates,
# entities whose only content is the "Observed across N session(s)" boilerplate.
#
# These rules drop the unambiguous junk while keeping entities that carry any
# substantive content. The bar: an entity makes it into the L5 only if a
# downstream agent could plausibly use it as a recall anchor.

_NOISE_NAME_RE = re.compile(
    r"^(?:"
    r"project\s*\d+"
    r"|new\s+project(?:\s*\d+)?"
    r"|project\s+name"
    r"|untitled"
    r"|brief\s+description\s+of\s+the\s+project\.?"
    r")$",
    re.IGNORECASE,
)

_NOISE_NAME_SINGLE_WORDS = frozenset(
    {"memories", "memory", "notes", "note", "temp", "tmp", "test", "tests", "todo"}
)

_OBSERVED_ACROSS_SUMMARY_RE = re.compile(
    r"^observed across \d+ codex session\(s\)\.?$",
    re.IGNORECASE,
)

_CODEX_THREAD_SUMMARY_RE = re.compile(
    r"^codex (?:thread|memory topic): ",
    re.IGNORECASE,
)


def _is_junk_entity(entity: Entity) -> bool:
    """Return True if the entity should be dropped from the L5 export.

    Drops:

    - Names matching well-known placeholder patterns ("Project 2",
      "New Project", "untitled", "Brief description of the project").
    - Single-word noise names ("memories", "notes", "temp").
    - Entities whose summary is *only* the "Observed across N session(s)"
      placeholder AND that carry no other signal (no descriptive aliases,
      no descriptive tags beyond the canonical "codex-project" marker).
    - Entities whose summary is *only* the "Codex thread:" or "Codex memory
      topic:" placeholder AND whose name matches a noise pattern.

    Entities with substantive summary content are kept regardless. See #78.
    """
    name = (entity.name or "").strip()
    if not name:
        return True

    name_lc = name.lower()
    if _NOISE_NAME_RE.match(name):
        return True
    if name_lc in _NOISE_NAME_SINGLE_WORDS:
        return True
    if len(name) < 4 and name_lc not in {"nas", "ios", "css", "git", "ci"}:
        # Drop very short noise names but keep widely-recognized 3-letter acronyms.
        return True

    summary = (entity.summary or "").strip()
    if summary and _OBSERVED_ACROSS_SUMMARY_RE.match(summary):
        descriptive_tags = [
            tag for tag in entity.tags
            if tag and tag not in {"codex-project", "codex-thread", "codex-memory"}
        ]
        if not entity.aliases and not descriptive_tags:
            return True

    if summary and _CODEX_THREAD_SUMMARY_RE.match(summary):
        # "Codex thread:" / "Codex memory topic:" placeholders are only junk
        # when paired with a noise-shaped name (handled above) -- a real topic
        # name carrying this prefix is still useful as an anchor. So no
        # additional drop here; the name-based rules above catch the bad cases.
        pass

    return False


# -- Path resolution -----------------------------------------------------------


def _resolve_codex_home(base_home: Path | None = None) -> Path | None:
    """Locate `~/.codex/` (the primary Codex store)."""
    env_home = os.environ.get("CODEX_HOME")
    if env_home:
        candidate = _normalize_local_path(env_home)
        if candidate.is_dir():
            return candidate

    home = Path(base_home) if base_home else Path.home()
    candidate = home / ".codex"
    return candidate if candidate.is_dir() else None


def _resolve_codex_brain(base_home: Path | None = None) -> Path | None:
    """Locate the optional `~/codex-brain/` overlay."""
    home = Path(base_home) if base_home else Path.home()
    candidate = home / "codex-brain"
    return candidate if candidate.is_dir() else None


# -- Parsing helpers -----------------------------------------------------------


def _parse_session_index(path: Path, limit: int | None = None) -> list[dict]:
    """Read `session_index.jsonl` newest-first by `updated_at`."""
    if not path.is_file():
        return []
    entries: list[dict] = []
    try:
        with open(path, encoding="utf-8") as f:
            for line_num, line in enumerate(f, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError as exc:
                    logger.warning(
                        "Skipping malformed JSONL in %s:%d -- %s",
                        path,
                        line_num,
                        exc,
                    )
                    continue
                if isinstance(record, dict) and isinstance(record.get("id"), str):
                    entries.append(record)
    except OSError as exc:
        logger.warning("Failed to read %s: %s", path, exc)
        return []
    entries.sort(key=lambda record: record.get("updated_at", ""), reverse=True)
    return entries[:limit] if limit is not None else entries


def _find_rollout_file(codex_home: Path, session_id: str) -> Path | None:
    """Locate the rollout JSONL for a session id."""
    search_roots = [
        codex_home / "sessions",
        codex_home / "archived_sessions",
    ]
    matches: list[Path] = []
    for root in search_roots:
        if root.is_dir():
            matches.extend(root.rglob(f"rollout-*-{session_id}.jsonl"))
    return matches[0] if matches else None


def _empty_codex_state_report(codex_home: Path | None) -> dict[str, Any]:
    db_path = codex_home / "state_5.sqlite" if codex_home else None
    return {
        "path": str(db_path) if db_path else None,
        "present": bool(db_path and db_path.is_file()),
        "readable": False,
        "error": None,
        "threads": {
            "total": 0,
            "memory_enabled": 0,
            "active": 0,
            "archived": 0,
        },
        "schema": {
            "tables": [],
            "variant": "unknown",
            "stage1_outputs_table": False,
            "legacy_jobs_table": False,
            "agent_jobs_table": False,
            "agent_job_items_table": False,
            "stage1_counters_available": False,
        },
        "stage1_outputs": {
            "available": False,
            "source": "missing_table",
            "total": 0,
            "raw_memory": 0,
            "rollout_summary": 0,
        },
        "memory_stage1_jobs": {
            "available": False,
            "source": "missing_table",
            "total": 0,
            "by_status": {},
            "errors": [],
        },
        "agent_jobs": {
            "total": 0,
            "by_status": {},
            "items_total": 0,
            "items_by_status": {},
            "errors": [],
        },
    }


def _sqlite_table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_schema WHERE type='table' AND name=?",
        (table_name,),
    ).fetchone()
    return row is not None


def _sqlite_table_columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
    escaped_table = table_name.replace('"', '""')
    rows = conn.execute(f'PRAGMA table_info("{escaped_table}")').fetchall()
    return {str(row[1]) for row in rows}


def _sqlite_count(conn: sqlite3.Connection, query: str, params: tuple[Any, ...] = ()) -> int:
    row = conn.execute(query, params).fetchone()
    if row is None:
        return 0
    value = row[0]
    return int(value or 0)


def _sqlite_group_counts(
    conn: sqlite3.Connection,
    query: str,
    params: tuple[Any, ...] = (),
) -> dict[str, int]:
    rows = conn.execute(query, params).fetchall()
    return {str(status): int(count) for status, count in rows}


def _inspect_codex_state_db(codex_home: Path | None) -> dict[str, Any]:
    """Summarize Codex's local memory pipeline state without reading auth data."""
    report = _empty_codex_state_report(codex_home)
    db_path = codex_home / "state_5.sqlite" if codex_home else None
    if db_path is None or not db_path.is_file():
        report["error"] = "missing"
        return report

    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        report["error"] = str(exc)
        return report

    try:
        report["readable"] = True
        table_rows = conn.execute(
            "SELECT name FROM sqlite_schema WHERE type='table' ORDER BY name"
        ).fetchall()
        tables = {str(row[0]) for row in table_rows}
        report["schema"]["tables"] = sorted(tables)
        report["schema"]["stage1_outputs_table"] = "stage1_outputs" in tables
        report["schema"]["legacy_jobs_table"] = "jobs" in tables
        report["schema"]["agent_jobs_table"] = "agent_jobs" in tables
        report["schema"]["agent_job_items_table"] = "agent_job_items" in tables
        if "stage1_outputs" in tables or "jobs" in tables:
            report["schema"]["variant"] = "legacy_stage1"
            report["schema"]["stage1_counters_available"] = True
        elif "agent_jobs" in tables or "agent_job_items" in tables:
            report["schema"]["variant"] = "agent_jobs"
            report["stage1_outputs"]["source"] = "unavailable_new_schema"
            report["memory_stage1_jobs"]["source"] = "unavailable_new_schema"

        if _sqlite_table_exists(conn, "threads"):
            thread_columns = _sqlite_table_columns(conn, "threads")
            report["threads"]["total"] = _sqlite_count(conn, "SELECT count(*) FROM threads")
            if "memory_mode" in thread_columns:
                report["threads"]["memory_enabled"] = _sqlite_count(
                    conn,
                    "SELECT count(*) FROM threads WHERE memory_mode = ?",
                    ("enabled",),
                )
            if "archived" in thread_columns:
                report["threads"]["active"] = _sqlite_count(
                    conn,
                    "SELECT count(*) FROM threads WHERE archived = 0",
                )
                report["threads"]["archived"] = _sqlite_count(
                    conn,
                    "SELECT count(*) FROM threads WHERE archived = 1",
                )

        if _sqlite_table_exists(conn, "stage1_outputs"):
            report["stage1_outputs"]["available"] = True
            report["stage1_outputs"]["source"] = "legacy_stage1_outputs"
            stage1_columns = _sqlite_table_columns(conn, "stage1_outputs")
            report["stage1_outputs"]["total"] = _sqlite_count(
                conn,
                "SELECT count(*) FROM stage1_outputs",
            )
            if "raw_memory" in stage1_columns:
                report["stage1_outputs"]["raw_memory"] = _sqlite_count(
                    conn,
                    "SELECT count(raw_memory) FROM stage1_outputs",
                )
            if "rollout_summary" in stage1_columns:
                report["stage1_outputs"]["rollout_summary"] = _sqlite_count(
                    conn,
                    "SELECT count(rollout_summary) FROM stage1_outputs",
                )

        if _sqlite_table_exists(conn, "jobs"):
            report["memory_stage1_jobs"]["available"] = True
            report["memory_stage1_jobs"]["source"] = "legacy_jobs"
            job_columns = _sqlite_table_columns(conn, "jobs")
            if {"kind", "status"}.issubset(job_columns):
                by_status = _sqlite_group_counts(
                    conn,
                    """
                    SELECT status, count(*)
                    FROM jobs
                    WHERE kind = ?
                    GROUP BY status
                    ORDER BY status
                    """,
                    ("memory_stage1",),
                )
                report["memory_stage1_jobs"]["by_status"] = by_status
                report["memory_stage1_jobs"]["total"] = sum(by_status.values())
            if {
                "kind",
                "job_key",
                "status",
                "retry_remaining",
                "last_error",
            }.issubset(job_columns):
                error_rows = conn.execute(
                    """
                    SELECT job_key, status, retry_remaining, last_error
                    FROM jobs
                    WHERE kind = ? AND status != ?
                    ORDER BY job_key
                    LIMIT 20
                    """,
                    ("memory_stage1", "done"),
                ).fetchall()
                report["memory_stage1_jobs"]["errors"] = [
                    {
                        "job_key": str(job_key),
                        "status": str(status),
                        "retry_remaining": int(retry_remaining or 0),
                        "last_error": str(last_error or "")[:500],
                    }
                    for job_key, status, retry_remaining, last_error in error_rows
                ]
        if _sqlite_table_exists(conn, "agent_jobs"):
            agent_job_columns = _sqlite_table_columns(conn, "agent_jobs")
            report["agent_jobs"]["total"] = _sqlite_count(
                conn,
                "SELECT count(*) FROM agent_jobs",
            )
            if "status" in agent_job_columns:
                report["agent_jobs"]["by_status"] = _sqlite_group_counts(
                    conn,
                    """
                    SELECT status, count(*)
                    FROM agent_jobs
                    GROUP BY status
                    ORDER BY status
                    """,
                )
            if {"id", "name", "status", "last_error"}.issubset(agent_job_columns):
                error_rows = conn.execute(
                    """
                    SELECT id, name, status, last_error
                    FROM agent_jobs
                    WHERE status NOT IN ('done', 'completed', 'success', 'succeeded')
                    ORDER BY updated_at DESC
                    LIMIT 20
                    """
                ).fetchall()
                report["agent_jobs"]["errors"] = [
                    {
                        "job_id": str(job_id),
                        "name": str(name),
                        "status": str(status),
                        "last_error": str(last_error or "")[:500],
                    }
                    for job_id, name, status, last_error in error_rows
                ]
        if _sqlite_table_exists(conn, "agent_job_items"):
            item_columns = _sqlite_table_columns(conn, "agent_job_items")
            report["agent_jobs"]["items_total"] = _sqlite_count(
                conn,
                "SELECT count(*) FROM agent_job_items",
            )
            if "status" in item_columns:
                report["agent_jobs"]["items_by_status"] = _sqlite_group_counts(
                    conn,
                    """
                    SELECT status, count(*)
                    FROM agent_job_items
                    GROUP BY status
                    ORDER BY status
                    """,
                )
    except sqlite3.Error as exc:
        report["readable"] = False
        report["error"] = str(exc)
    finally:
        conn.close()

    return report


def _iter_rollout_records(
    rollout_path: Path,
    *,
    max_chars: int | None = None,
) -> Iterator[dict[str, Any]]:
    if not rollout_path.is_file():
        return
    chars_read = 0
    try:
        with open(rollout_path, encoding="utf-8") as f:
            for line in f:
                chars_read += len(line)
                if max_chars is not None and chars_read > max_chars:
                    break
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    record = json.loads(stripped)
                except json.JSONDecodeError:
                    continue
                if isinstance(record, dict):
                    yield record
    except OSError as exc:
        logger.warning("Failed to read rollout %s: %s", rollout_path, exc)


def _read_session_meta(rollout_path: Path) -> dict | None:
    """Return the first `session_meta.payload` dict from a rollout JSONL."""
    for index, record in enumerate(_iter_rollout_records(rollout_path)):
        if index >= 5:
            break
        if record.get("type") == "session_meta":
            payload = record.get("payload")
            return payload if isinstance(payload, dict) else None
    return None


def _path_from_state_value(path_value: Any) -> Path | None:
    if not isinstance(path_value, str) or not path_value.strip():
        return None
    path = _normalize_local_path(path_value)
    return path if path.is_file() else None


def _extract_files_from_patch(patch_text: str) -> list[str]:
    if not patch_text:
        return []
    files: list[str] = []
    patterns = (
        r"^\*\*\* (?:Update|Add|Delete) File: (.+)$",
        r"^\*\*\* Move to: (.+)$",
    )
    for line in patch_text.splitlines():
        for pattern in patterns:
            match = re.match(pattern, line.strip())
            if match:
                files.append(match.group(1).strip())
    return _dedupe_preserve(files, key=lambda item: item.lower())


def _extract_structured_files_touched(rollout_path: Path) -> list[str]:
    """Collect edited files from structured tool calls only."""
    files: list[str] = []
    for record in _iter_rollout_records(rollout_path):
        if record.get("type") != "response_item":
            continue
        payload = record.get("payload")
        if not isinstance(payload, dict):
            continue
        if payload.get("type") != "function_call":
            continue
        if payload.get("name") != "apply_patch":
            continue
        arguments = payload.get("arguments")
        if isinstance(arguments, str):
            files.extend(_extract_files_from_patch(arguments))
    return _dedupe_preserve(files, key=lambda item: item.lower())


def _extract_structured_files_touched_bounded(rollout_path: Path | None) -> list[str]:
    if rollout_path is None:
        return []
    try:
        if rollout_path.stat().st_size > _MAX_STRUCTURED_ROLLOUT_SCAN_BYTES:
            return []
    except OSError:
        return []
    return _extract_structured_files_touched(rollout_path)


def _extract_rollout_user_texts(rollout_path: Path, limit: int = 3) -> list[str]:
    texts: list[str] = []

    def append_text(value: Any) -> bool:
        if not isinstance(value, str) or not value.strip():
            return False
        texts.append(value)
        return len(texts) >= limit

    for record in _iter_rollout_records(rollout_path, max_chars=2_000_000):
        payload = record.get("payload")
        if not isinstance(payload, dict):
            continue

        record_type = record.get("type")
        if record_type == "user_input":
            if append_text(payload.get("text")):
                break
            continue

        if record_type == "event_msg" and payload.get("type") == "user_message":
            if append_text(payload.get("message")):
                break
            continue

        if record_type != "response_item":
            continue
        if payload.get("type") != "message" or payload.get("role") != "user":
            continue

        content = payload.get("content")
        if isinstance(content, str):
            if append_text(content):
                break
            continue
        if not isinstance(content, list):
            continue

        for item in content:
            if not isinstance(item, dict):
                continue
            if item.get("type") != "input_text":
                continue
            if append_text(item.get("text")):
                break
        if len(texts) >= limit:
            break

    return texts


def _extract_fallback_concepts(texts: list[str]) -> list[str]:
    combined = "\n".join(texts)
    concepts: list[str] = []
    if re.search(r"\bbourdon\b", combined, re.IGNORECASE) and re.search(
        r"\brecognition[- ]first runtime layer\b",
        combined,
        re.IGNORECASE,
    ):
        concepts.append("Bourdon recognition first runtime layer")
    pattern_concepts = [
        label
        for label, pattern in _FALLBACK_CONCEPT_PATTERNS
        if pattern.search(combined)
    ]
    return _dedupe_preserve(concepts + pattern_concepts, key=lambda item: item.lower())


def _extract_rollout_fallback_concepts(rollout_path: Path | None) -> list[str]:
    if rollout_path is None:
        return []
    try:
        with open(rollout_path, encoding="utf-8") as f:
            text = f.read(_MAX_ROLLOUT_CONCEPT_SCAN_CHARS)
    except OSError as exc:
        logger.warning("Failed to scan rollout concepts %s: %s", rollout_path, exc)
        return []
    return _extract_fallback_concepts([text])


def _extract_recovered_fallback_concepts(descriptions: list[str]) -> list[str]:
    concepts: list[str] = []
    prefix = "Recovered concept:"
    for description in descriptions:
        if not isinstance(description, str):
            continue
        if not description.startswith(prefix):
            continue
        concept = _normalize_text(description.removeprefix(prefix))
        if concept:
            concepts.append(concept)
    return _dedupe_preserve(concepts, key=lambda item: item.lower())


def _fallback_concept_aliases(concept_name: str) -> list[str]:
    normalized = concept_name.lower()
    if normalized == "bourdon recognition first runtime layer":
        return [
            "Bourdon",
            "Continuo",
            "runtime recognition",
            "runtime-recognition",
            "recognition first runtime layer",
            "recognition-first runtime layer",
        ]
    if normalized == "continuo":
        return ["Bourdon"]
    if normalized == "runtime recognition":
        return [
            "run time recognition",
            "run-time recognition",
            "runtime-recognition",
            "recognition first runtime layer",
            "Bourdon recognition first runtime layer",
        ]
    if normalized == "recognition first runtime layer":
        return ["recognition-first runtime layer"]
    if normalized == "native codex memory":
        return ["native Codex memories"]
    return []


def _memory_session_action(files_touched: list[str]) -> str:
    if not files_touched:
        return "Updated Codex memory artifacts"

    shown_files = [
        _bounded_l5_text(str(path), limit=90)
        for path in files_touched[:5]
        if str(path).strip()
    ]
    if not shown_files:
        return "Updated Codex memory artifacts"
    suffix = ", ..." if len(files_touched) > len(shown_files) else ""
    return _bounded_l5_text(
        f"Updated Codex memory artifacts: {', '.join(shown_files)}{suffix}",
        limit=_MAX_L5_KEY_ACTION_CHARS,
    )


def _session_action_from_record(
    record: dict[str, Any],
    codex_home: Path | None,
) -> str:
    if _is_codex_memory_cwd(record.get("cwd"), codex_home):
        return _memory_session_action(list(record.get("files_touched") or []))
    return _bounded_l5_text(
        str(record.get("thread_name") or "(untitled)"),
        limit=_MAX_L5_KEY_ACTION_CHARS,
    )


def _topic_name_from_record(
    record: dict[str, Any],
    codex_home: Path | None,
) -> str:
    if _is_codex_memory_cwd(record.get("cwd"), codex_home):
        return "Codex memory artifacts"
    return _bounded_l5_text(
        str(record.get("thread_name") or "(untitled)"),
        limit=_MAX_L5_TOPIC_NAME_CHARS,
    )


def _clean_preference_text(preference_text: str) -> str:
    text = _normalize_text(preference_text)
    text = re.sub(r"\s*\[Task\s+\d+\]\s*$", "", text, flags=re.IGNORECASE)
    return text.strip(" .")


def _canonical_preference_name(preference_text: str) -> str:
    clean_text = _clean_preference_text(preference_text)
    lower = clean_text.lower()

    if "backend-first" in lower or "backend first" in lower:
        return "backend-first delivery preference"
    if "cross-machine" in lower or (
        "commit" in lower
        and "push" in lower
        and any(token in lower for token in ("mac", "ios", "xcode"))
    ):
        return "cross-machine handoff workflow"
    if "machine switch" in lower and "branch" in lower and "checkpoint" in lower:
        return "machine-switch checkpoint workflow"
    if "old-to-new letter translation" in lower or "drive letter" in lower:
        return "drive-letter translation"
    if "handoff playbook" in lower:
        return "reusable handoff playbook"
    if "implemented architecture" in lower and "intended architecture" in lower:
        return "architecture stage reporting"
    if "full native ios" in lower or "full rewrite" in lower:
        return "full-platform migration planning"
    if "reinstall" in lower and any(
        token in lower for token in ("handoff", "recovery", "offline")
    ):
        return "reinstall recovery handoff"
    if "exact path" in lower or "exact paths" in lower:
        return "exact-path operational guidance"
    if re.search(r"\b(wait|stop)\b", lower):
        return "stop/wait interruption preference"
    if "git" in lower and "commit" in lower and "push" in lower:
        return "git checkpoint workflow"

    candidate = lower.split("->", 1)[1].strip() if "->" in lower else lower
    candidate = re.sub(r"^for\s+", "", candidate)
    candidate = re.sub(r"^future\s+", "", candidate)
    candidate = re.sub(r"^the user (?:wants|prefers|gave|says|likes)\s+", "", candidate)
    candidate = re.sub(r"^user (?:wants|prefers|gave|says|likes)\s+", "", candidate)
    candidate = re.sub(r"^prefer(?:s|red)?\s+", "", candidate)
    candidate = re.sub(r"^default to\s+", "", candidate)
    candidate = re.split(r"[:.;]", candidate, maxsplit=1)[0]
    candidate = re.sub(r"[^a-z0-9][\"'`][^a-z0-9]", " ", candidate)
    tokens = [
        token
        for token in re.findall(r"[a-z][a-z0-9-]{2,}", candidate)
        if token not in _GENERIC_PROJECT_NAMES
    ]
    if not tokens:
        return "codex preference"
    name = " ".join(tokens[:6])
    if not name.endswith("preference"):
        name = f"{name} preference"
    return _bounded_l5_text(name, limit=_MAX_L5_PREFERENCE_NAME_CHARS)


def _preference_entity(preference_text: str) -> Entity:
    clean_text = _clean_preference_text(preference_text)
    name = _canonical_preference_name(clean_text)
    tags = ["codex-preference"]
    if "workflow" in name or "handoff" in name:
        tags.append("workflow")
    return Entity(
        name=name,
        type="preference",
        summary=_bounded_l5_text(
            f"User preference: {clean_text}",
            limit=_MAX_L5_PREFERENCE_SUMMARY_CHARS,
        ),
        tags=tags,
        visibility=Visibility.TEAM,
    )


def _timestamp_to_iso_date(ts: str) -> str | None:
    """Extract `YYYY-MM-DD` from an ISO-ish timestamp string."""
    if not ts:
        return None
    try:
        normalized = ts.replace("Z", "+00:00")
        return datetime.fromisoformat(normalized).date().isoformat()
    except ValueError:
        if len(ts) >= 10 and ts[4] == "-" and ts[7] == "-":
            return ts[:10]
        return None


def _epoch_to_iso_date(value: Any) -> str | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number <= 0:
        return None
    if number > 10_000_000_000:
        number = number / 1000
    try:
        return datetime.fromtimestamp(number, tz=timezone.utc).date().isoformat()
    except (OSError, OverflowError, ValueError):
        return None


def _record_updated_at_date(record: dict[str, Any]) -> str | None:
    for key in ("updated_at_ms", "updated_at", "created_at_ms", "created_at"):
        if key not in record or record.get(key) is None:
            continue
        date_text = _epoch_to_iso_date(record.get(key))
        if date_text:
            return date_text
        date_text = _timestamp_to_iso_date(str(record.get(key)))
        if date_text:
            return date_text
    return None


def _path_basename(path_value: str | None) -> str | None:
    if not isinstance(path_value, str) or not path_value.strip():
        return None
    parts = [part for part in re.split(r"[\\/]+", path_value.strip()) if part]
    return parts[-1] if parts else None


def _project_key_from_cwd(cwd: str | None) -> str | None:
    basename = _path_basename(cwd)
    if not basename:
        return None
    key = basename.strip().lower()
    normalized_key = key.replace(" ", "-")
    if (
        key in _GENERIC_PROJECT_NAMES
        or normalized_key in _GENERIC_PROJECT_NAMES
        or key == Path.home().name.lower()
        or _looks_like_generated_workspace_name(key)
    ):
        return None
    return key


def _looks_like_generated_workspace_name(value: str) -> bool:
    return bool(
        re.match(r"^\d{4}[-_ ]\d{2}[-_ ]\d{2}", value)
        or value.startswith("202")
        or ("codex" in value and any(char.isdigit() for char in value))
    )


def _best_display_name(identifier: str, phrases: list[str]) -> str:
    pattern = re.compile(rf"\b{re.escape(identifier)}\b", re.IGNORECASE)
    candidates: list[str] = []
    for phrase in phrases:
        if not isinstance(phrase, str):
            continue
        for match in pattern.finditer(phrase):
            candidates.append(match.group(0))
    if candidates:
        counts = Counter(candidates)
        return sorted(
            counts.items(),
            key=lambda item: (-item[1], item[0].islower(), -len(item[0])),
        )[0][0]
    return _friendly_label(identifier)


def _extract_project_candidates(memory_data: dict[str, Any]) -> list[str]:
    counts: Counter[str] = Counter()
    stopwords = set(_GENERIC_PROJECT_NAMES)
    stopwords.add(Path.home().name.lower())

    for keyword in memory_data["keywords"]:
        tokens = re.findall(r"[A-Za-z][A-Za-z0-9-]{2,}", keyword)
        if not tokens:
            continue
        if len(tokens) == 1:
            candidate = tokens[0].strip("-").lower()
            if candidate in stopwords:
                continue
            counts[candidate] += 1
            continue

        for token in tokens:
            candidate = token.strip("-").lower()
            if candidate in stopwords:
                continue
            if token[0].isupper() or any(char.isupper() for char in token[1:]):
                counts[candidate] += 1

    for task_group in memory_data["task_groups"]:
        tokens = re.findall(r"[A-Za-z][A-Za-z0-9-]{2,}", task_group)
        if not tokens:
            continue
        lead_token = tokens[0].strip("-")
        candidate = lead_token.lower()
        if candidate in stopwords:
            continue
        if not (
            len(tokens) == 1
            or task_group == task_group.lower()
            or lead_token[0].isupper()
            or any(char.isupper() for char in lead_token[1:])
        ):
            continue
        counts[candidate] += 1

    return [
        candidate
        for candidate, count in counts.most_common()
        if count >= 2 and not _looks_like_generated_workspace_name(candidate)
    ]


def _pick_project_label(
    record: dict[str, Any],
    project_labels: dict[str, str],
    preferred_projects: list[str],
    thread_context: dict[str, list[str]] | None = None,
) -> str | None:
    def match_project(phrases: list[str]) -> str | None:
        for phrase in phrases:
            for project_key, project_label in project_labels.items():
                if re.search(
                    rf"\b{re.escape(project_key)}\b",
                    phrase,
                    re.IGNORECASE,
                ) or re.search(
                    rf"\b{re.escape(project_label)}\b",
                    phrase,
                    re.IGNORECASE,
                ):
                    return project_label
        return None

    project_key = _project_key_from_cwd(record.get("cwd"))
    if project_key and project_key in project_labels:
        return project_labels[project_key]

    if thread_context:
        thread_phrases = (
            thread_context["task_groups"]
            + thread_context["task_titles"]
            + thread_context["keywords"]
            + thread_context["descriptions"]
        )
        matched = match_project(thread_phrases)
        if matched:
            return matched

    thread_name = str(record.get("thread_name") or "")
    matched = match_project([thread_name])
    if matched:
        return matched

    if len(preferred_projects) == 1:
        return preferred_projects[0]
    return None


def _parse_memory_text(text: str) -> dict[str, list[str]]:
    """Extract task groups, task titles, preferences, keywords, and descriptions."""
    result = _empty_memory_sections()
    current_section: str | None = None

    for raw_line in text.splitlines():
        line = _normalize_text(raw_line)
        if not line:
            continue
        lower = line.lower()

        if line.startswith("# Task Group:"):
            result["task_groups"].append(
                _normalize_text(line.split(":", 1)[1])
            )
            current_section = None
            continue

        if re.match(r"^#{1,3}\s*Task\b", line):
            title = line.split(":", 1)[1] if ":" in line else re.sub(
                r"^#{1,3}\s*", "", line
            )
            result["task_titles"].append(_normalize_text(title))
            current_section = None
            continue

        if line.startswith("# ") and not lower.startswith("# raw memories"):
            result["task_groups"].append(_normalize_text(line[2:]))
            current_section = None
            continue

        if lower.startswith("task_group:"):
            result["task_groups"].append(
                _normalize_text(line.split(":", 1)[1]).replace("-", " ")
            )
            current_section = None
            continue

        if lower.startswith("task:"):
            result["task_titles"].append(
                _normalize_text(line.split(":", 1)[1]).replace("-", " ")
            )
            current_section = None
            continue

        if lower.startswith("description:"):
            result["descriptions"].append(_normalize_text(line.split(":", 1)[1]))
            continue

        if lower.startswith("keywords:"):
            result["keywords"].extend(_split_csv(line.split(":", 1)[1]))
            current_section = None
            continue

        if lower in {
            "## user preferences",
            "### user preferences",
            "## preference signals",
            "### preference signals",
        } or lower == "preference signals:":
            current_section = "preferences"
            continue

        if lower in {"## keywords", "### keywords"}:
            current_section = "keywords"
            continue

        if lower in {
            "## reusable knowledge",
            "### reusable knowledge",
        } or lower == "reusable knowledge:":
            current_section = "descriptions"
            continue

        if (
            lower in {
                "key steps:",
                "failures and how to do differently:",
                "references:",
                "rollout context:",
            }
            or re.match(r"^[a-z][a-z0-9 /()'`-]*:$", lower)
        ):
            current_section = None
            continue

        if line.startswith("- "):
            bullet = _normalize_text(line[2:])
            if current_section == "preferences":
                result["preferences"].append(bullet)
            elif current_section == "keywords":
                result["keywords"].extend(_split_csv(bullet))
            elif current_section == "descriptions":
                result["descriptions"].append(bullet)

    for key, values in result.items():
        result[key] = _dedupe_preserve(
            [value for value in values if value],
            key=lambda item: item.lower(),
        )
    return result


def _parse_raw_memories_threads(text: str) -> dict[str, dict[str, list[str]]]:
    thread_contexts: dict[str, dict[str, list[str]]] = {}
    current_thread_id: str | None = None
    current_lines: list[str] = []

    def flush_current() -> None:
        if not current_thread_id:
            return
        parsed = _parse_memory_text("\n".join(current_lines))
        if not any(parsed.values()):
            return
        existing = thread_contexts.setdefault(
            current_thread_id,
            _empty_memory_sections(),
        )
        _extend_memory_data(existing, parsed)

    for raw_line in text.splitlines():
        thread_match = re.match(r"^##\s+Thread\s+`([^`]+)`", raw_line.strip())
        if thread_match:
            flush_current()
            current_thread_id = _normalize_text(thread_match.group(1))
            current_lines = []
            continue
        if current_thread_id:
            current_lines.append(raw_line)

    flush_current()
    return thread_contexts


def _strip_bourdon_memory_md_section(existing_text: str) -> str:
    text = existing_text
    while BOURDON_MEMORY_MD_BEGIN in text:
        start = text.index(BOURDON_MEMORY_MD_BEGIN)
        end = text.find(BOURDON_MEMORY_MD_END, start)
        if end == -1:
            text = text[:start].rstrip()
            break

        end += len(BOURDON_MEMORY_MD_END)
        prefix = text[:start].rstrip()
        suffix = text[end:].lstrip()
        joined_text = f"{prefix}\n\n{suffix}"
        text = joined_text if prefix and suffix else prefix or suffix
    return text


def _extend_memory_data(
    into: dict[str, Any], parsed: dict[str, list[str]]
) -> None:
    for key in _MEMORY_SECTION_KEYS:
        into[key].extend(parsed.get(key, []))
        into[key] = _dedupe_preserve(
            into[key],
            key=lambda item: item.lower(),
        )


def _collect_memory_data(
    codex_home: Path | None, codex_brain: Path | None
) -> dict[str, Any]:
    data: dict[str, Any] = {
        **_empty_memory_sections(),
        "thread_contexts": {},
        "source_counts": {
            "memory_md": 0,
            "raw_memories": 0,
            "rollout_summaries": 0,
            "codex_brain_files": 0,
        },
    }

    memories_dir = codex_home / "memories" if codex_home else None
    if memories_dir and memories_dir.is_dir():
        memory_md = memories_dir / "MEMORY.md"
        raw_memories = memories_dir / "raw_memories.md"
        rollout_summaries = memories_dir / "rollout_summaries"

        if memory_md.is_file():
            data["source_counts"]["memory_md"] += 1
            memory_text = _strip_bourdon_memory_md_section(
                memory_md.read_text(encoding="utf-8")
            )
            _extend_memory_data(
                data,
                _parse_memory_text(memory_text),
            )
        if raw_memories.is_file():
            data["source_counts"]["raw_memories"] += 1
            raw_text = raw_memories.read_text(encoding="utf-8")
            _extend_memory_data(data, _parse_memory_text(raw_text))
            for thread_id, parsed in _parse_raw_memories_threads(raw_text).items():
                existing = data["thread_contexts"].setdefault(
                    thread_id,
                    _empty_memory_sections(),
                )
                _extend_memory_data(existing, parsed)
        if rollout_summaries.is_dir():
            for summary_file in sorted(rollout_summaries.glob("*.md")):
                data["source_counts"]["rollout_summaries"] += 1
                _extend_memory_data(
                    data,
                    _parse_memory_text(summary_file.read_text(encoding="utf-8")),
                )

    if codex_brain and codex_brain.is_dir():
        for path in sorted(codex_brain.rglob("*")):
            if not path.is_file() or path.stat().st_size == 0:
                continue
            if not any(
                str(path).lower().endswith(suffix)
                for suffix in (".md", ".txt", ".md.txt")
            ):
                continue
            data["source_counts"]["codex_brain_files"] += 1
            _extend_memory_data(
                data,
                _parse_memory_text(path.read_text(encoding="utf-8")),
            )

    return data


def _memory_item_count(memory_data: dict[str, Any]) -> int:
    total = sum(len(memory_data.get(key, [])) for key in _MEMORY_SECTION_KEYS)
    for thread_context in memory_data.get("thread_contexts", {}).values():
        if not isinstance(thread_context, dict):
            continue
        total += sum(len(thread_context.get(key, [])) for key in _MEMORY_SECTION_KEYS)
    return total


def _collect_rollout_fallback_memory_data(
    records: list[dict[str, Any]],
) -> dict[str, Any]:
    data: dict[str, Any] = {
        **_empty_memory_sections(),
        "thread_contexts": {},
        "source_counts": {
            "rollout_fallbacks": 0,
        },
    }

    for record in records:
        thread_data = _empty_memory_sections()
        thread_name = str(record.get("thread_name") or "").strip()
        if thread_name and thread_name != "(untitled)":
            if _looks_like_codex_memories_path(record.get("cwd")):
                thread_data["task_titles"].append("Codex memory artifacts")
            else:
                thread_data["task_titles"].append(
                    _bounded_l5_text(thread_name, limit=_MAX_L5_TOPIC_NAME_CHARS)
                )

        project_key = _project_key_from_cwd(record.get("cwd"))
        if project_key:
            project_label = _friendly_label(project_key)
            thread_data["keywords"].append(project_label)
            thread_data["task_groups"].append(f"{project_label} Codex session history")

        concepts = list(record.get("fallback_concepts") or [])
        if concepts:
            thread_data["keywords"].extend(concepts)
            for concept in concepts:
                thread_data["descriptions"].append(f"Recovered concept: {concept}")

        files_touched = list(record.get("files_touched") or [])
        if files_touched:
            shown_files = ", ".join(files_touched[:5])
            thread_data["descriptions"].append(
                f"Structured patch evidence touched: {shown_files}"
            )

        if not any(thread_data.values()):
            continue

        data["source_counts"]["rollout_fallbacks"] += 1
        _extend_memory_data(data, thread_data)
        thread_id = str(record.get("id") or "")
        if thread_id:
            existing = data["thread_contexts"].setdefault(
                thread_id,
                _empty_memory_sections(),
            )
            _extend_memory_data(existing, thread_data)

    return data


def _merge_memory_data(into: dict[str, Any], other: dict[str, Any]) -> None:
    _extend_memory_data(into, other)
    source_counts = into.setdefault("source_counts", {})
    for source_name, count in other.get("source_counts", {}).items():
        source_counts[source_name] = int(source_counts.get(source_name, 0)) + int(count)
    for thread_id, thread_data in other.get("thread_contexts", {}).items():
        existing = into.setdefault("thread_contexts", {}).setdefault(
            thread_id,
            _empty_memory_sections(),
        )
        _extend_memory_data(existing, thread_data)


def _collect_session_records(
    codex_home: Path | None, limit: int | None = None
) -> list[dict[str, Any]]:
    state_records = _collect_state_thread_records(codex_home, limit=limit)
    if state_records:
        latest_state_date = max(
            (str(record.get("date") or "") for record in state_records),
            default="",
        )
        records = _merge_session_records(
            state_records,
            _collect_unindexed_rollout_records(
                codex_home,
                excluded_ids={str(record.get("id") or "") for record in state_records},
                after_date=latest_state_date,
            ),
        )
        return _limit_session_records(records, limit)
    return _collect_session_index_records(codex_home, limit=limit)


def _collect_session_index_records(
    codex_home: Path | None, limit: int | None = None
) -> list[dict[str, Any]]:
    if codex_home is None:
        return []
    entries = _parse_session_index(codex_home / "session_index.jsonl", limit=limit)
    records: list[dict[str, Any]] = []
    for entry in entries:
        session_id = entry.get("id")
        if not isinstance(session_id, str):
            continue
        rollout = _find_rollout_file(codex_home, session_id)
        meta = _read_session_meta(rollout) if rollout else {}
        user_texts = _extract_rollout_user_texts(rollout) if rollout else []
        fallback_concepts = _dedupe_preserve(
            _extract_fallback_concepts(
                [str(entry.get("thread_name") or "")] + user_texts
            )
            + _extract_rollout_fallback_concepts(rollout),
            key=lambda item: item.lower(),
        )
        updated_at = entry.get("updated_at") or meta.get("timestamp") or ""
        session_date = _timestamp_to_iso_date(str(updated_at))
        if not session_date:
            continue
        records.append(
            {
                "id": session_id,
                "thread_name": _normalize_text(
                    str(entry.get("thread_name") or "(untitled)")
                ),
                "updated_at": str(updated_at),
                "date": session_date,
                "cwd": meta.get("cwd") if isinstance(meta.get("cwd"), str) else None,
                "model_provider": meta.get("model_provider")
                if isinstance(meta.get("model_provider"), str)
                else None,
                "cli_version": meta.get("cli_version")
                if isinstance(meta.get("cli_version"), str)
                else None,
                "originator": meta.get("originator")
                if isinstance(meta.get("originator"), str)
                else None,
                "source": meta.get("source")
                if isinstance(meta.get("source"), str)
                else None,
                "has_rollout": rollout is not None,
                "fallback_concepts": fallback_concepts,
                "files_touched": _extract_structured_files_touched_bounded(rollout),
            }
        )
    return records


def _session_record_sort_key(record: dict[str, Any]) -> tuple[str, float, str]:
    updated_at = str(record.get("updated_at") or "")
    try:
        parsed_number = float(updated_at)
    except ValueError:
        try:
            timestamp_value = datetime.fromisoformat(
                updated_at.replace("Z", "+00:00")
            ).timestamp()
        except ValueError:
            timestamp_value = 0.0
    else:
        timestamp_value = parsed_number
        if timestamp_value > 10_000_000_000:
            timestamp_value = timestamp_value / 1000
    return (str(record.get("date") or ""), timestamp_value, updated_at)


def _limit_session_records(
    records: list[dict[str, Any]], limit: int | None
) -> list[dict[str, Any]]:
    if limit is None:
        return records
    return records[:limit]


def _merge_session_records(
    primary: list[dict[str, Any]],
    supplemental: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    records_by_id: dict[str, dict[str, Any]] = {}
    for record in primary + supplemental:
        session_id = str(record.get("id") or "")
        if not session_id or session_id in records_by_id:
            continue
        records_by_id[session_id] = record
    return sorted(
        records_by_id.values(),
        key=_session_record_sort_key,
        reverse=True,
    )


def _thread_name_from_rollout_concepts(
    concepts: list[str], session_date: str
) -> str:
    if "Bourdon recognition first runtime layer" in concepts:
        return "Bourdon recognition first runtime layer"
    if concepts:
        shown = ", ".join(concepts[:3])
        return f"Codex session about {shown}"
    return f"Codex session {session_date}"


def _collect_unindexed_rollout_records(
    codex_home: Path | None,
    *,
    excluded_ids: set[str] | None = None,
    after_date: str | None = None,
) -> list[dict[str, Any]]:
    if codex_home is None:
        return []
    sessions_dir = codex_home / "sessions"
    if not sessions_dir.is_dir():
        return []

    records: list[dict[str, Any]] = []
    skipped_ids = excluded_ids or set()
    for rollout in sessions_dir.rglob("*.jsonl"):
        meta = _read_session_meta(rollout)
        if not isinstance(meta, dict):
            continue
        session_id = meta.get("id")
        if not isinstance(session_id, str) or not session_id:
            continue
        if session_id in skipped_ids:
            continue
        timestamp = str(meta.get("timestamp") or "")
        session_date = _timestamp_to_iso_date(timestamp)
        if not session_date:
            continue
        # Use < not <=: excluded_ids already prevents true duplicates, so
        # rollouts on the same calendar day as the latest state record but
        # with a different ID (e.g., a session created later that day not yet
        # indexed) must still be included. <= silently drops them.
        if after_date and session_date < after_date:
            continue

        concepts = _extract_rollout_fallback_concepts(rollout)
        thread_name = _thread_name_from_rollout_concepts(concepts, session_date)
        records.append(
            {
                "id": session_id,
                "thread_name": thread_name,
                "updated_at": timestamp,
                "date": session_date,
                "cwd": meta.get("cwd") if isinstance(meta.get("cwd"), str) else None,
                "model_provider": meta.get("model_provider")
                if isinstance(meta.get("model_provider"), str)
                else None,
                "cli_version": meta.get("cli_version")
                if isinstance(meta.get("cli_version"), str)
                else None,
                "originator": meta.get("originator")
                if isinstance(meta.get("originator"), str)
                else None,
                "source": meta.get("source")
                if isinstance(meta.get("source"), str)
                else None,
                "has_rollout": True,
                "fallback_concepts": concepts,
                "files_touched": [],
            }
        )
    return records


def _collect_state_thread_records(
    codex_home: Path | None, limit: int | None = None
) -> list[dict[str, Any]]:
    if codex_home is None:
        return []
    db_path = codex_home / "state_5.sqlite"
    if not db_path.is_file():
        return []
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
    except sqlite3.Error:
        return []

    try:
        if not _sqlite_table_exists(conn, "threads"):
            return []
        thread_columns = _sqlite_table_columns(conn, "threads")
        if "id" not in thread_columns:
            return []

        selectable = [
            column
            for column in (
                "id",
                "title",
                "first_user_message",
                "cwd",
                "rollout_path",
                "model_provider",
                "cli_version",
                "source",
                "memory_mode",
                "archived",
                "updated_at_ms",
                "updated_at",
                "created_at_ms",
                "created_at",
            )
            if column in thread_columns
        ]
        if "title" not in selectable and "first_user_message" not in selectable:
            return []

        order_column = next(
            (
                column
                for column in (
                    "updated_at_ms",
                    "updated_at",
                    "created_at_ms",
                    "created_at",
                )
                if column in thread_columns
            ),
            "id",
        )
        escaped_select = ", ".join(f'"{column}"' for column in selectable)
        query = f'SELECT {escaped_select} FROM threads ORDER BY "{order_column}" DESC'
        params: tuple[Any, ...] = ()
        if limit is not None:
            query += " LIMIT ?"
            params = (limit,)
        rows = [dict(row) for row in conn.execute(query, params).fetchall()]
    except sqlite3.Error:
        return []
    finally:
        conn.close()

    records: list[dict[str, Any]] = []
    for row in rows:
        session_id = str(row.get("id") or "")
        if not session_id:
            continue
        title = _normalize_text(str(row.get("title") or ""))
        first_user_message = _normalize_text(str(row.get("first_user_message") or ""))
        thread_name = title or first_user_message or "(untitled)"
        session_date = _record_updated_at_date(row)
        if not session_date:
            continue

        rollout = _path_from_state_value(row.get("rollout_path"))
        concepts = _dedupe_preserve(
            _extract_fallback_concepts([thread_name, first_user_message])
            + _extract_rollout_fallback_concepts(rollout),
            key=lambda item: item.lower(),
        )

        records.append(
            {
                "id": session_id,
                "thread_name": thread_name,
                "updated_at": str(
                    row.get("updated_at_ms")
                    or row.get("updated_at")
                    or row.get("created_at_ms")
                    or row.get("created_at")
                    or ""
                ),
                "date": session_date,
                "cwd": row.get("cwd") if isinstance(row.get("cwd"), str) else None,
                "model_provider": row.get("model_provider")
                if isinstance(row.get("model_provider"), str)
                else None,
                "cli_version": row.get("cli_version")
                if isinstance(row.get("cli_version"), str)
                else None,
                "originator": None,
                "source": row.get("source") if isinstance(row.get("source"), str) else None,
                "has_rollout": rollout is not None,
                "fallback_concepts": concepts,
                "files_touched": _extract_structured_files_touched_bounded(rollout),
            }
        )
    return records


def _inspect_codex_fallback_recall(
    codex_home: Path | None, codex_brain: Path | None
) -> dict[str, Any]:
    records = _collect_session_records(codex_home)
    memory_data = _collect_memory_data(codex_home, codex_brain)
    fallback_data = _collect_rollout_fallback_memory_data(records)

    distilled_items = _memory_item_count(memory_data)
    fallback_items = _memory_item_count(fallback_data)
    project_candidates = sorted(
        {
            _friendly_label(project_key)
            for project_key in (
                _project_key_from_cwd(record.get("cwd")) for record in records
            )
            if project_key
        }
    )
    active = distilled_items == 0 and fallback_items > 0
    status = "available" if fallback_items > 0 else "missing"
    if active:
        reason = "codex_distilled_memory_empty"
    elif fallback_items > 0:
        reason = "codex_distilled_memory_available"
    else:
        reason = "no_session_rollout_fallback"

    return {
        "status": status,
        "active": active,
        "reason": reason,
        "distilled_memory_items": distilled_items,
        "fallback_memory_items": fallback_items,
        "session_records": len(records),
        "rollout_records": sum(1 for record in records if record.get("has_rollout")),
        "file_evidence_sessions": sum(
            1 for record in records if record.get("files_touched")
        ),
        "project_candidates": project_candidates,
    }


def _default_codex_native_memory_path(codex_home: Path | None) -> Path:
    root = codex_home or (Path.home() / ".codex")
    return root / "memories" / BOURDON_NATIVE_MEMORY_FILENAME


def _default_codex_memory_md_path(codex_home: Path | None) -> Path:
    root = codex_home or (Path.home() / ".codex")
    return root / "memories" / "MEMORY.md"


def _merge_bourdon_memory_md_section(existing_text: str, bourdon_text: str) -> str:
    block = (
        f"{BOURDON_MEMORY_MD_BEGIN}\n"
        f"{bourdon_text.rstrip()}\n"
        f"{BOURDON_MEMORY_MD_END}\n"
    )
    if BOURDON_MEMORY_MD_BEGIN not in existing_text:
        prefix = existing_text.rstrip()
        return f"{prefix}\n\n{block}" if prefix else block

    start = existing_text.index(BOURDON_MEMORY_MD_BEGIN)
    end = existing_text.find(BOURDON_MEMORY_MD_END, start)
    if end == -1:
        prefix = existing_text.rstrip()
        return f"{prefix}\n\n{block}" if prefix else block

    end += len(BOURDON_MEMORY_MD_END)
    merged = existing_text[:start].rstrip() + "\n\n" + block
    suffix = existing_text[end:].lstrip()
    if suffix:
        merged += "\n" + suffix
    return merged


def _render_codex_native_memory_text(
    codex_home: Path | None,
    codex_brain: Path | None,
    max_sessions: int = 20,
) -> str:
    records = _collect_session_records(codex_home, limit=max_sessions)
    fallback = _inspect_codex_fallback_recall(codex_home, codex_brain)
    project_names = list(fallback.get("project_candidates") or [])
    concepts = _dedupe_preserve(
        [
            str(concept)
            for record in records
            for concept in list(record.get("fallback_concepts") or [])
            if concept
        ],
        key=lambda item: item.lower(),
    )

    lines = [
        "# Bourdon Fallback Memory",
        "",
        "Generated by Bourdon from Codex session and rollout metadata.",
        "This file is local, reversible, and avoids raw conversation transcripts.",
        "",
        "## Recovered Projects",
    ]
    if project_names:
        lines.extend(f"- {_safe_native_memory_text(name)}" for name in project_names)
    else:
        lines.append("- No project anchors recovered yet.")

    lines.extend(["", "## Recovered Concepts"])
    if concepts:
        lines.extend(f"- {_safe_native_memory_text(concept)}" for concept in concepts)
    else:
        lines.append("- No concept anchors recovered yet.")

    lines.extend(["", "## Recent Codex Threads"])
    if not records:
        lines.append("- No Codex session records found.")
    for record in records[:max_sessions]:
        title = _safe_native_memory_text(str(record.get("thread_name") or "(untitled)"))
        date_text = _safe_native_memory_text(str(record.get("date") or "unknown-date"))
        project_key = _project_key_from_cwd(record.get("cwd"))
        parts = [f"- {date_text}: {title}"]
        if project_key:
            parts.append(f"project={_safe_native_memory_text(_friendly_label(project_key))}")
        files_touched = [
            _safe_native_memory_text(str(path), limit=120)
            for path in list(record.get("files_touched") or [])[:5]
        ]
        if files_touched:
            parts.append(f"files={', '.join(files_touched)}")
        lines.append("; ".join(parts))

    lines.extend(
        [
            "",
            "## Recognition Hints",
            "- Treat recovered project names and thread titles as lightweight recall anchors.",
            "- Prefer Bourdon fallback recall only when Codex distilled memories are empty.",
            "- Do not treat this file as a raw transcript or source of secrets.",
            "",
        ]
    )
    return "\n".join(lines)


def _render_codex_federation_memory_text(
    library_path: Path | None = None,
    access_level: str = "team",
    max_entities: int = 100,
    max_sessions: int = 20,
    include_local: bool = False,
    codex_home: Path | None = None,
    codex_brain: Path | None = None,
) -> str:
    """Render a Codex-native memory file sourced from the federation library.

    Reads ``~/agent-library/agents/*.l5.yaml`` (or ``library_path``) via L6Store
    and produces project + concept anchors carrying source-agent attribution.
    When ``include_local`` is True the rendered text appends local Codex
    session/rollout content as a separate trailing section.

    Each entity row includes ``(via agent1, agent2)`` so Codex can tell which
    federated peer contributed a given anchor. This is the fresh-machine
    counterpart to ``_render_codex_native_memory_text`` -- which sources only
    from local Codex history and therefore renders an empty file on a fresh
    install.
    """
    store = L6Store(library_path=library_path)
    manifest = store.build_recognition_manifest(access_level=access_level)
    entities = list(manifest.get("known_entities") or [])
    sessions = list(manifest.get("recent_sessions") or [])

    entities.sort(key=lambda e: str(e.get("name") or "").lower())
    projects: list[dict[str, Any]] = []
    concepts: list[dict[str, Any]] = []
    for entity in entities:
        types = [str(t).lower() for t in entity.get("types") or []]
        primary = str(entity.get("type") or "topic").lower()
        if primary == "project" or "project" in types:
            projects.append(entity)
        else:
            concepts.append(entity)

    def _format_entity(entity: dict[str, Any]) -> str:
        name = _safe_native_memory_text(str(entity.get("name") or ""))
        sources = entity.get("source_agents") or []
        source_attr = (
            f" (via {', '.join(_safe_native_memory_text(str(s), limit=40) for s in sources)})"
            if sources
            else ""
        )
        summary = str(entity.get("summary") or "").strip()
        if summary:
            return f"- {name}{source_attr}: {_safe_native_memory_text(summary, limit=240)}"
        return f"- {name}{source_attr}"

    lines = [
        "# Bourdon Fallback Memory",
        "",
        "Generated by Bourdon from the federated L5 library across agents.",
        "This file is local, reversible, and avoids raw conversation transcripts.",
        "",
        "## Recovered Projects",
    ]
    if projects:
        lines.extend(_format_entity(p) for p in projects[:max_entities])
    else:
        lines.append("- No project anchors recovered yet.")

    lines.extend(["", "## Recovered Concepts"])
    if concepts:
        lines.extend(_format_entity(c) for c in concepts[:max_entities])
    else:
        lines.append("- No concept anchors recovered yet.")

    lines.extend(["", "## Recent Federation Sessions"])
    sessions.sort(key=lambda s: str(s.get("date") or ""), reverse=True)
    if sessions:
        for session in sessions[:max_sessions]:
            date_text = _safe_native_memory_text(str(session.get("date") or "unknown-date"))
            agent = _safe_native_memory_text(str(session.get("agent") or "?"), limit=40)
            focus = session.get("project_focus") or []
            focus_text = (
                f"; project={_safe_native_memory_text(', '.join(str(f) for f in focus), limit=120)}"
                if focus
                else ""
            )
            actions = session.get("key_actions") or []
            action_text = ""
            if actions:
                first = str(actions[0]).strip()
                if first:
                    action_text = f"; {_safe_native_memory_text(first, limit=160)}"
            lines.append(f"- {date_text} ({agent}){focus_text}{action_text}")
    else:
        lines.append("- No federation sessions recovered yet.")

    lines.extend(
        [
            "",
            "## Recognition Hints",
            "- Treat federation anchors as cross-agent recall, not authoritative state.",
            "- Source-attribution ``(via <agent>)`` identifies which peer contributed each anchor.",
            "- Prefer Bourdon fallback recall only when Codex distilled memories are empty.",
            "- Do not treat this file as a raw transcript or source of secrets.",
            "",
        ]
    )

    if include_local:
        local_text = _render_codex_native_memory_text(
            codex_home,
            codex_brain,
            max_sessions=max_sessions,
        )
        lines.extend(
            [
                "## Local Codex History",
                "",
                local_text,
            ]
        )

    return "\n".join(lines)


def _build_codex_native_memory_payload(
    codex_home: Path | None,
    codex_brain: Path | None,
    max_sessions: int = 20,
    from_library: bool = False,
    include_local: bool = False,
    library_path: Path | None = None,
    access_level: str = "team",
    max_entities: int = 100,
) -> dict[str, Any]:
    if from_library:
        text = _render_codex_federation_memory_text(
            library_path=library_path,
            access_level=access_level,
            max_entities=max_entities,
            max_sessions=max_sessions,
            include_local=include_local,
            codex_home=codex_home,
            codex_brain=codex_brain,
        )
    else:
        text = _render_codex_native_memory_text(
            codex_home,
            codex_brain,
            max_sessions=max_sessions,
        )
    return {
        "text": text,
        "bytes": len(text.encode("utf-8")),
        "fallback_recall": _inspect_codex_fallback_recall(codex_home, codex_brain),
    }


def _record_to_session(
    record: dict[str, Any],
    project_label: str | None = None,
    codex_home: Path | None = None,
) -> Session:
    project_focus = [project_label] if project_label else []
    return Session(
        date=record["date"],
        cwd=record.get("cwd"),
        project_focus=project_focus,
        key_actions=[_session_action_from_record(record, codex_home)],
        files_touched=list(record.get("files_touched") or []),
        visibility=Visibility.TEAM,
    )


# -- Participant class -------------------------------------------------------------


class CodexParticipant:
    """External participant for local Codex memories, sessions, and overlays."""

    agent_id = AGENT_ID
    agent_type = AGENT_TYPE

    def __init__(
        self,
        codex_home: Path | None = None,
        codex_brain: Path | None = None,
    ) -> None:
        if codex_home is not None:
            self._codex_home = _normalize_local_path(codex_home)
        else:
            self._codex_home = _resolve_codex_home()
        if codex_brain is not None:
            self._codex_brain = _normalize_local_path(codex_brain)
        else:
            self._codex_brain = _resolve_codex_brain()
        self.native_path = str(self._codex_home or (Path.home() / ".codex"))

    def discover(self) -> AgentStore:
        memories_dir = self._codex_home / "memories" if self._codex_home else None
        rollout_summaries = memories_dir / "rollout_summaries" if memories_dir else None
        sources = {
            "codex_home": str(self._codex_home) if self._codex_home else None,
            "state_db": None,
            "session_index": None,
            "sessions_dir": None,
            "memories_dir": str(memories_dir) if memories_dir and memories_dir.is_dir() else None,
            "memory_md": None,
            "raw_memories": None,
            "rollout_summaries_dir": (
                str(rollout_summaries) if rollout_summaries and rollout_summaries.is_dir() else None
            ),
            "codex_brain": str(self._codex_brain) if self._codex_brain else None,
        }
        if self._codex_home is not None:
            state_db = self._codex_home / "state_5.sqlite"
            session_index = self._codex_home / "session_index.jsonl"
            sessions_dir = self._codex_home / "sessions"
            if state_db.is_file():
                sources["state_db"] = str(state_db)
            if session_index.is_file():
                sources["session_index"] = str(session_index)
            if sessions_dir.is_dir():
                sources["sessions_dir"] = str(sessions_dir)
            if memories_dir and (memories_dir / "MEMORY.md").is_file():
                sources["memory_md"] = str(memories_dir / "MEMORY.md")
            if memories_dir and (memories_dir / "raw_memories.md").is_file():
                sources["raw_memories"] = str(memories_dir / "raw_memories.md")
        if not any(sources.values()):
            raise ParticipantDiscoveryError(
                "No Codex memory sources found. Expected ~/.codex/ and/or ~/codex-brain/."
            )
        return AgentStore(
            path=self.native_path,
            version="codex-memory-v2",
            metadata={"sources": sources},
        )

    def export_sessions(self, since: datetime, limit: int = 100) -> list[Session]:
        cutoff = since.date() if since else None
        sessions: list[Session] = []
        for record in _collect_session_records(self._codex_home, limit=limit):
            try:
                parsed = date.fromisoformat(record["date"])
            except ValueError:
                continue
            if cutoff and parsed < cutoff:
                continue
            project_key = _project_key_from_cwd(record.get("cwd"))
            project_label = _friendly_label(project_key) if project_key else None
            sessions.append(
                _record_to_session(
                    record,
                    project_label=project_label,
                    codex_home=self._codex_home,
                )
            )
        return sessions

    def export_l5(self, since: datetime | None = None) -> L5Manifest:
        store = self.discover()
        capabilities = sorted(
            key for key, value in store.metadata["sources"].items() if value
        )
        records = _collect_session_records(self._codex_home)
        memory_data = _collect_memory_data(self._codex_home, self._codex_brain)
        if _memory_item_count(memory_data) == 0:
            fallback_data = _collect_rollout_fallback_memory_data(records)
            if _memory_item_count(fallback_data) > 0:
                _merge_memory_data(memory_data, fallback_data)
        phrases = (
            memory_data["keywords"]
            + memory_data["task_groups"]
            + memory_data["task_titles"]
            + [record["thread_name"] for record in records]
        )

        project_labels: dict[str, str] = {}
        entity_map: dict[tuple[str, str], Entity] = {}
        preferred_project_keys = _extract_project_candidates(memory_data)

        project_counts = Counter(
            project_key
            for project_key in (
                _project_key_from_cwd(record.get("cwd")) for record in records
            )
            if project_key
        )
        for project_key, count in project_counts.items():
            label = _best_display_name(project_key, phrases)
            project_labels[project_key] = label
            descriptions = [
                text
                for text in memory_data["descriptions"] + memory_data["task_groups"]
                if project_key in text.lower()
            ]
            summary = (
                descriptions[0]
                if descriptions
                else f"Observed across {count} Codex session(s)."
            )
            entity = Entity(
                name=label,
                type="project",
                summary=summary,
                last_touched=max(
                    (
                        record["date"]
                        for record in records
                        if _project_key_from_cwd(record.get("cwd")) == project_key
                    ),
                    default=None,
                ),
                tags=["codex-project"],
                visibility=Visibility.TEAM,
            )
            entity_map[(entity.type or "topic", entity.name.lower())] = entity

        for project_key in preferred_project_keys:
            label = _best_display_name(project_key, phrases)
            project_labels[project_key] = label
            descriptions = [
                text
                for text in memory_data["descriptions"] + memory_data["task_groups"]
                if re.search(rf"\b{re.escape(project_key)}\b", text, re.IGNORECASE)
            ]
            entity = Entity(
                name=label,
                type="project",
                summary=(
                    descriptions[0]
                    if descriptions
                    else f"Named repeatedly across Codex memory as {label}."
                ),
                tags=["codex-project", "codex-memory"],
                visibility=Visibility.TEAM,
            )
            key = (entity.type or "topic", entity.name.lower())
            if key in entity_map:
                _merge_entity(entity_map[key], entity)
            else:
                entity_map[key] = entity

        preferred_projects = [
            entity.name
            for entity in entity_map.values()
            if entity.type == "project"
        ]

        cutoff = since.date() if since else None
        sessions: list[Session] = []
        for record in records:
            try:
                parsed = date.fromisoformat(record["date"])
            except ValueError:
                continue
            if cutoff and parsed < cutoff:
                continue
            project_label = _pick_project_label(
                record,
                project_labels,
                preferred_projects,
                thread_context=memory_data["thread_contexts"].get(record["id"]),
            )
            sessions.append(
                _record_to_session(
                    record,
                    project_label=project_label,
                    codex_home=self._codex_home,
                )
            )

            topic_name = _topic_name_from_record(record, self._codex_home)
            topic = Entity(
                name=topic_name,
                type="topic",
                summary=f"Codex thread: {topic_name}",
                last_touched=record["date"],
                tags=["codex-thread"],
                visibility=Visibility.TEAM,
            )
            key = (topic.type or "topic", topic.name.lower())
            if key in entity_map:
                _merge_entity(entity_map[key], topic)
            else:
                entity_map[key] = topic

        for topic_text in memory_data["task_groups"] + memory_data["task_titles"]:
            topic_name = _bounded_l5_text(topic_text, limit=_MAX_L5_TOPIC_NAME_CHARS)
            topic = Entity(
                name=topic_name,
                type="topic",
                summary=f"Codex memory topic: {topic_name}",
                tags=["codex-memory"],
                visibility=Visibility.TEAM,
            )
            key = (topic.type or "topic", topic.name.lower())
            if key in entity_map:
                _merge_entity(entity_map[key], topic)
            else:
                entity_map[key] = topic

        fallback_concept_dates: dict[str, list[str]] = {}
        for record in records:
            record_date = str(record.get("date") or "")
            for concept in list(record.get("fallback_concepts") or []):
                concept_key = str(concept).lower()
                fallback_concept_dates.setdefault(concept_key, []).append(record_date)

        record_concepts = [
            concept
            for record in records
            for concept in list(record.get("fallback_concepts") or [])
            if concept
        ]
        recovered_concepts = _extract_recovered_fallback_concepts(
            memory_data["descriptions"]
        )
        for concept_name in _dedupe_preserve(
            recovered_concepts + record_concepts,
            key=lambda item: item.lower(),
        ):
            concept = Entity(
                name=concept_name,
                type="topic",
                aliases=_fallback_concept_aliases(concept_name),
                summary=(
                    "Codex fallback concept recovered from rollout user prompts: "
                    f"{concept_name}."
                ),
                last_touched=max(
                    fallback_concept_dates.get(concept_name.lower(), []),
                    default=None,
                ),
                tags=["codex-memory", "codex-fallback-concept"],
                visibility=Visibility.TEAM,
            )
            key = (concept.type or "topic", concept.name.lower())
            if key in entity_map:
                _merge_entity(entity_map[key], concept)
            else:
                entity_map[key] = concept

        for preference_text in memory_data["preferences"]:
            preference = _preference_entity(preference_text)
            key = (preference.type or "topic", preference.name.lower())
            if key in entity_map:
                _merge_entity(entity_map[key], preference)
            else:
                entity_map[key] = preference

        # Issue #78: drop junk entities synthesized from session-title noise
        # and placeholder-only summaries before federation.
        entities = sorted(
            (
                entity
                for entity in entity_map.values()
                if not _is_junk_entity(entity)
            ),
            key=lambda entity: ((entity.type or "topic"), entity.name.lower()),
        )
        visible_entities = filter_for_federation(entities, DEFAULT_POLICY)

        return L5Manifest(
            spec_version=SPEC_VERSION,
            agent=AgentInfo(
                id=self.agent_id,
                type=self.agent_type,
                instance=socket.gethostname() or "unknown",
                spec_version_compat=f">={SPEC_VERSION}",
                role_narrative=ROLE_NARRATIVE,
            ),
            last_updated=datetime.now(timezone.utc).isoformat(),
            capabilities=capabilities,
            recent_sessions=sessions,
            known_entities=visible_entities,
            visibility_policy=DEFAULT_POLICY,
        )

    def health_check(self) -> HealthStatus:
        details: dict[str, Any] = {
            "codex_home": str(self._codex_home) if self._codex_home else "missing",
            "state_db": "missing",
            "session_index": "missing",
            "sessions_dir": "missing",
            "memories_dir": "missing",
            "memory_md": "missing",
            "raw_memories": "missing",
            "rollout_summaries_dir": "missing",
            "codex_brain": str(self._codex_brain) if self._codex_brain else "missing",
        }
        if self._codex_home is None:
            return HealthStatus(
                status="blocked",
                reason="~/.codex/ not found -- Codex CLI never used on this machine",
                details=details,
                proposed_fix=(
                    "Install OpenAI Codex (https://github.com/openai/codex) and "
                    "sign in once. Then `bourdon codex sync-native --from-library "
                    "--memory-md --write` to seed Codex's local recognition surface "
                    "from your federation library."
                ),
            )

        state_db = self._codex_home / "state_5.sqlite"
        session_index = self._codex_home / "session_index.jsonl"
        sessions_dir = self._codex_home / "sessions"
        memories_dir = self._codex_home / "memories"
        if state_db.is_file():
            details["state_db"] = str(state_db)
        if session_index.is_file():
            details["session_index"] = str(session_index)
        if sessions_dir.is_dir():
            details["sessions_dir"] = str(sessions_dir)
        if memories_dir.is_dir():
            details["memories_dir"] = str(memories_dir)
            if (memories_dir / "MEMORY.md").is_file():
                details["memory_md"] = str(memories_dir / "MEMORY.md")
            if (memories_dir / "raw_memories.md").is_file():
                details["raw_memories"] = str(memories_dir / "raw_memories.md")
            if (memories_dir / "rollout_summaries").is_dir():
                details["rollout_summaries_dir"] = str(memories_dir / "rollout_summaries")

        state_records = _collect_state_thread_records(self._codex_home, limit=1)
        if state_records:
            return HealthStatus(status="ok", details=details)

        if session_index.is_file() and sessions_dir.is_dir():
            return HealthStatus(status="ok", details=details)

        missing = [
            name
            for name, ok in (
                ("state_db", state_db.is_file()),
                ("session_index", session_index.is_file()),
                ("sessions_dir", sessions_dir.is_dir()),
            )
            if not ok
        ]
        return HealthStatus(
            status="degraded",
            reason=f"Missing Codex sub-sources: {', '.join(missing)}",
            details=details,
            proposed_fix=(
                "If Codex was just signed in but has no chat history, this is "
                "expected -- run `bourdon codex sync-native --from-library "
                "--memory-md --write` to seed recognition substrate from your "
                "federation library."
            ),
        )


_: BourdonParticipant = CodexParticipant()
