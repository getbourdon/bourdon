"""Shared helpers for the Claude desktop-app participants.

The Claude *desktop* application (distinct from the ``claude-code`` CLI) keeps
per-surface local state under a platform-specific application-support directory.
Two surfaces are federated as separate Bourdon participants:

    * ``claude-desktop-cowork`` -- Co-Work / local-agent mode (richest surface;
      ``local-agent-mode-sessions/``)
    * ``claude-desktop-code``   -- the desktop GUI's Claude Code
      (metadata-only; ``claude-code-sessions/``)

Both stores share the same on-disk skeleton:

    <desktop>/<store>/<accountUUID>/<orgUUID>/local_<id>.json

This module factors out everything the two participants share: the
cross-platform desktop-dir resolver, the account/org glob, epoch->date
conversion, bounded/redacted text, project inference, and the
``enabledMcpTools`` -> capability-count helper.

PRIVACY: every string that can originate from on-disk state is passed through
``participants.codex._safe_native_memory_text`` (credential-pattern redaction +
link stripping) and then a length cap. The participants emit recognition
*metadata only* -- titles, cwd-derived project names, model/effort labels, turn
counts, costs, capability *counts*, and timestamps. Conversation bodies
(``audit.jsonl`` ``user``/``assistant`` messages, ``mcqAnswers``,
``initialMessage``, ``result`` text, tool inputs/outputs) are never read into
any emitted field.

The leading underscore in the module name keeps it out of participant
auto-discovery (``participants.discover_participants`` skips ``_``-prefixed
modules).
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from participants.codex import _safe_native_memory_text

logger = logging.getLogger(__name__)

# Environment override (tests + non-standard installs). Takes precedence over
# the platform default but not over an explicit ``home`` argument.
_DESKTOP_DIR_ENV = "BOURDON_CLAUDE_DESKTOP_DIR"

# Sub-store directory names under the desktop dir.
COWORK_STORE = "local-agent-mode-sessions"
CODE_STORE = "claude-code-sessions"

# State filename prefix shared by both stores (``local_<uuid>.json``).
_STATE_PREFIX = "local_"

# Caps -- keep emitted metadata small and bounded.
_MAX_KEY_ACTION_CHARS = 280
_MAX_TITLE_CHARS = 160
_MAX_KEY_ACTIONS = 8
_MAX_PROJECTS = 6
# audit.jsonl can be large; we only need the (small) init + result lines, so
# bound how much we are willing to scan before giving up.
_MAX_AUDIT_LINES = 50_000

# Project-name hints, mirroring participants.claude_code_automations._PROJECT_HINTS.
PROJECT_HINTS = (
    "ShipStable",
    "ILTT",
    "Prun",
    "PRUN",
    "OMNIvour",
    "Castmore",
    "Bourdon",
    "RADLAB",
    "CHIP",
    "Claude Brain",
    "Cursor",
    "Copilot",
    "Codex",
    "Cascade",
)


def default_claude_desktop_dir(home: Path | None = None) -> Path | None:
    """Resolve the Claude desktop application-support directory.

    Precedence:
      1. ``BOURDON_CLAUDE_DESKTOP_DIR`` env var (tests + non-standard installs).
      2. Platform default, anchored on ``home`` (defaults to ``Path.home()``):
           * Windows -- ``%APPDATA%/Claude`` (falls back to ``<home>/AppData/Roaming/Claude``)
           * macOS   -- ``<home>/Library/Application Support/Claude``
           * Linux   -- ``<home>/.config/Claude``

    Returns ``None`` only on an unrecognized platform with no env override --
    callers treat that as "blocked" rather than crashing.
    """
    env = os.environ.get(_DESKTOP_DIR_ENV)
    if env:
        return Path(env)

    base = home or Path.home()
    if sys.platform == "darwin":
        return base / "Library" / "Application Support" / "Claude"
    if sys.platform.startswith("win"):
        # When an explicit home is provided (tests), keep everything under it so
        # the fake tree is self-contained; otherwise honor %APPDATA%.
        if home is None:
            appdata = os.environ.get("APPDATA")
            if appdata:
                return Path(appdata) / "Claude"
        return base / "AppData" / "Roaming" / "Claude"
    if sys.platform.startswith("linux"):
        return base / ".config" / "Claude"
    return None


def iter_state_files(store_dir: Path) -> list[Path]:
    """Return every ``local_*.json`` under ``<store_dir>/<acct>/<org>/``.

    Globs across all account/org UUID directories. Returns ``[]`` (never
    raises) when the store dir is absent so health checks can distinguish
    "store missing" from "store empty".
    """
    if not store_dir.is_dir():
        return []
    # <store>/<acct>/<org>/local_*.json
    return sorted(store_dir.glob(f"*/*/{_STATE_PREFIX}*.json"))


def load_state_json(path: Path) -> dict[str, Any] | None:
    """Read + parse one ``local_*.json`` state file.

    Returns the parsed dict, or ``None`` on any read/parse failure or if the
    top-level JSON is not an object. Never raises -- malformed files are
    counted and skipped by callers.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("claude-desktop: cannot read %s: %s", path, exc)
        return None
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError) as exc:
        logger.warning("claude-desktop: cannot parse %s: %s", path, exc)
        return None
    if not isinstance(data, dict):
        return None
    return data


