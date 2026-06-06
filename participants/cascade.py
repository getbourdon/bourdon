"""
Bourdon participant for Cascade (Windsurf).

Cascade is an agentic AI coding assistant embedded in the Windsurf IDE. It has
persistent memory, multi-step planning, tool use (file editing, terminal, search),
and workspace-level context awareness.

This participant uses a **convention-based** approach: Cascade maintains a structured
memory file at ``~/.cascade-bourdon/memory.md`` with YAML front-matter containing
entities and sessions. This file can be updated by Cascade at session end, giving
it persistent cross-session entity awareness via the L6 federation library.

Architecture choice
-------------------
Cascade's internal state is not directly accessible on the filesystem in a
standardized format (similar to Copilot). The convention-file approach means
Cascade owns its memory projection explicitly -- it writes what it knows, and
the participant normalizes that into L5.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import yaml

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
from participants.codex import (
    _NATIVE_MEMORY_SENSITIVE_PATTERNS,
    _safe_native_memory_text,
)

logger = logging.getLogger(__name__)

# -- Constants -----------------------------------------------------------------

AGENT_ID = "cascade"
AGENT_TYPE = "code-assistant"
ROLE_NARRATIVE = (
    "Agentic AI coding assistant embedded in Windsurf IDE. "
    "Operates with multi-step planning, tool use (file editing, terminal, "
    "browser preview, code search), persistent memory, and workspace-level "
    "context awareness. Specializes in pair-programming workflows with "
    "concurrent read-plan-execute cycles."
)

_CONVENTION_DIR_NAME = ".cascade-bourdon"
_MEMORY_FILENAME = "memory.md"

DEFAULT_POLICY = VisibilityPolicy(
    default=Visibility.PUBLIC,
    private_tags=["personal", "credential", "financial", "secret", "private"],
)

_CASCADE_SENSITIVE_PATTERNS = _NATIVE_MEMORY_SENSITIVE_PATTERNS + (
    re.compile(r"\bsecret\b", re.IGNORECASE),
    re.compile(r"sk[_-]test[_-]", re.IGNORECASE),
)


# -- Helpers -------------------------------------------------------------------


def default_cascade_dir() -> Path:
    """Return the default Cascade-Bourdon convention directory."""
    return Path.home() / _CONVENTION_DIR_NAME


def default_cascade_memory_path() -> Path:
    """Return the default path to the Cascade memory file."""
    return default_cascade_dir() / _MEMORY_FILENAME


def _parse_frontmatter(text: str, source: Optional[Path] = None) -> dict[str, Any]:
    """
    Extract YAML front-matter from a ``---`` fenced block.

    Returns an empty dict if the text has no valid front-matter. On YAML
    parse failure logs at WARNING with participant id, source path (if provided),
    and a truncated exception detail so the offending file is discoverable.
    See issue #79.
    """
    if not text.startswith("---"):
        return {}
    end = text.find("---", 3)
    if end == -1:
        return {}
    yaml_block = text[3:end].strip()
    if not yaml_block:
        return {}
    try:
        data = yaml.safe_load(yaml_block)
    except yaml.YAMLError as exc:
        where = f" in {source}" if source is not None else ""
        detail = str(exc).replace("\n", " ")[:200]
        logger.warning(
            "CascadeParticipant: malformed YAML frontmatter%s; "
            "treating as no-frontmatter (%s)",
            where,
            detail,
        )
        return {}
    return data if isinstance(data, dict) else {}


def _scrub_credential(text: str) -> str:
    """Redact + truncate native-memory text.

    Same semantics as codex's ``_safe_native_memory_text``, extended with
    Cascade-specific patterns (``secret``, ``sk_test_*``).
    """
    if not text:
        return text
    if any(p.search(text) for p in _CASCADE_SENSITIVE_PATTERNS):
        return "[redacted credential-like text]"
    return _safe_native_memory_text(text)


def _build_entity(raw: Any) -> Entity | None:
    """Build an Entity from a raw front-matter dict entry. Returns None on invalid."""
    if not isinstance(raw, dict):
        return None
    name = raw.get("name")
    if not isinstance(name, str) or not name.strip():
        return None

    summary = raw.get("summary")
    if isinstance(summary, str):
        summary = _scrub_credential(summary)

    return Entity(
        name=name.strip(),
        type=raw.get("type"),
        summary=summary,
        aliases=list(raw.get("aliases") or []),
        tags=list(raw.get("tags") or []),
        last_touched=str(raw["last_touched"]) if raw.get("last_touched") else None,
        valid_from=str(raw["valid_from"]) if raw.get("valid_from") else None,
        valid_to=str(raw["valid_to"]) if raw.get("valid_to") else None,
        visibility=None,
    )


def _build_session(raw: Any) -> Session | None:
    """Build a Session from a raw front-matter dict entry. Returns None on invalid."""
    if not isinstance(raw, dict):
        return None
    date_val = raw.get("date")
    if not date_val:
        return None
    # Normalize datetime strings to date-only (YYYY-MM-DD)
    date_str = str(date_val)[:10]

    return Session(
        date=date_str,
        cwd=raw.get("cwd"),
        key_actions=list(raw.get("key_actions") or []),
        files_touched=list(raw.get("files_touched") or []),
        project_focus=list(raw.get("project_focus") or []),
        visibility=None,
    )


def _inspect_cascade_memory(cascade_dir: Path) -> dict[str, Any]:
    """
    Diagnostic inspection of the Cascade memory file.

    Returns a dict with presence, readability, and content stats.
    """
    memory_path = cascade_dir / _MEMORY_FILENAME
    if not memory_path.is_file():
        return {"present": False, "error": "missing"}

    try:
        text = memory_path.read_text(encoding="utf-8")
    except OSError as e:
        return {"present": True, "readable": False, "error": str(e)}

    data = _parse_frontmatter(text, source=memory_path)
    if not data:
        return {
            "present": True,
            "readable": True,
            "frontmatter_valid": False,
            "entity_count": 0,
            "session_count": 0,
        }

    entities = data.get("entities") or []
    sessions = data.get("sessions") or []
    return {
        "present": True,
        "readable": True,
        "frontmatter_valid": True,
        "entity_count": len(entities) if isinstance(entities, list) else 0,
        "session_count": len(sessions) if isinstance(sessions, list) else 0,
    }


# -- Init helper ---------------------------------------------------------------

_MEMORY_TEMPLATE = """\
---
entities:
  - name: Example Project
    type: project
    summary: Replace with real project summaries
    tags: [project]
