"""Turn-scoped recognition compiler for Claude Code.

A thin wrapper over the agent-agnostic engine in ``core/turn_compiler.py``. The
Claude-specific surface lives in ``ClaudeSessionSource``:

- **native memory health** is read from the auto-memory index
  ``~/.claude/projects/<slug>/memory/MEMORY.md``. Claude has no async
  summarization stage like Codex; instead the analogous "native memory is
  degraded" signal is the documented MEMORY.md size-limit truncation (the
  loader drops index entries past a soft limit), which we detect by size.
- **local records** come from the live session transcripts
  ``~/.claude/projects/<slug>/*.jsonl`` — fresh work that has not yet been
  exported to the federation L5. Only the first few JSON lines of each
  transcript are read (bounded), never auth/credential files.

All reads are read-only. See ``docs/turn-compiler-architecture.md``.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.turn_compiler import (  # re-exported for backwards compatibility
    DEFAULT_MAX_CHARS,
    DEFAULT_MAX_ITEMS,
    TurnBrief,
    _Candidate,
    _safe_summary,
    compile_turn,
)

SCHEMA_VERSION = "claude-turn-brief/v1"
EXHAUSTED_PATHS = [
    "native_memory_primary",
    "static_fallback_primary",
    "l5_export_only",
    "session_jsonl_only",
]

# MEMORY.md is loaded into each Claude session as the auto-memory index. Past a
# soft size limit the loader truncates it and only part is loaded (observed in
# the wild as e.g. "MEMORY.md is 31.5KB (limit: 24.4KB) -- Only part of it was
# loaded."). That partial load is the Claude-side analogue of Codex's degraded
# native Stage 1, so we treat an oversized index as ``degraded``. Conservative
# and reviewer-tunable.
_MEMORY_MD_SOFT_LIMIT_BYTES = 25_000

# Read at most this many leading JSONL records per transcript when extracting a
# thread name + cwd. Keeps the probe bounded and avoids scanning whole sessions.
_MAX_JSONL_HEAD_LINES = 5

__all__ = [
    "compile_claude_turn",
    "ClaudeSessionSource",
    "SCHEMA_VERSION",
    "EXHAUSTED_PATHS",
]


class ClaudeSessionSource:
    """Claude Code-specific seam for the turn compiler."""

    agent_id = "claude-code"
    agent_display = "Claude"
    schema_version = SCHEMA_VERSION
    l5_source_label = "claude_l5"
    native_health_key = "native_memory"
    native_health_noun = "native memory"
    local_record_noun = "Claude session"
    exhausted_paths = EXHAUSTED_PATHS

    def resolve_home(self, override: str | Path | None) -> Path | None:
        """Resolve the ``~/.claude/projects`` base (override-able for tests)."""
        if override:
            return Path(override)
        base = Path.home() / ".claude" / "projects"
        return base if base.is_dir() else None

    def inspect_native(self, home: Path | None) -> dict[str, Any]:
        report: dict[str, Any] = {
            "present": False,
            "path": None,
            "size_bytes": 0,
            "over_limit": False,
            "projects_base": str(home) if home else None,
        }
        memory_md = _find_memory_md(home)
        if memory_md is None:
            return report
        report["present"] = True
        report["path"] = str(memory_md)
        try:
            size = memory_md.stat().st_size
        except OSError:
            return report
        report["size_bytes"] = int(size)
        report["over_limit"] = size > _MEMORY_MD_SOFT_LIMIT_BYTES
        return report

    def classify_native(self, report: dict[str, Any]) -> str:
        if not report.get("present"):
            return "unknown"
        if report.get("over_limit"):
            return "degraded"
        return "available"

    def collect_local_records(self, home: Path | None, *, limit: int) -> list[_Candidate]:
        if home is None:
            return []
        try:
            if not home.is_dir():
                return []
        except OSError:
            return []

        entries: list[tuple[float, str, Path]] = []
        try:
            for project_dir in home.iterdir():
                if not project_dir.is_dir():
                    continue
                slug = project_dir.name
                for transcript in project_dir.glob("*.jsonl"):
                    try:
                        mtime = transcript.stat().st_mtime
                    except OSError:
                        continue
                    entries.append((mtime, slug, transcript))
        except (OSError, PermissionError):
            return []

        entries.sort(key=lambda entry: entry[0], reverse=True)

        candidates: list[_Candidate] = []
        for mtime, slug, transcript in entries[:limit]:
            name, cwd = _scan_jsonl_head(transcript)
            thread_name = _safe_summary(str(name or "").strip(), limit=120)
            if not thread_name:
                continue
            cwd_value = cwd or _decode_claude_slug(slug)
            date_text = _date_from_mtime(mtime)
            candidates.append(
                _Candidate(
                    kind="thread",
                    name=thread_name,
                    summary=_safe_summary(_record_summary(cwd_value)),
                    source="claude_session",
                    source_agents=["claude-code"],
                    date_text=date_text or None,
                    cwd=cwd_value,
                    files_touched=[],
                    evidence=_record_evidence(cwd_value, date_text),
                )
            )
        return candidates

    def native_diagnostics(self, report: dict[str, Any]) -> dict[str, Any]:
        return {
            "auto_memory": {
                "present": bool(report.get("present")),
                "size_bytes": int(report.get("size_bytes") or 0),
                "over_limit": bool(report.get("over_limit")),
                "path": report.get("path"),
            }
        }


def compile_claude_turn(
    prompt: str,
    *,
    cwd: str | Path | None = None,
    projects_base: str | Path | None = None,
    library_path: str | Path | None = None,
    access_level: str = "team",
    max_items: int = DEFAULT_MAX_ITEMS,
    max_chars: int = DEFAULT_MAX_CHARS,
    delivery: str = "all",
) -> TurnBrief:
    """Compile a turn-scoped Claude Code recognition brief.

    The function is read-only. It does not write native Claude files, mutate the
    federation library, run model calls, or depend on native memory health.
    """
    return compile_turn(
        prompt,
        source=ClaudeSessionSource(),
        cwd=cwd,
        home=projects_base,
        library_path=library_path,
        access_level=access_level,
        max_items=max_items,
        max_chars=max_chars,
        delivery=delivery,
    )


# -- Claude native surface readers --------------------------------------------


def _find_memory_md(home: Path | None) -> Path | None:
    """Return the first ``<slug>/memory/MEMORY.md`` under the projects base."""
    if home is None:
        return None
    try:
        if not home.is_dir():
            return None
        for child in sorted(home.iterdir()):
            candidate = child / "memory" / "MEMORY.md"
            if candidate.is_file():
                return candidate
    except (OSError, PermissionError):
        return None
    return None


def _scan_jsonl_head(transcript: Path) -> tuple[str, str | None]:
    """Read the first few JSONL records for a thread name + cwd (bounded)."""
    name = ""
    cwd: str | None = None
    try:
        with open(transcript, encoding="utf-8") as handle:
            for index, line in enumerate(handle):
                if index >= _MAX_JSONL_HEAD_LINES:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if not isinstance(record, dict):
                    continue
                if cwd is None:
                    candidate_cwd = record.get("cwd")
                    if isinstance(candidate_cwd, str) and candidate_cwd.strip():
                        cwd = candidate_cwd.strip()
                if not name:
                    name = _record_display_name(record)
                if name and cwd:
                    break
    except (OSError, PermissionError):
        return "", None
    return name, cwd


def _record_display_name(record: dict[str, Any]) -> str:
    if str(record.get("type")) == "summary" and isinstance(record.get("summary"), str):
        return record["summary"].strip()
    return _first_user_text(record)


def _first_user_text(record: dict[str, Any]) -> str:
    message = record.get("message")
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and isinstance(block.get("text"), str):
                return block["text"].strip()
    return ""


def _decode_claude_slug(slug: str) -> str | None:
    """Best-effort decode of a Claude project slug back to a cwd path.

    Claude encodes a workspace path into a slug by replacing path separators
    (and the Windows drive colon) with ``-``, e.g.
    ``C:\\Users\\cumul\\repos\\bourdon`` -> ``C--Users-cumul-repos-bourdon`` and
    ``/Users/foo/bar`` -> ``-Users-foo-bar``. This cannot be reversed perfectly
    (directory names may themselves contain ``-``), so the authoritative cwd is
    always the one read from the transcript; this is a display/match hint only.
    """
    if not slug:
        return None
    windows = re.match(r"^([A-Za-z])--(.*)$", slug)
    if windows:
        drive = windows.group(1).upper()
        rest = windows.group(2).replace("-", "\\")
        return f"{drive}:\\{rest}" if rest else f"{drive}:\\"
    if slug.startswith("-"):
        return "/" + slug[1:].replace("-", "/")
    return slug.replace("-", "/")


def _date_from_mtime(mtime: float) -> str:
    try:
        return datetime.fromtimestamp(mtime, tz=timezone.utc).date().isoformat()
    except (OverflowError, OSError, ValueError):
        return ""


def _record_summary(cwd: str | None) -> str:
    if cwd:
        return f"cwd {cwd}"
    return "Recent Claude session metadata."


def _record_evidence(cwd: str | None, date_text: str) -> list[str]:
    evidence: list[str] = []
    if date_text:
        evidence.append(f"session date {date_text}")
    if cwd:
        evidence.append(f"cwd {cwd}")
    return evidence
