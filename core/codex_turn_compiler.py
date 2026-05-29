"""Turn-scoped recognition compiler for Codex.

This is now a thin wrapper over the agent-agnostic engine in
``core/turn_compiler.py``. The Codex-specific surface — native Stage 1 health
(read from ``state_5.sqlite``) and local thread metadata — lives in
``CodexSessionSource``. ``compile_codex_turn`` keeps its original signature and
byte-for-byte output; the shared engine is exercised by both this module and
``core/claude_turn_compiler.py``.

See ``docs/turn-compiler-architecture.md`` for the design.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from adapters.codex import _inspect_codex_state_db, _resolve_codex_home
from core.turn_compiler import (  # re-exported for backwards compatibility
    ACCESS_LEVELS,
    DEFAULT_MAX_CHARS,
    DEFAULT_MAX_ITEMS,
    DELIVERY_MODES,
    MAX_PROMPT_CHARS,
    STRATEGY,
    BriefHealth,
    BriefItem,
    RepoIdentity,
    TurnBrief,
    _Candidate,
    _safe_summary,
    compile_turn,
)

SCHEMA_VERSION = "codex-turn-brief/v1"
EXHAUSTED_PATHS = [
    "native_stage1_primary",
    "static_fallback_primary",
    "l5_export_only",
    "sync_native_only",
]

__all__ = [
    "compile_codex_turn",
    "CodexSessionSource",
    "SCHEMA_VERSION",
    "EXHAUSTED_PATHS",
    "TurnBrief",
    "BriefItem",
    "BriefHealth",
    "RepoIdentity",
    "STRATEGY",
    "ACCESS_LEVELS",
    "DELIVERY_MODES",
    "MAX_PROMPT_CHARS",
    "DEFAULT_MAX_ITEMS",
    "DEFAULT_MAX_CHARS",
]


class CodexSessionSource:
    """Codex-specific seam for the turn compiler.

    Native memory health comes from Codex's local ``state_5.sqlite`` memory
    pipeline (``memory_stage1`` jobs + ``stage1_outputs``); local records come
    from the ``threads`` table. All reads are read-only and avoid auth data.
    """

    agent_id = "codex"
    agent_display = "Codex"
    schema_version = SCHEMA_VERSION
    l5_source_label = "codex_l5"
    native_health_key = "native_stage1"
    native_health_noun = "native Stage 1"
    local_record_noun = "Codex thread"
    exhausted_paths = EXHAUSTED_PATHS

    def resolve_home(self, override: str | Path | None) -> Path | None:
        return Path(override) if override else _resolve_codex_home()

    def inspect_native(self, home: Path | None) -> dict[str, Any]:
        return _inspect_codex_state_db(home)

    def classify_native(self, report: dict[str, Any]) -> str:
        return _classify_native_stage1(report)

    def collect_local_records(self, home: Path | None, *, limit: int) -> list[_Candidate]:
        candidates: list[_Candidate] = []
        for record in _collect_lightweight_session_records(home, limit=limit):
            thread_name = str(record.get("thread_name") or "").strip()
            if not thread_name or thread_name == "(untitled)":
                continue
            source = "codex_rollout" if record.get("has_rollout") else "codex_state"
            candidates.append(
                _Candidate(
                    kind="thread",
                    name=_safe_summary(thread_name, limit=120),
                    summary=_safe_summary(_record_summary(record)),
                    source=source,
                    source_agents=["codex"],
                    date_text=str(record.get("date") or "") or None,
                    cwd=record.get("cwd") if isinstance(record.get("cwd"), str) else None,
                    files_touched=[
                        str(path)
                        for path in record.get("files_touched") or []
                        if isinstance(path, str) and path.strip()
                    ],
                    evidence=_record_evidence(record),
                )
            )
        return candidates

    def native_diagnostics(self, report: dict[str, Any]) -> dict[str, Any]:
        return {"stage1_jobs": _stage1_job_summary(report)}


def compile_codex_turn(
    prompt: str,
    *,
    cwd: str | Path | None = None,
    codex_home: str | Path | None = None,
    library_path: str | Path | None = None,
    access_level: str = "team",
    max_items: int = DEFAULT_MAX_ITEMS,
    max_chars: int = DEFAULT_MAX_CHARS,
    delivery: str = "all",
) -> TurnBrief:
    """Compile a turn-scoped Codex recognition brief.

    The function is read-only. It does not write native Codex files, mutate the
    federation library, run model calls, or depend on native Stage 1 output.
    """
    return compile_turn(
        prompt,
        source=CodexSessionSource(),
        cwd=cwd,
        home=codex_home,
        library_path=library_path,
        access_level=access_level,
        max_items=max_items,
        max_chars=max_chars,
        delivery=delivery,
    )


# -- Codex native surface readers ---------------------------------------------


def _classify_native_stage1(report: dict[str, Any]) -> str:
    if not report.get("present"):
        return "unknown"
    jobs = report.get("memory_stage1_jobs") or {}
    by_status = jobs.get("by_status") or {}
    errors = int(by_status.get("error") or 0)
    done = int(by_status.get("done") or 0)
    outputs = int((report.get("stage1_outputs") or {}).get("total") or 0)
    if errors > 0 and errors >= done:
        return "degraded"
    if done > 0 or outputs > 0:
        return "available"
    return "unknown"


def _collect_lightweight_session_records(
    codex_home: Path | None,
    *,
    limit: int,
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
        columns = _sqlite_table_columns(conn, "threads")
        if "id" not in columns:
            return []
        selectable = [
            column
            for column in (
                "id",
                "title",
                "first_user_message",
                "cwd",
                "rollout_path",
                "updated_at_ms",
                "updated_at",
                "created_at_ms",
                "created_at",
            )
            if column in columns
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
                if column in columns
            ),
            "id",
        )
        escaped_select = ", ".join(f'"{column}"' for column in selectable)
        query = f'SELECT {escaped_select} FROM threads ORDER BY "{order_column}" DESC LIMIT ?'
        rows = [dict(row) for row in conn.execute(query, (limit,)).fetchall()]
    except sqlite3.Error:
        return []
    finally:
        conn.close()

    records: list[dict[str, Any]] = []
    for row in rows:
        title = _safe_summary(str(row.get("title") or ""), limit=180)
        first_message = _safe_summary(str(row.get("first_user_message") or ""), limit=180)
        thread_name = title or first_message or "(untitled)"
        updated_at = str(
            row.get("updated_at_ms")
            or row.get("updated_at")
            or row.get("created_at_ms")
            or row.get("created_at")
            or ""
        )
        session_date = _date_from_state_timestamp(updated_at)
        if not session_date:
            continue
        records.append(
            {
                "id": str(row.get("id") or ""),
                "thread_name": thread_name,
                "updated_at": updated_at,
                "date": session_date,
                "cwd": row.get("cwd") if isinstance(row.get("cwd"), str) else None,
                "has_rollout": bool(row.get("rollout_path")),
                "files_touched": [],
            }
        )
    return records


def _sqlite_table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return row is not None


def _sqlite_table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f'PRAGMA table_info("{table}")')}


def _date_from_state_timestamp(value: str) -> str:
    text = value.strip()
    if not text:
        return ""
    try:
        number = float(text)
    except ValueError:
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return ""
    else:
        if number > 10_000_000_000:
            number = number / 1000
        parsed = datetime.fromtimestamp(number, tz=timezone.utc)
    return parsed.date().isoformat()


def _record_summary(record: dict[str, Any]) -> str:
    parts: list[str] = []
    if record.get("cwd"):
        parts.append(f"cwd {record['cwd']}")
    files = [str(path) for path in record.get("files_touched") or [] if isinstance(path, str)]
    if files:
        parts.append(f"touched {', '.join(files[:3])}")
    concepts = [
        str(item) for item in record.get("fallback_concepts") or [] if isinstance(item, str)
    ]
    if concepts:
        parts.append(f"concepts {', '.join(concepts[:3])}")
    return "; ".join(parts) if parts else "Recent Codex thread metadata."


def _record_evidence(record: dict[str, Any]) -> list[str]:
    evidence: list[str] = []
    if record.get("date"):
        evidence.append(f"thread date {record['date']}")
    if record.get("has_rollout"):
        evidence.append("rollout available")
    if record.get("cwd"):
        evidence.append(f"cwd {record['cwd']}")
    return evidence


def _stage1_job_summary(state_report: dict[str, Any]) -> dict[str, Any]:
    jobs = state_report.get("memory_stage1_jobs") or {}
    error_classes: dict[str, int] = {}
    for error in jobs.get("errors") or []:
        text = str(error.get("last_error") or "").lower()
        if "usage limit" in text:
            key = "usage_limit"
        elif "context window" in text or "ran out of room" in text:
            key = "context_window"
        elif text:
            key = "other"
        else:
            key = "unknown"
        error_classes[key] = error_classes.get(key, 0) + 1
    return {
        "total": int(jobs.get("total") or 0),
        "by_status": dict(jobs.get("by_status") or {}),
        "error_classes": dict(sorted(error_classes.items())),
    }
