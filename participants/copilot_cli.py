"""Bourdon participant for GitHub Copilot CLI (``copilot`` terminal agent).

Copilot CLI stores session history in a SQLite database at
``~/.copilot/session-store.db``. This is distinct from:

- The VS Code Copilot Chat extension (workspace-scoped transcripts + memory-tool)
- The convention-file participant at ``~/.copilot-bourdon/memory.md``
- GitHub-embedded Copilot (no local state)

The schema (schema_version 4) contains:

    sessions          — id, cwd, repository, host_type, branch, summary, created_at, updated_at
    turns             — session_id, turn_index, user_message, assistant_response, timestamp
    checkpoints       — session_id, checkpoint_number, title, overview, history, ...
    session_files     — session_id, file_path, tool_name, turn_index
    session_refs      — session_id, ref_type (commit/pr/issue), ref_value
    dynamic_context_items — repository, branch, src, name, description, content, read_count, count

This participant reads the database read-only (via a temp copy to avoid WAL
locking), extracts sessions and entities, and emits a Bourdon L5 manifest.

Usage::

    from participants.copilot_cli import CopilotCliParticipant

    participant = CopilotCliParticipant()
    store = participant.discover()
    manifest = participant.export_l5()
"""

from __future__ import annotations

import logging
import os
import shutil
import socket
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from participants.base import (
    SPEC_VERSION,
    AgentInfo,
    AgentStore,
    Entity,
    HealthStatus,
    L5Manifest,
    ParticipantDiscoveryError,
    Session,
    Visibility,
    VisibilityPolicy,
    filter_for_federation,
)
from participants.codex import _safe_native_memory_text

logger = logging.getLogger(__name__)

AGENT_ID = "copilot-cli"
AGENT_TYPE = "code-assistant"
DISPLAY_NAME = "GitHub Copilot CLI"
ROLE_NARRATIVE = (
    "Terminal-native Copilot agent with full filesystem + tool access. "
    "Runs multi-turn sessions with checkpoints, file edits, and git "
    "operations. The CLI surface has the deepest tool integration of "
    "all Copilot surfaces — shell, grep, edit, git — and persists "
    "rich session history locally in SQLite."
)

DEFAULT_POLICY = VisibilityPolicy(
    default=Visibility.TEAM,
    private_tags=["personal", "financial", "credential", "health", "family", "legal"],
    team_tags=["copilot-cli", "copilot", "terminal", "workspace"],
)

_DB_FILENAME = "session-store.db"
_COPILOT_DIR_NAME = ".copilot"
_MAX_SUMMARY_CHARS = 260
_MAX_KEY_ACTIONS = 6
_MAX_KEY_ACTION_CHARS = 280


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


def default_copilot_cli_dir() -> Path:
    """Return the conventional ``~/.copilot/`` directory path.

    Respects the ``COPILOT_CLI_HOME`` environment variable override.
    """
    env = os.environ.get("COPILOT_CLI_HOME")
    if env:
        return Path(env)
    return Path.home() / _COPILOT_DIR_NAME


def default_copilot_cli_db_path(copilot_dir: Optional[Path] = None) -> Path:
    """Return the path to ``session-store.db``."""
    return (copilot_dir or default_copilot_cli_dir()) / _DB_FILENAME


# ---------------------------------------------------------------------------
# SQLite extraction (read-only temp copy)
# ---------------------------------------------------------------------------


def _safe_copy_db(db_path: Path) -> Optional[Path]:
    """Copy the database to a temp file for read-only access.

    Returns None if the source doesn't exist or copy fails.
    """
    if not db_path.is_file():
        return None
    try:
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        shutil.copy2(str(db_path), tmp.name)
        # Also copy WAL/SHM if present for consistency
        for ext in (".db-wal", ".db-shm"):
            wal = db_path.parent / (db_path.name + ext.replace(".db", ""))
            wal_actual = Path(str(db_path) + ext.replace(".db", ""))
            if wal_actual.is_file():
                shutil.copy2(str(wal_actual), tmp.name + ext.replace(".db", ""))
        return Path(tmp.name)
    except OSError as exc:
        logger.warning("CopilotCliParticipant: cannot copy db %s: %s", db_path, exc)
        return None


