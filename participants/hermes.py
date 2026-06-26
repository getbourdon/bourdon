"""Hermes participant -- normalize Hermes Agent memory + sessions into L5.

Hermes Agent (Nous Research) keeps all of its durable state under a single home
directory, by default ``~/.hermes`` (overridable via ``$HERMES_HOME``). The two
surfaces this participant reads:

  * ``state.db``  -- a SQLite store with ``sessions`` and ``messages`` tables.
    Every CLI, TUI, and gateway (Slack/Telegram/Discord/...) conversation lands
    here. We derive recent-session rows and project/topic entities from it.
  * ``memories/`` -- durable, cross-session facts the agent has chosen to save
    (one Markdown file per memory store, e.g. ``memory.md`` / ``user.md``). These
    are the highest-signal entities -- user preferences and environment facts.

Design notes / parity with the Codex participant:

  * Read-only. We never write to ``state.db`` (we open it ``mode=ro`` via URI so
    a live Hermes process holding a write lock cannot be disturbed).
  * Deterministic. ``export_l5`` is a pure function of on-disk state, so L6 can
    detect change via a manifest hash (PARTICIPANT_CONTRACT.md "Idempotency").
  * Visibility is enforced here, before emission. Memory-derived entities default
    to ``team``; anything carrying a private-class tag (personal/financial/...)
    is dropped via ``filter_for_federation`` so it never reaches L6.
  * Never propagates unknown exceptions to L6: ``health_check`` always returns a
    ``HealthStatus`` and ``discover`` raises only ``ParticipantDiscoveryError``.

Registered under the ``bourdon.participants`` entry point as ``hermes``.
"""

from __future__ import annotations

import logging
import os
import re
import sqlite3
from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from core.redaction import redact_text
from participants._sqlite_base import (
    epoch_to_iso_date,
    friendly_label,
    project_key_from_cwd,
    table_exists,
    try_open_readonly,
)
from participants.base import (
    SPEC_VERSION,
    AgentInfo,
    AgentStore,
    BourdonParticipant,
    Entity,
    HealthStatus,
    L5Manifest,
    ParticipantDiscoveryError,
    Session,
    Visibility,
    VisibilityPolicy,
    filter_for_federation,
)

logger = logging.getLogger(__name__)

CONTRACT_VERSION = "0.1"

AGENT_ID = "hermes"
AGENT_TYPE = "code-assistant"
DISPLAY_NAME = "Hermes Agent"
ROLE_NARRATIVE = (
    "General-purpose tool-calling assistant (Nous Research). Operates across CLI, "
    "TUI, and messaging gateways (Slack/Telegram/Discord/WhatsApp); runs terminal, "
    "file, web, and delegation toolsets. Carries durable cross-session memory and a "
    "curated skill library. Federates session + memory context, not vendor account."
)

# How many recent sessions feed the manifest by default.
DEFAULT_SESSION_LIMIT = 100

# Hermes memory stores live as Markdown under ~/.hermes/memories/.
_MEMORY_FILENAMES = ("memory.md", "user.md")

DEFAULT_POLICY = VisibilityPolicy(
    default=Visibility.TEAM,
    private_tags=[
        "personal",
        "financial",
        "credential",
        "secret",
        "health",
        "family",
        "legal",
    ],
    team_tags=["hermes-memory", "hermes-session", "hermes-project"],
)

# cwd basenames that carry no project identity.
_GENERIC_PROJECT_NAMES = frozenset(
    {"", "root", "home", "tmp", "temp", "desktop", "documents", "downloads", "src"}
)

# Source rows in `sessions` whose conversations are gateway/messaging surfaces.
# Kept for capability reporting + per-source session counts.
_KNOWN_SOURCES = ("cli", "tui", "slack", "telegram", "discord", "whatsapp", "signal")


# -- Path resolution -----------------------------------------------------------


def default_native_path(home: Path | None = None) -> Path:
    """Conventional Hermes home used by the setup wizard's detection.

    Resolution order:
      1. An explicit ``home`` argument wins — the setup wizard passes
         ``home=<fake or real home>`` for hermetic, testable detection, so this
         must take precedence over ambient env to keep detection deterministic.
      2. Otherwise ``$HERMES_HOME`` (Hermes' own override) if set.
      3. Otherwise ``~/.hermes``.
    """
    if home is not None:
        return home / ".hermes"
    env = os.environ.get("HERMES_HOME")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".hermes"