def epoch_to_date(value: Any) -> str:
    """Convert an epoch timestamp to a UTC ``YYYY-MM-DD`` string.

    Accepts ms (``> 1e12``) or seconds; ints, floats, and numeric strings are
    all tolerated. Returns ``""`` on anything unparseable so callers can fall
    back to a sibling timestamp.
    """
    if isinstance(value, bool):  # bool is an int subclass -- reject it explicitly
        return ""
    number: float | None = None
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str) and value.strip():
        try:
            number = float(value.strip())
        except ValueError:
            number = None
    if number is None:
        return ""
    seconds = number / 1000.0 if number > 1e12 else number
    try:
        return datetime.fromtimestamp(seconds, timezone.utc).date().isoformat()
    except (OverflowError, OSError, ValueError):
        return ""


def session_date(state: dict[str, Any]) -> str:
    """Pick a session date from ``createdAt`` then ``lastActivityAt``."""
    for key in ("createdAt", "lastActivityAt"):
        parsed = epoch_to_date(state.get(key))
        if parsed:
            return parsed
    return ""


def bounded(value: str, limit: int = _MAX_KEY_ACTION_CHARS) -> str:
    """Redact credential-like text + strip links, then cap to ``limit`` chars.

    Runs the value through ``_safe_native_memory_text`` (the shared Codex
    redactor) first so a planted secret becomes ``[redacted ...]`` *before*
    truncation can split it into a non-matching fragment.
    """
    return _safe_native_memory_text(value, limit=limit)


def safe_label(value: Any, limit: int = _MAX_KEY_ACTION_CHARS) -> str:
    """Coerce a scalar to a bounded, redacted display string ("" if empty)."""
    if value is None:
        return ""
    text = str(value).strip()
    if not text:
        return ""
    return bounded(text, limit)


def count_enabled_mcp_tools(enabled: Any) -> int:
    """Count ``True`` values in an ``enabledMcpTools`` mapping.

    Keys are ``"<serverUUID>:<tool>"`` and are NOT emitted -- only the count of
    enabled entries is surfaced, so no tool names or server identifiers leak.
    """
    if not isinstance(enabled, dict):
        return 0
    return sum(1 for v in enabled.values() if v is True)


def _basename(path_value: Any) -> str:
    if not isinstance(path_value, str) or not path_value.strip():
        return ""
    name = Path(path_value.strip()).name.strip()
    return name if name and name not in {".", "/", "\\"} else ""


