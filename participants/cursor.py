"""
Bourdon external participant for Cursor (the AI-first IDE).

Cursor stores its workspace state in SQLite databases at platform-specific
paths (`~/.config/Cursor/`, `~/Library/Application Support/Cursor/`,
`%APPDATA%/Cursor/`). This participant discovers those stores, copies them
read-only to a tmp file, parses the ``ItemTable`` key-value rows, and
emits a normalized Bourdon L5 manifest.

The SQLite extraction logic is in ``participants/_cursor_sqlite.py``. This
module wraps it in the ``BourdonParticipant`` Protocol from
``participants/base.py`` and applies the project's standard visibility policy.

Origin: this participant graduates from the v0 implementation in
``ryandavispro1-cmyk/cursor-spot`` (``cursor_bourdon`` package). The
SQLite extraction is preserved verbatim; the L5 emission is rewritten
on top of Bourdon's normative schema (``participants/base.L5Manifest``,
``Entity``, ``Session``) for federation consistency.

Usage::

    from participants.cursor import CursorParticipant

    participant = CursorParticipant()
    store = participant.discover()
    manifest = participant.export_l5()
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

from participants._cursor_sqlite import (
    CursorSQLiteMemories,
    default_cursor_dir,
    extract_cursor_memories,
)
from participants.base import (
    ParticipantDiscoveryError,
    AgentInfo,
    AgentStore,
    Entity,
    HealthStatus,
    L5Manifest,
    Session,
    Visibility,
    VisibilityPolicy,
    filter_for_federation,
)

logger = logging.getLogger(__name__)


AGENT_ID = "cursor"
AGENT_TYPE = "code-assistant"
ROLE_NARRATIVE = (
    "AI-first IDE. Bourdon reads the SQLite-backed composer/workspace "
    "state to surface recent sessions and project entities to other agents."
)

DEFAULT_POLICY = VisibilityPolicy(
    default=Visibility.TEAM,
    private_tags=["personal", "credential", "secret"],
    team_tags=["cursor", "workspace", "sqlite"],
)

_SPEC_VERSION = "0.1"


class CursorParticipant:
    """External participant for Cursor's SQLite workspace state.

    Implements the :class:`~participants.base.BourdonParticipant` Protocol
    structurally. Defensive throughout: missing data dir → raise
    ``ParticipantDiscoveryError`` from ``discover()``; everything else
    degrades to empty results rather than raising, matching the
    contract spec.
    """

    agent_id = AGENT_ID
    agent_type = AGENT_TYPE

    @classmethod
    def default_native_path(cls, home: Path | None = None) -> Path:
        """Conventional Cursor config dir (``~/.cursor``) used by the setup wizard.

        The wizard probes ``~/.cursor`` for *presence*; the SQLite workspace state
        that ``export_l5`` actually reads lives at the platform-specific
        :func:`default_cursor_dir` (e.g. ``%APPDATA%/Cursor``). Detection and
        extraction intentionally use different anchors.
        """
        return (home or Path.home()) / ".cursor"

    def __init__(self, cursor_dir: Path | None = None) -> None:
        self._cursor_dir = cursor_dir
        self._policy = DEFAULT_POLICY

    @property
    def native_path(self) -> str:
        path = self._cursor_dir or default_cursor_dir()
        return str(path) if path is not None else ""

    # -- Protocol surface -----------------------------------------------------

    def discover(self) -> AgentStore:
        """Locate the Cursor data directory and return metadata.

        Raises ``ParticipantDiscoveryError`` if no Cursor data directory is
        present at the default platform path or the explicitly-passed
        ``cursor_dir``.
        """
        path = self._cursor_dir or default_cursor_dir()
        if path is None or not path.is_dir():
            raise ParticipantDiscoveryError(
                f"Cursor data directory not found at {path!r}. "
                "Pass an explicit ``cursor_dir`` to CursorParticipant() if Cursor "
                "stores its state somewhere non-standard."
            )
        return AgentStore(
            path=str(path),
            version="unknown",
            metadata={"platform_default": str(default_cursor_dir())},
        )

    def export_sessions(
        self,
        since: datetime,
        limit: int = 100,
    ) -> list[Session]:
        """Return recent Cursor sessions newer than ``since``, capped at ``limit``."""
        memories = self._extract()
        out: list[Session] = []
        since_iso = since.astimezone(timezone.utc).date().isoformat()
        for raw in memories.sessions:
            if raw.date and raw.date < since_iso:
                continue
            out.append(_to_session(raw))
            if len(out) >= limit:
                break
        return out

    def export_l5(self, since: datetime | None = None) -> L5Manifest:
        """Build the L5 manifest from Cursor's current SQLite state.

        Filters sessions to those newer than ``since`` when provided.
        Applies ``DEFAULT_POLICY`` visibility before returning so the
        manifest is safe to drop into ``~/agent-library/agents/``.
        """
        memories = self._extract()
        sessions = [_to_session(s) for s in memories.sessions]
        if since is not None:
            since_iso = since.astimezone(timezone.utc).date().isoformat()
            sessions = [s for s in sessions if not s.date or s.date >= since_iso]

        entities = [_to_entity(e) for e in memories.entities]
        visible_entities = filter_for_federation(entities, self._policy)

        return L5Manifest(
            spec_version=_SPEC_VERSION,
            agent=AgentInfo(
                id=AGENT_ID,
                type=AGENT_TYPE,
                role_narrative=ROLE_NARRATIVE,
                spec_version_compat=_SPEC_VERSION,
            ),
            last_updated=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            capabilities=["composer-history", "workspace-state"],
            recent_sessions=sessions,
            known_entities=visible_entities,
            visibility_policy=self._policy,
        )

    def health_check(self) -> HealthStatus:
        """Report ok / degraded / blocked. Never raises."""
        path = self._cursor_dir or default_cursor_dir()
        if path is None:
            return HealthStatus(
                status="blocked",
                reason="Cursor data directory not resolvable on this platform.",
                details={},
                proposed_fix=(
                    "Install Cursor (https://cursor.sh) and open it once. "
                    "Set CURSOR_DIR if Cursor stores state outside the default location."
                ),
            )
        if not path.is_dir():
            return HealthStatus(
                status="blocked",
                reason=f"Cursor data directory not present at {path}.",
                details={"expected_path": str(path)},
                proposed_fix=(
                    "Install Cursor and open it once to create the data directory, "
                    "or set CURSOR_DIR to the actual location."
                ),
            )
        try:
            memories = self._extract()
        except Exception as exc:  # noqa: BLE001 -- health check must not raise
            logger.warning("CursorParticipant health_check extraction failed: %s", exc)
            return HealthStatus(
                status="degraded",
                reason="Cursor data directory present but extraction failed.",
                details={"error": str(exc)},
                proposed_fix=(
                    "Close Cursor (its SQLite stores may be locked) then re-run "
                    "`bourdon cursor export`. If extraction still fails, file an "
                    "issue with the error above."
                ),
            )
        if memories.databases_scanned == ():
            return HealthStatus(
                status="degraded",
                reason="No Cursor SQLite stores found under the data directory.",
                details={"path": str(path)},
                proposed_fix=(
                    "Open Cursor and use it for at least one chat session, then "
                    "re-run `bourdon cursor export`."
                ),
            )
        return HealthStatus(
            status="ok",
            reason=None,
            details={
                "databases_scanned": len(memories.databases_scanned),
                "sessions_extracted": len(memories.sessions),
                "entities_extracted": len(memories.entities),
                "malformed_records": memories.malformed_records,
            },
        )

    # -- Internal -------------------------------------------------------------

    def _extract(self) -> CursorSQLiteMemories:
        return extract_cursor_memories(self._cursor_dir)


# -- Conversion helpers -------------------------------------------------------


def _to_session(raw) -> Session:
    return Session(
        date=raw.date or "",
        cwd=raw.cwd or None,
        project_focus=[],
        key_actions=list(raw.key_actions),
        files_touched=list(raw.files_touched),
    )


def _to_entity(raw) -> Entity:
    return Entity(
        name=raw.name,
        type=raw.entity_type or None,
        aliases=list(raw.aliases),
        summary=raw.summary or None,
        tags=list(raw.tags),
    )