def _query_db(tmp_db: Path, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    """Execute a query and return rows as dicts. Never raises."""
    try:
        conn = sqlite3.connect(f"file:{tmp_db}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(sql, params).fetchall()
        result = [dict(r) for r in rows]
        conn.close()
        return result
    except (sqlite3.Error, OSError) as exc:
        logger.warning("CopilotCliParticipant: query failed: %s", exc)
        return []


def _cleanup_tmp(tmp_path: Optional[Path]) -> None:
    """Remove temp db and its WAL/SHM companions."""
    if tmp_path is None:
        return
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(tmp_path) + suffix)
        try:
            p.unlink(missing_ok=True)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Data extraction
# ---------------------------------------------------------------------------


def _extract_sessions(tmp_db: Path, since: Optional[str] = None) -> list[dict[str, Any]]:
    """Extract session records from the database."""
    sql = "SELECT id, cwd, repository, branch, summary, created_at, updated_at FROM sessions"
    params: tuple = ()
    if since:
        sql += " WHERE created_at >= ?"
        params = (since,)
    sql += " ORDER BY created_at DESC LIMIT 200"
    return _query_db(tmp_db, sql, params)


def _extract_session_files(tmp_db: Path, session_id: str) -> list[str]:
    """Extract files touched in a session."""
    rows = _query_db(
        tmp_db,
        "SELECT file_path FROM session_files WHERE session_id = ? LIMIT 20",
        (session_id,),
    )
    return [r["file_path"] for r in rows if r.get("file_path")]


def _extract_session_refs(tmp_db: Path, session_id: str) -> list[dict[str, str]]:
    """Extract refs (commits, PRs, issues) from a session."""
    return _query_db(
        tmp_db,
        "SELECT ref_type, ref_value FROM session_refs WHERE session_id = ?",
        (session_id,),
    )


def _extract_checkpoints(tmp_db: Path, session_id: str) -> list[dict[str, Any]]:
    """Extract checkpoints for a session."""
    return _query_db(
        tmp_db,
        "SELECT checkpoint_number, title, overview FROM checkpoints "
        "WHERE session_id = ? ORDER BY checkpoint_number",
        (session_id,),
    )


def _extract_dynamic_context(tmp_db: Path) -> list[dict[str, Any]]:
    """Extract dynamic context items (cross-session learned context)."""
    return _query_db(
        tmp_db,
        "SELECT repository, branch, src, name, description, content, read_count, count "
        "FROM dynamic_context_items ORDER BY count DESC LIMIT 50",
    )


def _extract_turn_count(tmp_db: Path, session_id: str) -> int:
    """Count turns in a session."""
    rows = _query_db(
        tmp_db,
        "SELECT COUNT(*) as cnt FROM turns WHERE session_id = ?",
        (session_id,),
    )
    return rows[0]["cnt"] if rows else 0


# ---------------------------------------------------------------------------
# Conversion to Bourdon types
# ---------------------------------------------------------------------------


def _bounded(value: str, limit: int) -> str:
    normalized = " ".join(value.split())
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 1].rstrip() + "…"


def _session_to_bourdon(
    raw: dict[str, Any],
    files: list[str],
    refs: list[dict[str, str]],
    checkpoints: list[dict[str, Any]],
    turn_count: int,
) -> Session:
    """Convert a raw session row to a Bourdon Session."""
    created = raw.get("created_at") or ""
    date_str = created[:10] if created else ""

    key_actions: list[str] = []
    if raw.get("summary"):
        key_actions.append(_bounded(_safe_native_memory_text(raw["summary"]), _MAX_KEY_ACTION_CHARS))
    for cp in checkpoints[:3]:
        if cp.get("title"):
            key_actions.append(_bounded(f"checkpoint: {_safe_native_memory_text(cp['title'])}", _MAX_KEY_ACTION_CHARS))
    if turn_count:
        key_actions.append(f"turns: {turn_count}")
    for ref in refs[:3]:
        ref_label = f"{ref['ref_type']}: {ref['ref_value']}"
        key_actions.append(_bounded(ref_label, _MAX_KEY_ACTION_CHARS))

    project_focus: list[str] = []
    if raw.get("repository"):
        project_focus.append(raw["repository"])

    return Session(
        date=date_str,
        cwd=raw.get("cwd") or None,
        project_focus=project_focus,
        key_actions=key_actions[:_MAX_KEY_ACTIONS],
        files_touched=files[:20],
        visibility=Visibility.TEAM,
    )


