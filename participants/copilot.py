"""
Bourdon external participant for GitHub Copilot.

GitHub Copilot has no accessible local session index -- it runs as an IDE
extension whose reasoning is cloud-side, and its native memory is not
exposed on disk in a readable format.  This participant takes the same fallback
approach that the Codex participant uses for pre-distillation state: it reads
from a **convention-based memory file** at ``~/.copilot-bourdon/memory.md``.

The file uses YAML front-matter for structured entity and session data,
followed by an optional freeform markdown body that Copilot Chat can read
as plain context:

.. code-block:: text

    ---
    entities:
      - name: ILTT
        type: project
        summary: AI fitness business platform
        tags: [project, active]
        last_touched: "2026-05-01"
    sessions:
      - date: "2026-05-10"
        cwd: /projects/bourdon
        key_actions:
          - Implemented Copilot participant for Bourdon
    ---

    # Copilot notes

    Freeform markdown below the closing ``---`` is available to Copilot
    Chat as context but is not parsed by this participant.

Users (or Copilot Chat itself, when instructed) maintain this file.  The
participant normalises its content into a Bourdon L5 manifest so Copilot's
cross-session context is visible in the L6 federation library alongside
Claude Code, Codex, and Cursor.

Usage::

    from participants.copilot import CopilotParticipant

    participant = CopilotParticipant()
    store   = participant.discover()
    manifest = participant.export_l5()

Paths checked (in order):

1. ``COPILOT_BOURDON_HOME`` env-var override
2. ``~/.copilot-bourdon/``   (default convention path)
"""

from __future__ import annotations

import logging
import os
import socket
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
from participants.codex import _NATIVE_MEMORY_SENSITIVE_PATTERNS, _safe_native_memory_text

logger = logging.getLogger(__name__)

AGENT_ID = "copilot"
AGENT_TYPE = "code-assistant"
ROLE_NARRATIVE = (
    "Inline completion and chat assistant present across every IDE the team uses. "
    "Works alongside the human at the keystroke level -- the ambient layer that "
    "recognises what is being typed before a full turn is even formed. Bourdon "
    "gives Copilot cross-session entity awareness it would otherwise lack entirely."
)

# Convention directory and file names. Override dir with COPILOT_BOURDON_HOME env var.
_CONVENTION_DIR_NAME = ".copilot-bourdon"
_MEMORY_FILENAME = "memory.md"

# Front-matter delimiters (mirrors claude_code._parse_frontmatter conventions).
_FRONTMATTER_OPEN = "---\n"
_FRONTMATTER_CLOSE = "\n---\n"

DEFAULT_POLICY = VisibilityPolicy(
    default=Visibility.TEAM,
    private_tags=["personal", "financial", "credential", "health", "family", "legal"],
    team_tags=["copilot-memory", "workspace", "copilot"],
)

# Starter template written by ``bourdon copilot init``.
_MEMORY_TEMPLATE = """\
---
# Copilot Bourdon Memory
# Edit this file to give Copilot cross-session entity awareness.
# The YAML front-matter is parsed by `bourdon copilot export`.
# The markdown body below the closing `---` is freeform context for Copilot Chat.

entities: []

sessions: []
---

# Copilot notes

Add project notes, preferences, or anything else you want Copilot Chat to
remember here. This section is freeform -- the participant only reads the YAML
front-matter above.
"""


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


def default_copilot_bourdon_dir() -> Path:
    """Return the conventional ``~/.copilot-bourdon/`` directory path.

    Respects the ``COPILOT_BOURDON_HOME`` environment variable override.
    The directory is not created here; creation is left to ``init_memory_file``.
    """
    env = os.environ.get("COPILOT_BOURDON_HOME")
    if env:
        return Path(env)
    return Path.home() / _CONVENTION_DIR_NAME


def default_copilot_memory_path(copilot_dir: Optional[Path] = None) -> Path:
    """Return the path to the convention memory file."""
    return (copilot_dir or default_copilot_bourdon_dir()) / _MEMORY_FILENAME