sessions:
  - date: "{today}"
    cwd: /path/to/workspace
    key_actions:
      - Initialized Cascade Bourdon memory
    files_touched: []
    project_focus: []
---

# Cascade Bourdon Memory

This file is maintained by Cascade (Windsurf) for cross-agent memory federation.
Edit the YAML front-matter to update entities and sessions.
Cascade will update this file at session end when instructed.
"""


def init_memory_file(
    cascade_dir: Path | None = None, force: bool = False
) -> Path:
    """
    Create a starter memory.md in the Cascade-Bourdon convention directory.

    Parameters
    ----------
    cascade_dir : Path, optional
        Override the convention directory. Defaults to ``~/.cascade-bourdon``.
    force : bool
        If True, overwrite an existing file. Otherwise raises FileExistsError.

    Returns
    -------
    Path
        The path to the created memory file.
    """
    target_dir = cascade_dir or default_cascade_dir()
    target_dir.mkdir(parents=True, exist_ok=True)
    memory_path = target_dir / _MEMORY_FILENAME

    if memory_path.exists() and not force:
        raise FileExistsError(
            f"Memory file already exists: {memory_path}. Use force=True to overwrite."
        )

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    content = _MEMORY_TEMPLATE.format(today=today)
    memory_path.write_text(content, encoding="utf-8")
    return memory_path


# -- Participant -------------------------------------------------------------------


class CascadeParticipant(BourdonParticipant):
    """
    Convention-based Bourdon participant for Cascade (Windsurf).

    Reads structured memory from ``~/.cascade-bourdon/memory.md`` and
    normalizes it into an L5 manifest for federation.
    """

    agent_id = AGENT_ID
    agent_type = AGENT_TYPE

    def __init__(
        self,
        cascade_dir: Path | None = None,
        policy: VisibilityPolicy | None = None,
    ) -> None:
        self._dir = cascade_dir or default_cascade_dir()
        self._policy = policy or DEFAULT_POLICY

    @property
    def native_path(self) -> str:
        """Return the path to the Cascade-Bourdon convention directory."""
        return str(self._dir)

    def _memory_path(self) -> Path:
        return self._dir / _MEMORY_FILENAME

    def _read_frontmatter(self) -> dict[str, Any]:
        """Read and parse the memory file's front-matter."""
        path = self._memory_path()
        if not path.is_file():
            return {}
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            return {}
        return _parse_frontmatter(text, source=path)

    # -- BourdonParticipant protocol -----------------------------------------------

    def discover(self) -> AgentStore:
        """
        Verify the convention directory exists and report its state.

        Raises ParticipantDiscoveryError if the directory is missing.
        """
        if not self._dir.is_dir():
            raise ParticipantDiscoveryError(
                f"Cascade-Bourdon directory not found: {self._dir}"
            )
        memory_present = self._memory_path().is_file()
        return AgentStore(
            path=str(self._dir),
            metadata={
                "memory_file": str(self._memory_path()),
                "memory_file_present": memory_present,
            },
        )

    def export_l5(
        self,
        since: datetime | None = None,
        access_level: str = "team",
    ) -> L5Manifest:
        """
        Export the Cascade memory as an L5 manifest.

        Parameters
        ----------
        since : datetime, optional
            Only include sessions on or after this datetime.
        access_level : str
            Visibility filter level (public, team, private).
        """
        data = self._read_frontmatter()

        # Build entities
        raw_entities = data.get("entities") or []
        entities: list[Entity] = []
        for raw in raw_entities:
            entity = _build_entity(raw)
            if entity is not None:
                entities.append(entity)

        # Build sessions
        raw_sessions = data.get("sessions") or []
        sessions: list[Session] = []
        for raw in raw_sessions:
            session = _build_session(raw)
            if session is None:
                continue
            if since is not None:
                try:
                    session_date = datetime.fromisoformat(session.date)
                    if session_date.tzinfo is None:
                        session_date = session_date.replace(tzinfo=timezone.utc)
                    if session_date < since:
                        continue
                except ValueError:
                    pass
            sessions.append(session)

        # Apply visibility policy -- filter out PRIVATE entities
        entities = filter_for_federation(entities, self._policy)

        return L5Manifest(
            spec_version=SPEC_VERSION,
            agent=AgentInfo(
                id=AGENT_ID,
                type=AGENT_TYPE,
                role_narrative=ROLE_NARRATIVE,
            ),
            last_updated=datetime.now(timezone.utc).isoformat(),
            known_entities=entities,
            recent_sessions=sessions,
            capabilities=["chat", "code-editing", "terminal", "planning", "search"],
        )

    def export_sessions(
        self,
        since: datetime | None = None,
        limit: int | None = None,
    ) -> list[Session]:
        """Export sessions, optionally filtered by date and limited in count."""
        data = self._read_frontmatter()
        raw_sessions = data.get("sessions") or []
        sessions: list[Session] = []
        for raw in raw_sessions:
            session = _build_session(raw)
            if session is None:
                continue
            if since is not None:
                try:
                    session_date = datetime.fromisoformat(session.date)
                    if session_date.tzinfo is None:
                        session_date = session_date.replace(tzinfo=timezone.utc)
                    if session_date < since:
                        continue
                except ValueError:
                    pass
            sessions.append(session)

        sessions.sort(key=lambda s: s.date, reverse=True)
        if limit is not None:
            sessions = sessions[:limit]
        return sessions

    def health_check(self) -> HealthStatus:
        """
        Check the health of the Cascade-Bourdon integration.

        Returns
        -------
        HealthStatus
            - ``ok``: directory exists, memory file present and parseable
            - ``degraded``: directory exists but memory file missing or empty
            - ``blocked``: directory does not exist
        """
        if not self._dir.is_dir():
            return HealthStatus(
                status="blocked",
                reason="Cascade-Bourdon directory not found",
                details={"expected_path": str(self._dir)},
                proposed_fix="Run `bourdon cascade init` to create the convention directory + starter memory.md.",
            )

        report = _inspect_cascade_memory(self._dir)
        if not report.get("present"):
            return HealthStatus(
                status="degraded",
                reason="Memory file not found; run `bourdon cascade init` to create it",
                details=report,
                proposed_fix="Run `bourdon cascade init` to write the starter memory.md template.",
            )

        if not report.get("readable"):
            return HealthStatus(
                status="degraded",
                reason=f"Memory file not readable: {report.get('error')}",
                details=report,
                proposed_fix=(
                    f"Check filesystem permissions on {self._dir / 'memory.md'} "
                    "(should be readable by your user)."
                ),
            )

        if not report.get("frontmatter_valid"):
            return HealthStatus(
                status="degraded",
                reason="Memory file has no valid YAML front-matter",
                details=report,
                proposed_fix=(
                    f"Inspect {self._dir / 'memory.md'} -- the opening and closing "
                    "`---` fences must wrap a valid YAML block. Run "
                    "`bourdon cascade init --force` to reset to the template "
                    "(WARNING: this overwrites existing content)."
                ),
            )

        return HealthStatus(
            status="ok",
            reason=None,
            details=report,
        )