def _resolve_hermes_home(base_home: Path | None = None) -> Path | None:
    path = default_native_path(base_home)
    return path if path.is_dir() else None


# -- Text helpers --------------------------------------------------------------


def _bounded(value: str, limit: int = 240) -> str:
    """Redact secrets, collapse whitespace, and clamp length."""
    cleaned = redact_text(re.sub(r"\s+", " ", value or "").strip(), limit=limit)
    return cleaned.strip()


def _epoch_to_iso_date(value: Any) -> str | None:
    """Hermes stores started_at/ended_at as float epoch seconds.

    Thin wrapper over the shared ``_sqlite_base.epoch_to_iso_date`` so the rest
    of this module (and its tests) keep a stable local name.
    """
    return epoch_to_iso_date(value)


def _project_key_from_cwd(cwd: str | None) -> str | None:
    """Project key from a cwd, treating Hermes' generic dirs as no-project."""
    return project_key_from_cwd(cwd, generic_names=_GENERIC_PROJECT_NAMES)


def _friendly_label(key: str) -> str:
    return friendly_label(key)


# -- SQLite (read-only) --------------------------------------------------------


def _open_state_db(state_db: Path) -> sqlite3.Connection:
    """Open state.db read-only so a live Hermes write-lock can't block us.

    Raises ``sqlite3.Error`` on failure (callers that need the never-raises
    contract use ``try_open_readonly`` directly).
    """
    conn = try_open_readonly(state_db)
    if conn is None:
        raise sqlite3.Error(f"cannot open {state_db} read-only")
    return conn


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return table_exists(conn, name)