def _entities_from_sessions_and_context(
    sessions: list[dict[str, Any]],
    dynamic_context: list[dict[str, Any]],
) -> list[Entity]:
    """Build entities from repositories mentioned in sessions + dynamic context."""
    entities: dict[str, Entity] = {}

    # Repositories as project entities
    for raw in sessions:
        repo = raw.get("repository")
        if repo and repo not in entities:
            entities[repo] = Entity(
                name=repo,
                type="project",
                summary=f"Repository worked on via Copilot CLI.",
                last_touched=(raw.get("created_at") or "")[:10] or None,
                tags=["copilot-cli", "project"],
                visibility=Visibility.TEAM,
            )

    # Dynamic context items as knowledge entities
    for ctx in dynamic_context:
        name = ctx.get("name") or ""
        if not name or name in entities:
            continue
        desc = ctx.get("description") or ""
        content = ctx.get("content") or ""
        summary_text = desc or content
        if summary_text:
            summary_text = _bounded(_safe_native_memory_text(summary_text), _MAX_SUMMARY_CHARS)
        entities[name] = Entity(
            name=name,
            type="dynamic-context",
            summary=summary_text or None,
            last_touched=None,
            tags=["copilot-cli", "dynamic-context", ctx.get("src") or "unknown"],
            visibility=Visibility.TEAM,
        )

    return list(entities.values())


# ---------------------------------------------------------------------------
# Participant
# ---------------------------------------------------------------------------