def infer_projects(state: dict[str, Any]) -> list[str]:
    """Infer project names from cwd + user-selected folders (basenames only).

    Combines:
      * basename of ``cwd``
      * basenames of ``userSelectedFolders`` (Co-Work only; absent elsewhere)
      * a ``PROJECT_HINTS`` substring match over those path strings

    No file *contents* and no full paths are emitted -- only directory
    basenames and recognized project labels. Order-preserving + de-duplicated.
    """
    projects: list[str] = []
    seen: set[str] = set()

    def _add(name: str) -> None:
        cleaned = name.strip()
        if cleaned and cleaned.lower() not in seen:
            projects.append(cleaned)
            seen.add(cleaned.lower())

    path_strings: list[str] = []
    cwd = state.get("cwd")
    if isinstance(cwd, str) and cwd.strip():
        path_strings.append(cwd)
    folders = state.get("userSelectedFolders")
    if isinstance(folders, list):
        path_strings.extend(f for f in folders if isinstance(f, str) and f.strip())

    for raw_path in path_strings:
        base = _basename(raw_path)
        if base:
            _add(base)

    haystack = " ".join(path_strings).lower()
    for hint in PROJECT_HINTS:
        if hint.lower() in haystack:
            _add(hint)

    return projects[:_MAX_PROJECTS]


def read_audit_scalars(run_dir: Path) -> dict[str, Any]:
    """Extract ONLY the safe scalars from a Co-Work ``audit.jsonl`` transcript.

    Reads ``<run_dir>/audit.jsonl`` line-by-line, tolerantly, and pulls:

      * from the ``system``/``init`` record: capability *counts* + claude_code
        version (no tool names, no slash-command text -- counts + version only)
      * from the ``result``/``success`` record: ``total_cost_usd``,
        ``num_turns``, ``is_error``, ``duration_ms``, ``stop_reason``
        (these scalars are explicitly SAFE metadata)

    NEVER reads ``user``/``assistant`` message bodies or any other content
    field. A missing, unreadable, or locked ``audit.jsonl`` simply yields an
    empty dict ("no run scalars") -- it is never an error. The file is opened
    read-only and never rewritten (each line is HMAC-signed upstream).

    Returns a flat dict with any of these optional keys:
        total_cost_usd, num_turns, is_error, duration_ms, stop_reason,
        init_tools, init_mcp_servers, init_skills, init_plugins,
        init_slash_commands, claude_code_version
    """
    audit_path = run_dir / "audit.jsonl"
    if not audit_path.is_file():
        return {}

    out: dict[str, Any] = {}
    try:
        with audit_path.open("r", encoding="utf-8", errors="replace") as handle:
            for line_no, raw_line in enumerate(handle):
                if line_no >= _MAX_AUDIT_LINES:
                    break
                line = raw_line.strip()
                if not line or line[0] != "{":
                    continue
                try:
                    record = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if not isinstance(record, dict):
                    continue
                _absorb_audit_record(record, out)
    except OSError as exc:
        # Missing/locked file -> "no run scalars", not a failure.
        logger.warning("claude-desktop: cannot read %s: %s", audit_path, exc)
        return out
    return out


def _absorb_audit_record(record: dict[str, Any], out: dict[str, Any]) -> None:
    """Pull safe scalars from one audit record into ``out`` (in place).

    Only the ``system``/``init`` and ``result``/``success`` record types
    contribute. Everything else (``user``, ``assistant``, ``system``/``status``,
    ``compact_boundary``, ``permission_request``/``response``,
    ``rate_limit_event``) is ignored -- their bodies are never touched.
    """
    rec_type = record.get("type")
    subtype = record.get("subtype")

    if rec_type == "system" and subtype == "init":
        # Counts only -- never the tool/skill/command names themselves.
        for src_key, dst_key in (
            ("tools", "init_tools"),
            ("mcp_servers", "init_mcp_servers"),
            ("skills", "init_skills"),
            ("plugins", "init_plugins"),
            ("slash_commands", "init_slash_commands"),
        ):
            value = record.get(src_key)
            if isinstance(value, list):
                out[dst_key] = len(value)
        version = record.get("claude_code_version")
        if version is not None:
            out["claude_code_version"] = safe_label(version, limit=40)
        return

    if rec_type == "result":
        # These scalars are explicitly safe metadata; the ``result`` *text*
        # field (free-form summary) is deliberately NOT read.
        for key in ("total_cost_usd", "num_turns", "is_error", "duration_ms"):
            if key in record:
                out[key] = record[key]
        stop_reason = record.get("stop_reason")
        if stop_reason is not None:
            out["stop_reason"] = safe_label(stop_reason, limit=40)
        return