def _collect_session_rows(
    state_db: Path, limit: int = DEFAULT_SESSION_LIMIT
) -> list[dict[str, Any]]:
    """Pull recent, non-archived sessions newest-first. Never raises."""
    if not state_db.is_file():
        return []
    out: list[dict[str, Any]] = []
    try:
        conn = _open_state_db(state_db)
    except sqlite3.Error as exc:
        logger.debug("hermes: cannot open state.db: %s", exc)
        return []
    try:
        if not _table_exists(conn, "sessions"):
            return []
        # `archived` may be absent in older schemas -- guard the column.
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(sessions)")}
        archived_clause = "WHERE COALESCE(archived, 0) = 0" if "archived" in cols else ""
        rows = conn.execute(
            f"""
            SELECT id, source, model, title, cwd, started_at, ended_at,
                   message_count, tool_call_count
            FROM sessions
            {archived_clause}
            ORDER BY started_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        for r in rows:
            day = _epoch_to_iso_date(r["started_at"])
            if not day:
                continue
            out.append(
                {
                    "id": r["id"],
                    "source": (r["source"] or "").lower(),
                    "model": r["model"],
                    "title": r["title"],
                    "cwd": r["cwd"],
                    "date": day,
                    "message_count": r["message_count"] or 0,
                    "tool_call_count": r["tool_call_count"] or 0,
                }
            )
    except sqlite3.Error as exc:
        logger.debug("hermes: session query failed: %s", exc)
    finally:
        conn.close()
    return out


def _tool_names_for_session(conn: sqlite3.Connection, session_id: str) -> list[str]:
    """Distinct tool names used in a session, for key_actions evidence."""
    if not _table_exists(conn, "messages"):
        return []
    try:
        rows = conn.execute(
            """
            SELECT DISTINCT tool_name FROM messages
            WHERE session_id = ? AND tool_name IS NOT NULL AND tool_name != ''
            """,
            (session_id,),
        ).fetchall()
    except sqlite3.Error:
        return []
    return sorted({r["tool_name"] for r in rows if r["tool_name"]})


# -- Memory parsing ------------------------------------------------------------

_BULLET_RE = re.compile(r"^\s*[-*]\s+(.*)$")


def _parse_memory_file(path: Path) -> list[str]:
    """Return memory entries (one per bullet / non-empty line). Never raises."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    entries: list[str] = []
    for line in text.splitlines():
        m = _BULLET_RE.match(line)
        candidate = m.group(1) if m else line.strip()
        if candidate and not candidate.startswith("#"):
            entries.append(candidate)
    return entries


def _collect_memory_entries(home: Path) -> dict[str, list[str]]:
    """Map memory store name -> list of entry strings."""
    mem_dir = home / "memories"
    result: dict[str, list[str]] = {}
    if not mem_dir.is_dir():
        return result
    for fname in _MEMORY_FILENAMES:
        path = mem_dir / fname
        if path.is_file():
            entries = _parse_memory_file(path)
            if entries:
                result[path.stem] = entries
    return result


# -- Entity / session builders -------------------------------------------------


def _build_session(record: dict[str, Any], tool_names: list[str]) -> Session:
    project_key = _project_key_from_cwd(record.get("cwd"))
    focus: list[str] = []
    if project_key:
        focus.append(project_key)
    title = record.get("title")
    key_actions: list[str] = []
    if title:
        key_actions.append(_bounded(title, 160))
    if tool_names:
        key_actions.append("Tools: " + ", ".join(tool_names[:8]))
    elif record.get("tool_call_count"):
        key_actions.append(f"{record['tool_call_count']} tool call(s)")
    return Session(
        date=record["date"],
        cwd=record.get("cwd") or None,
        project_focus=focus,
        key_actions=key_actions,
        visibility=Visibility.TEAM,
    )


def _memory_entity(store: str, entry: str) -> Entity:
    """Turn a single memory line into a team-visibility entity."""
    name = _bounded(entry, 80)
    tag = "hermes-user-memory" if store == "user" else "hermes-memory"
    return Entity(
        name=name,
        type="preference" if store == "user" else "fact",
        summary=_bounded(entry, 240),
        tags=[tag],
        visibility=Visibility.TEAM,
    )


def _project_entities(records: list[dict[str, Any]]) -> list[Entity]:
    counts = Counter(
        key
        for key in (_project_key_from_cwd(r.get("cwd")) for r in records)
        if key
    )
    entities: list[Entity] = []
    for key, count in counts.items():
        last = max(
            (r["date"] for r in records if _project_key_from_cwd(r.get("cwd")) == key),
            default=None,
        )
        entities.append(
            Entity(
                name=_friendly_label(key),
                type="project",
                summary=f"Workspace observed across {count} Hermes session(s).",
                aliases=[key],
                last_touched=last,
                tags=["hermes-project"],
                visibility=Visibility.TEAM,
            )
        )
    return entities


# -- Participant ---------------------------------------------------------------


class HermesParticipant:
    """External participant for local Hermes Agent memory + sessions."""

    agent_id = AGENT_ID
    agent_type = AGENT_TYPE
    display_name = DISPLAY_NAME

    @classmethod
    def default_native_path(cls, home: Path | None = None) -> Path:
        return default_native_path(home)

    def __init__(self, hermes_home: Path | None = None) -> None:
        if hermes_home is not None:
            self._home: Path | None = Path(hermes_home).expanduser()
        else:
            self._home = _resolve_hermes_home()
        self.native_path = str(self._home or (Path.home() / ".hermes"))

    # -- discovery -------------------------------------------------------------

    def _sources(self) -> dict[str, str | None]:
        home = self._home
        state_db = (home / "state.db") if home else None
        mem_dir = (home / "memories") if home else None
        skills_dir = (home / "skills") if home else None
        return {
            "hermes_home": str(home) if home else None,
            "state_db": str(state_db) if state_db and state_db.is_file() else None,
            "memories_dir": str(mem_dir) if mem_dir and mem_dir.is_dir() else None,
            "skills_dir": str(skills_dir) if skills_dir and skills_dir.is_dir() else None,
        }

    def discover(self) -> AgentStore:
        sources = self._sources()
        if not any(sources.values()):
            raise ParticipantDiscoveryError(
                "No Hermes memory sources found. Expected ~/.hermes/ "
                "(set $HERMES_HOME to override)."
            )
        if not sources["state_db"] and not sources["memories_dir"]:
            raise ParticipantDiscoveryError(
                f"Hermes home {self.native_path!r} exists but has no state.db or "
                "memories/ -- nothing to federate yet."
            )
        return AgentStore(
            path=self.native_path,
            version="hermes-home-v1",
            metadata={"sources": sources},
        )

    # -- sessions --------------------------------------------------------------

    def export_sessions(
        self, since: datetime, limit: int = DEFAULT_SESSION_LIMIT
    ) -> list[Session]:
        if self._home is None:
            return []
        state_db = self._home / "state.db"
        cutoff = since.date() if since else None
        records = _collect_session_rows(state_db, limit=limit)
        if not records:
            return []
        try:
            conn = _open_state_db(state_db)
        except sqlite3.Error:
            conn = None
        out: list[Session] = []
        try:
            for rec in records:
                try:
                    if cutoff and date.fromisoformat(rec["date"]) < cutoff:
                        continue
                except ValueError:
                    continue
                tools = (
                    _tool_names_for_session(conn, rec["id"]) if conn is not None else []
                )
                out.append(_build_session(rec, tools))
        finally:
            if conn is not None:
                conn.close()
        return out

    # -- L5 --------------------------------------------------------------------

    def export_l5(self, since: datetime | None = None) -> L5Manifest:
        store = self.discover()
        sources = store.metadata["sources"]
        capabilities = sorted(k for k, v in sources.items() if v and k != "hermes_home")

        home = self._home
        records = (
            _collect_session_rows(home / "state.db", limit=DEFAULT_SESSION_LIMIT)
            if home
            else []
        )

        # Entities: project workspaces + memory-derived facts/preferences.
        entities: list[Entity] = _project_entities(records)
        seen: set[tuple[str, str]] = {
            ((e.type or "topic"), e.name.lower()) for e in entities
        }
        if home:
            for store_name, entries in _collect_memory_entries(home).items():
                for entry in entries:
                    ent = _memory_entity(store_name, entry)
                    key = ((ent.type or "topic"), ent.name.lower())
                    if key in seen:
                        continue
                    seen.add(key)
                    entities.append(ent)

        # Visibility enforced here, before emission (contract requirement).
        visible = filter_for_federation(entities, DEFAULT_POLICY)

        sessions = self.export_sessions(
            since or datetime(1970, 1, 1, tzinfo=timezone.utc),
            limit=DEFAULT_SESSION_LIMIT,
        )

        return L5Manifest(
            spec_version=SPEC_VERSION,
            agent=AgentInfo(
                id=self.agent_id,
                type=self.agent_type,
                role_narrative=ROLE_NARRATIVE,
                spec_version_compat=CONTRACT_VERSION,
            ),
            last_updated=datetime.now(timezone.utc).isoformat(),
            capabilities=capabilities,
            recent_sessions=sessions,
            known_entities=visible,
            visibility_policy=DEFAULT_POLICY,
        )

    # -- health ----------------------------------------------------------------

    def health_check(self) -> HealthStatus:
        details: dict[str, Any] = {
            "hermes_home": self.native_path,
            "state_db": "missing",
            "memories_dir": "missing",
            "skills_dir": "missing",
        }
        if self._home is None:
            return HealthStatus(
                status="blocked",
                reason="~/.hermes/ not found -- Hermes Agent not installed here",
                details=details,
                proposed_fix=(
                    "Install Hermes Agent (https://hermes-agent.nousresearch.com) "
                    "and run a session once, then `bourdon export-all` to publish "
                    "its L5 manifest into your federation library."
                ),
            )
        sources = self._sources()
        for key in ("state_db", "memories_dir", "skills_dir"):
            if sources.get(key):
                details[key] = sources[key]

        if sources["state_db"]:
            records = _collect_session_rows(self._home / "state.db", limit=1)
            if records:
                return HealthStatus(status="ok", details=details)
            return HealthStatus(
                status="degraded",
                reason="state.db present but no readable sessions yet",
                details=details,
                proposed_fix=(
                    "Run at least one Hermes session (`hermes chat`), then "
                    "`bourdon export-all`."
                ),
            )
        if sources["memories_dir"]:
            return HealthStatus(
                status="degraded",
                reason="memories/ present but state.db missing -- session history unavailable",
                details=details,
            )
        return HealthStatus(
            status="blocked",
            reason="Hermes home has neither state.db nor memories/",
            details=details,
            proposed_fix="Run a Hermes session to populate ~/.hermes/state.db.",
        )


# Structural conformance assertion (mirrors codex.py's tail guard).
_: BourdonParticipant = HermesParticipant()