class CopilotCliParticipant:
    """External participant for GitHub Copilot CLI (``~/.copilot/session-store.db``).

    Implements the :class:`~participants.base.BourdonParticipant` Protocol.
    Reads the SQLite database via a temp copy (never locks the live db).
    """

    agent_id = AGENT_ID
    agent_type = AGENT_TYPE
    display_name = DISPLAY_NAME

    @classmethod
    def default_native_path(cls, home: Path | None = None) -> Path:
        """Conventional ``~/.copilot`` dir used by the setup wizard's detection."""
        if home is not None:
            return home / _COPILOT_DIR_NAME
        return default_copilot_cli_dir()

    def __init__(self, copilot_dir: Optional[Path] = None) -> None:
        self._copilot_dir = copilot_dir

    @property
    def native_path(self) -> str:
        return str(self._copilot_dir or default_copilot_cli_dir())

    # -- Protocol surface -----------------------------------------------------

    def discover(self) -> AgentStore:
        """Locate the Copilot CLI session-store.db and return metadata."""
        db_path = default_copilot_cli_db_path(self._copilot_dir)
        if not db_path.is_file():
            raise ParticipantDiscoveryError(
                f"Copilot CLI session-store.db not found at {db_path}. "
                "The Copilot CLI agent must be run at least once to create "
                "this database."
            )
        return AgentStore(
            path=str(db_path.parent),
            version="schema-v4",
            metadata={"db_path": str(db_path), "db_size_bytes": db_path.stat().st_size},
        )

    def export_sessions(self, since: datetime, limit: int = 100) -> list[Session]:
        """Return recent Copilot CLI sessions newer than ``since``."""
        since_iso = since.astimezone(timezone.utc).isoformat()
        db_path = default_copilot_cli_db_path(self._copilot_dir)
        tmp = _safe_copy_db(db_path)
        if tmp is None:
            return []
        try:
            raw_sessions = _extract_sessions(tmp, since=since_iso)
            sessions: list[Session] = []
            for raw in raw_sessions[:limit]:
                sid = raw["id"]
                files = _extract_session_files(tmp, sid)
                refs = _extract_session_refs(tmp, sid)
                checkpoints = _extract_checkpoints(tmp, sid)
                turn_count = _extract_turn_count(tmp, sid)
                sessions.append(_session_to_bourdon(raw, files, refs, checkpoints, turn_count))
            return sessions
        finally:
            _cleanup_tmp(tmp)

    def export_l5(self, since: Optional[datetime] = None) -> L5Manifest:
        """Build the L5 manifest from Copilot CLI's session-store.db."""
        db_path = default_copilot_cli_db_path(self._copilot_dir)
        tmp = _safe_copy_db(db_path)
        if tmp is None:
            return self._empty_manifest()
        try:
            since_iso = since.astimezone(timezone.utc).isoformat() if since else None
            raw_sessions = _extract_sessions(tmp, since=since_iso)
            dynamic_context = _extract_dynamic_context(tmp)

            sessions: list[Session] = []
            for raw in raw_sessions[:100]:
                sid = raw["id"]
                files = _extract_session_files(tmp, sid)
                refs = _extract_session_refs(tmp, sid)
                checkpoints = _extract_checkpoints(tmp, sid)
                turn_count = _extract_turn_count(tmp, sid)
                sessions.append(_session_to_bourdon(raw, files, refs, checkpoints, turn_count))

            entities = _entities_from_sessions_and_context(raw_sessions, dynamic_context)
            visible_entities = filter_for_federation(entities, DEFAULT_POLICY)

            return L5Manifest(
                spec_version=SPEC_VERSION,
                agent=AgentInfo(
                    id=AGENT_ID,
                    type=AGENT_TYPE,
                    instance=socket.gethostname() or "unknown",
                    spec_version_compat=f">={SPEC_VERSION}",
                    role_narrative=ROLE_NARRATIVE,
                ),
                last_updated=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                capabilities=[
                    "terminal-agent",
                    "file-edit",
                    "shell-execution",
                    "git-operations",
                    "multi-turn-sessions",
                    "checkpoints",
                    "dynamic-context",
                ],
                recent_sessions=sessions,
                known_entities=visible_entities,
                visibility_policy=DEFAULT_POLICY,
            )
        finally:
            _cleanup_tmp(tmp)

    def health_check(self) -> HealthStatus:
        """Report ok / degraded / blocked. Never raises."""
        copilot_dir = self._copilot_dir or default_copilot_cli_dir()
        if not copilot_dir.is_dir():
            return HealthStatus(
                status="blocked",
                reason=f"Copilot CLI directory not found at {copilot_dir}.",
                details={"expected_path": str(copilot_dir)},
                proposed_fix=(
                    "Run the Copilot CLI agent (`copilot` or `gh copilot`) at "
                    "least once to create the session store."
                ),
            )
        db_path = default_copilot_cli_db_path(copilot_dir)
        if not db_path.is_file():
            return HealthStatus(
                status="blocked",
                reason=f"session-store.db not found at {db_path}.",
                details={"expected_db": str(db_path)},
                proposed_fix=(
                    "Run the Copilot CLI agent at least once. The session-store.db "
                    "is created on first use."
                ),
            )
        # Try to read it
        tmp = _safe_copy_db(db_path)
        if tmp is None:
            return HealthStatus(
                status="degraded",
                reason="Cannot copy session-store.db for reading.",
                details={"db_path": str(db_path)},
                proposed_fix="Check file permissions on the session-store.db.",
            )
        try:
            raw_sessions = _extract_sessions(tmp)
            dynamic_context = _extract_dynamic_context(tmp)
            total_turns = _query_db(tmp, "SELECT COUNT(*) as cnt FROM turns")
            return HealthStatus(
                status="ok",
                reason=None,
                details={
                    "db_path": str(db_path),
                    "db_size_bytes": db_path.stat().st_size,
                    "session_count": len(raw_sessions),
                    "total_turns": total_turns[0]["cnt"] if total_turns else 0,
                    "dynamic_context_items": len(dynamic_context),
                },
            )
        finally:
            _cleanup_tmp(tmp)

    # -- Internal -------------------------------------------------------------

    def _empty_manifest(self) -> L5Manifest:
        return L5Manifest(
            spec_version=SPEC_VERSION,
            agent=AgentInfo(
                id=AGENT_ID,
                type=AGENT_TYPE,
                instance=socket.gethostname() or "unknown",
                spec_version_compat=f">={SPEC_VERSION}",
                role_narrative=ROLE_NARRATIVE,
            ),
            last_updated=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            capabilities=[],
            recent_sessions=[],
            known_entities=[],
            visibility_policy=DEFAULT_POLICY,
        )