# ---------------------------------------------------------------------------
# Front-matter parsing
# ---------------------------------------------------------------------------


def _parse_frontmatter(text: str, source: Optional[Path] = None) -> dict[str, Any]:
    """Extract and parse the YAML front-matter block from memory file text.

    Returns an empty dict when the file has no front-matter or when the YAML
    cannot be parsed (logged at WARNING; never raises). On parse failure logs
    the source path (if provided) and a truncated exception so the offending
    file is discoverable. See issue #79.
    """
    if not text.startswith(_FRONTMATTER_OPEN):
        return {}
    end = text.find(_FRONTMATTER_CLOSE, len(_FRONTMATTER_OPEN))
    if end == -1:
        return {}
    fm_text = text[len(_FRONTMATTER_OPEN) : end]
    try:
        result = yaml.safe_load(fm_text)
        return result if isinstance(result, dict) else {}
    except yaml.YAMLError as exc:
        where = f" in {source}" if source is not None else ""
        detail = str(exc).replace("\n", " ")[:200]
        logger.warning(
            "CopilotParticipant: malformed YAML frontmatter%s; "
            "treating as no-frontmatter (%s)",
            where,
            detail,
        )
        return {}


def _read_memory_file(path: Path) -> dict[str, Any]:
    """Read and parse a copilot-bourdon memory file.

    Returns an empty dict on any I/O or parse error -- the participant degrades
    gracefully to an empty manifest rather than raising.
    """
    if not path.is_file():
        return {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("CopilotParticipant: cannot read %s: %s", path, exc)
        return {}
    return _parse_frontmatter(text, source=path)


# ---------------------------------------------------------------------------
# Entity / Session converters
# ---------------------------------------------------------------------------


def _build_entity(raw: Any) -> Optional[Entity]:
    """Convert a raw YAML entity dict to a Bourdon Entity.

    Skips malformed entries (logs at DEBUG). Never raises.
    """
    if not isinstance(raw, dict):
        logger.debug("CopilotParticipant: skipping non-dict entity record: %r", raw)
        return None
    name = raw.get("name")
    if not isinstance(name, str) or not name.strip():
        logger.debug("CopilotParticipant: skipping entity with missing name: %r", raw)
        return None

    summary_raw = raw.get("summary") or ""
    summary = _safe_native_memory_text(str(summary_raw)) if summary_raw else None

    aliases_raw = raw.get("aliases") or []
    aliases = [str(a) for a in aliases_raw if isinstance(a, str) and a.strip()]

    tags_raw = raw.get("tags") or []
    tags = [str(t) for t in tags_raw if isinstance(t, str) and t.strip()]

    return Entity(
        name=name.strip(),
        type=str(raw.get("type") or "") or None,
        aliases=aliases,
        summary=summary,
        last_touched=str(raw.get("last_touched") or "") or None,
        tags=tags,
        valid_from=str(raw.get("valid_from") or "") or None,
        valid_to=str(raw.get("valid_to") or "") or None,
    )


def _build_session(raw: Any) -> Optional[Session]:
    """Convert a raw YAML session dict to a Bourdon Session.

    Skips malformed entries (logs at DEBUG). Never raises.
    """
    if not isinstance(raw, dict):
        logger.debug("CopilotParticipant: skipping non-dict session record: %r", raw)
        return None
    date_val = raw.get("date")
    if not date_val:
        logger.debug("CopilotParticipant: skipping session with no date: %r", raw)
        return None
    date_str = str(date_val)[:10]  # keep YYYY-MM-DD prefix only

    actions_raw = raw.get("key_actions") or []
    actions = [str(a) for a in actions_raw if isinstance(a, str) and a.strip()]

    files_raw = raw.get("files_touched") or []
    files = [str(f) for f in files_raw if isinstance(f, str) and f.strip()]

    focus_raw = raw.get("project_focus") or []
    focus = [str(p) for p in focus_raw if isinstance(p, str) and p.strip()]

    return Session(
        date=date_str,
        cwd=str(raw.get("cwd") or "") or None,
        project_focus=focus,
        key_actions=actions,
        files_touched=files,
    )


# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------


def _inspect_copilot_memory(copilot_dir: Optional[Path]) -> dict[str, Any]:
    """Return a diagnostic summary of the copilot-bourdon memory file.

    Used by ``bourdon copilot doctor``. Never raises.
    """
    mem_path = default_copilot_memory_path(copilot_dir)
    report: dict[str, Any] = {
        "path": str(mem_path),
        "present": mem_path.is_file(),
        "readable": False,
        "frontmatter_valid": False,
        "entity_count": 0,
        "session_count": 0,
        "error": None,
    }
    if not mem_path.is_file():
        report["error"] = "missing"
        return report
    try:
        text = mem_path.read_text(encoding="utf-8")
        report["readable"] = True
    except OSError as exc:
        report["error"] = str(exc)
        return report

    data = _parse_frontmatter(text, source=mem_path)
    if data:
        report["frontmatter_valid"] = True
    report["entity_count"] = len(data.get("entities") or [])
    report["session_count"] = len(data.get("sessions") or [])
    return report


# ---------------------------------------------------------------------------
# Participant
# ---------------------------------------------------------------------------


class CopilotParticipant:
    """External participant for GitHub Copilot via ``~/.copilot-bourdon/memory.md``.

    Implements the :class:`~participants.base.BourdonParticipant` Protocol structurally.
    Defensive throughout: missing convention directory → ``ParticipantDiscoveryError``
    from ``discover()``; bad YAML degrades to empty results rather than raising,
    matching the contract spec.
    """

    agent_id = AGENT_ID
    agent_type = AGENT_TYPE
    display_name = "GitHub Copilot"

    @classmethod
    def default_native_path(cls, home: Path | None = None) -> Path:
        """Conventional ``~/.copilot-bourdon`` dir used by the setup wizard's detection."""
        if home is not None:
            return home / ".copilot-bourdon"
        return default_copilot_bourdon_dir()

    def __init__(self, copilot_dir: Optional[Path] = None) -> None:
        self._copilot_dir = copilot_dir

    @property
    def native_path(self) -> str:
        return str(self._copilot_dir or default_copilot_bourdon_dir())

    # -- Protocol surface -----------------------------------------------------

    def discover(self) -> AgentStore:
        """Locate the copilot-bourdon convention directory and return metadata.

        Raises ``ParticipantDiscoveryError`` if the convention directory is absent.
        Users create it with ``bourdon copilot init`` or by placing a
        ``~/.copilot-bourdon/memory.md`` file manually.
        """
        path = self._copilot_dir or default_copilot_bourdon_dir()
        if not path.is_dir():
            raise ParticipantDiscoveryError(
                f"Copilot convention directory not found at {path!r}. "
                "Run `bourdon copilot init` to create it with a starter template, "
                "or create ~/.copilot-bourdon/memory.md manually."
            )
        mem_path = path / _MEMORY_FILENAME
        return AgentStore(
            path=str(path),
            version="convention-v1",
            metadata={
                "memory_file": str(mem_path),
                "memory_file_present": mem_path.is_file(),
            },
        )

    def export_sessions(self, since: datetime, limit: int = 100) -> list[Session]:
        """Return recent Copilot sessions newer than ``since``, capped at ``limit``."""
        data = self._read()
        since_iso = since.astimezone(timezone.utc).date().isoformat()
        sessions: list[Session] = []
        for raw in data.get("sessions") or []:
            session = _build_session(raw)
            if session is None:
                continue
            if session.date and session.date < since_iso:
                continue
            sessions.append(session)
            if len(sessions) >= limit:
                break
        return sessions

    def export_l5(self, since: Optional[datetime] = None) -> L5Manifest:
        """Build the L5 manifest from the copilot-bourdon memory file.

        Filters sessions to those newer than ``since`` when provided.
        Applies ``DEFAULT_POLICY`` visibility before returning so the
        manifest is safe to drop into ``~/agent-library/agents/``.
        """
        data = self._read()

        raw_entities = data.get("entities") or []
        entities: list[Entity] = []
        for raw in raw_entities:
            entity = _build_entity(raw)
            if entity is not None:
                entities.append(entity)
        visible_entities = filter_for_federation(entities, DEFAULT_POLICY)

        raw_sessions = data.get("sessions") or []
        sessions: list[Session] = []
        since_iso = since.astimezone(timezone.utc).date().isoformat() if since else None
        for raw in raw_sessions:
            session = _build_session(raw)
            if session is None:
                continue
            if since_iso and session.date and session.date < since_iso:
                continue
            sessions.append(session)

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
            capabilities=["inline-completion", "chat", "pr-review", "convention-memory"],
            recent_sessions=sessions,
            known_entities=visible_entities,
            visibility_policy=DEFAULT_POLICY,
        )

    def health_check(self) -> HealthStatus:
        """Report ok / degraded / blocked. Never raises."""
        path = self._copilot_dir or default_copilot_bourdon_dir()
        if not path.is_dir():
            return HealthStatus(
                status="blocked",
                reason=(
                    f"Convention directory {path} not found. "
                    "Run `bourdon copilot init` to create it."
                ),
                details={"expected_path": str(path)},
                proposed_fix="Run `bourdon copilot init` to create the convention directory.",
            )
        mem_path = path / _MEMORY_FILENAME
        if not mem_path.is_file():
            return HealthStatus(
                status="degraded",
                reason=(
                    f"Memory file {mem_path} not found. "
                    "Run `bourdon copilot init` to write a starter template."
                ),
                details={"expected_memory_file": str(mem_path)},
                proposed_fix="Run `bourdon copilot init` to write the starter memory.md template.",
            )
        try:
            data = self._read()
        except Exception as exc:  # noqa: BLE001 -- health_check must not raise
            logger.warning("CopilotParticipant health_check failed: %s", exc)
            return HealthStatus(
                status="degraded",
                reason="Memory file present but could not be parsed.",
                details={"error": str(exc)},
                proposed_fix=(
                    f"Inspect {mem_path} for malformed YAML front-matter. "
                    "The opening and closing `---` fences must wrap a valid YAML "
                    "block. See docs/agent-integration-status.md for the schema."
                ),
            )
        return HealthStatus(
            status="ok",
            reason=None,
            details={
                "memory_file": str(mem_path),
                "entity_count": len(data.get("entities") or []),
                "session_count": len(data.get("sessions") or []),
            },
        )

    # -- Internal -------------------------------------------------------------

    def _read(self) -> dict[str, Any]:
        mem_path = default_copilot_memory_path(self._copilot_dir)
        return _read_memory_file(mem_path)


# ---------------------------------------------------------------------------
# Init helper
# ---------------------------------------------------------------------------


def init_memory_file(copilot_dir: Optional[Path] = None, force: bool = False) -> Path:
    """Create ``~/.copilot-bourdon/memory.md`` with a starter template.

    Returns the path of the written file. Raises ``FileExistsError`` when
    the file already exists unless ``force=True``.
    """
    target_dir = copilot_dir or default_copilot_bourdon_dir()
    target_dir.mkdir(parents=True, exist_ok=True)
    mem_path = target_dir / _MEMORY_FILENAME
    if mem_path.exists() and not force:
        raise FileExistsError(
            f"{mem_path} already exists. Pass --force to overwrite."
        )
    mem_path.write_text(_MEMORY_TEMPLATE, encoding="utf-8")
    return mem_path


# Protocol conformance check at import time -- catches missing methods before CI.
_: BourdonParticipant = CopilotParticipant()
