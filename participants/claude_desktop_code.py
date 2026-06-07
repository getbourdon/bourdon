"""Bourdon participant for the Claude desktop app's GUI Claude Code surface.

The desktop GUI's Claude Code keeps **metadata-only** state -- there is no
transcript on disk:

    <desktop>/claude-code-sessions/<acct>/<org>/local_<convUUID>.json

State keys: ``sessionId, cliSessionId, cwd, originCwd, createdAt(int),
lastActivityAt(int), model, effort, isArchived, title, titleSource,
permissionMode, planPath, enabledMcpTools{}``.

Even though the on-disk state is already metadata-only, this participant still
emits **recognition metadata only** and routes every emitted string through the
shared redactor + length cap (defense in depth): title, cwd->project, model,
effort, ``enabledMcpTools`` *count*, timestamps. It never emits ``planPath``
contents or any free-form text beyond the redacted title.

Distinct from ``participants.claude_code`` (the interactive CLI) and
``participants.claude_desktop_cowork`` (the richer Co-Work surface). See
``participants/_claude_desktop.py`` for the shared extraction helpers.
"""

from __future__ import annotations

import logging
import socket
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from participants._claude_desktop import (
    CODE_STORE,
    bounded,
    count_enabled_mcp_tools,
    default_claude_desktop_dir,
    infer_projects,
    iter_state_files,
    load_state_json,
    safe_label,
    session_date,
)
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

logger = logging.getLogger(__name__)

AGENT_ID = "claude-desktop-code"
AGENT_TYPE = "code-assistant"
DISPLAY_NAME = "Claude Desktop · Code"
SURFACE_ENTITY_NAME = "Claude Desktop Code"
ROLE_NARRATIVE = (
    "Claude desktop app, GUI Claude Code. Bourdon reads the metadata-only "
    "per-conversation local state to surface recognition metadata -- title, "
    "project, model, effort, capability counts -- never conversation content "
    "-- so desktop Claude Code work is visible to other agents."
)

DEFAULT_POLICY = VisibilityPolicy(
    default=Visibility.TEAM,
    private_tags=["personal", "financial", "credential", "health", "family", "legal"],
    team_tags=["claude-desktop", "claude-desktop-code", "agent-surface", "workspace"],
)

_MAX_KEY_ACTIONS = 6


@dataclass(frozen=True)
class CodeConversation:
    """Normalized, privacy-redacted view of a single desktop Claude Code conv."""

    conv_id: str
    date: str
    cwd: str
    title: str
    model: str
    effort: str
    permission_mode: str
    is_archived: bool
    mcp_tool_count: int
    projects: tuple[str, ...]


# -- Parsing ------------------------------------------------------------------


def _conversation_from_state(state_path: Path, state: dict[str, Any]) -> CodeConversation:
    return CodeConversation(
        conv_id=str(state.get("sessionId") or state_path.stem),
        date=session_date(state),
        cwd=safe_label(state.get("cwd"), limit=300),
        title=safe_label(state.get("title"), limit=160) or "(untitled conversation)",
        model=safe_label(state.get("model"), limit=80),
        effort=safe_label(state.get("effort"), limit=40),
        permission_mode=safe_label(state.get("permissionMode"), limit=40),
        is_archived=bool(state.get("isArchived")),
        mcp_tool_count=count_enabled_mcp_tools(state.get("enabledMcpTools")),
        projects=tuple(infer_projects(state)),
    )


def _key_actions(conv: CodeConversation) -> list[str]:
    actions: list[str] = [bounded(conv.title, 160)]
    if conv.model:
        actions.append(bounded(f"model: {conv.model}", 120))
    if conv.effort:
        actions.append(bounded(f"effort: {conv.effort}", 60))
    if conv.permission_mode:
        actions.append(bounded(f"permission: {conv.permission_mode}", 80))
    if conv.mcp_tool_count:
        actions.append(f"mcp-tools: {conv.mcp_tool_count}")
    return actions[:_MAX_KEY_ACTIONS]


def _session_from_conversation(conv: CodeConversation) -> Session:
    return Session(
        date=conv.date,
        cwd=conv.cwd or None,
        project_focus=list(conv.projects),
        key_actions=_key_actions(conv),
        files_touched=[],  # never list user files -- privacy
        visibility=Visibility.TEAM,
    )


def _capabilities(convs: list[CodeConversation]) -> list[str]:
    max_mcp = max((conv.mcp_tool_count for conv in convs), default=0)
    return [AGENT_ID, f"mcp-tools:{max_mcp}"]


def _entities_from_conversations(convs: list[CodeConversation]) -> list[Entity]:
    last_seen = max((conv.date for conv in convs), default=None)
    entities: dict[str, Entity] = {
        SURFACE_ENTITY_NAME: Entity(
            name=SURFACE_ENTITY_NAME,
            type="agent-surface",
            summary=bounded(
                "Claude desktop app GUI Claude Code surface "
                "(metadata-only federation).",
                260,
            ),
            last_touched=last_seen,
            tags=["claude-desktop", "claude-desktop-code", "agent-surface"],
            visibility=Visibility.TEAM,
        )
    }
    for conv in convs:
        for project in conv.projects:
            entities.setdefault(
                project,
                Entity(
                    name=project,
                    type="project",
                    summary="Project inferred from a Claude Desktop Code conversation cwd.",
                    last_touched=conv.date or None,
                    tags=["claude-desktop", "claude-desktop-code", "project"],
                    visibility=Visibility.TEAM,
                ),
            )
    return list(entities.values())


class ClaudeDesktopCodeParticipant:
    """External participant for the Claude desktop app's GUI Claude Code surface."""

    agent_id = AGENT_ID
    agent_type = AGENT_TYPE
    display_name = DISPLAY_NAME

    @classmethod
    def default_native_path(cls, home: Path | None = None) -> Path:
        """The Claude Code sub-store dir the setup wizard probes for presence.

        Resolves to ``<desktop>/claude-code-sessions``. Falls back to a
        non-existent sentinel under ``home`` on an unrecognized platform so the
        wizard reports "not found" rather than crashing.
        """
        desktop = default_claude_desktop_dir(home)
        if desktop is None:
            return (home or Path.home()) / "Claude" / CODE_STORE
        return desktop / CODE_STORE

    def __init__(
        self,
        store_dir: Path | None = None,
        home: Path | None = None,
    ) -> None:
        self._store_dir = store_dir or self.default_native_path(home)
        self._policy = DEFAULT_POLICY

    @property
    def native_path(self) -> str:
        return str(self._store_dir)

    # -- Protocol surface -----------------------------------------------------

    def discover(self) -> AgentStore:
        if not self._store_dir.is_dir():
            raise ParticipantDiscoveryError(
                f"Claude Desktop Code store not found at {self._store_dir}."
            )
        state_files = iter_state_files(self._store_dir)
        return AgentStore(
            path=str(self._store_dir),
            version="unknown",
            metadata={"conversations": len(state_files)},
        )

    def export_sessions(self, since: datetime, limit: int = 100) -> list[Session]:
        convs = self._conversations(since=since)
        sessions = [_session_from_conversation(conv) for conv in convs]
        sessions.sort(key=lambda s: s.date, reverse=True)
        return sessions[:limit]

    def export_l5(self, since: datetime | None = None) -> L5Manifest:
        if not self._store_dir.is_dir():
            raise ParticipantDiscoveryError(
                f"Claude Desktop Code store not found at {self._store_dir}."
            )
        convs = self._conversations(since=since)
        sessions = [_session_from_conversation(conv) for conv in convs]
        sessions.sort(key=lambda s: s.date, reverse=True)
        entities = _entities_from_conversations(convs)
        visible_entities = filter_for_federation(entities, self._policy)
        return L5Manifest(
            spec_version=SPEC_VERSION,
            agent=AgentInfo(
                id=AGENT_ID,
                type=AGENT_TYPE,
                instance=socket.gethostname(),
                spec_version_compat=SPEC_VERSION,
                role_narrative=ROLE_NARRATIVE,
            ),
            last_updated=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            capabilities=_capabilities(convs),
            recent_sessions=sessions,
            known_entities=visible_entities,
            visibility_policy=self._policy,
        )

    def health_check(self) -> HealthStatus:
        if not self._store_dir.is_dir():
            return HealthStatus(
                status="blocked",
                reason=f"Claude Desktop Code store not found at {self._store_dir}.",
                details={"store_dir": str(self._store_dir)},
                proposed_fix=(
                    "Install the Claude desktop app and open a Claude Code "
                    "conversation once. Set BOURDON_CLAUDE_DESKTOP_DIR if the "
                    "app stores state in a non-standard location."
                ),
            )
        try:
            state_files = iter_state_files(self._store_dir)
            convs, malformed = self._collect_conversations()
        except Exception as exc:  # noqa: BLE001 -- health check must not raise
            logger.warning("ClaudeDesktopCodeParticipant health_check failed: %s", exc)
            return HealthStatus(
                status="degraded",
                reason="Code store present but extraction failed.",
                details={"error": str(exc)},
                proposed_fix=(
                    "Close the Claude desktop app (its state files may be "
                    "locked) and re-run `bourdon claude-desktop-code export`."
                ),
            )
        if not state_files:
            return HealthStatus(
                status="degraded",
                reason="No Claude Code conversations found under the store directory.",
                details={"store_dir": str(self._store_dir)},
                proposed_fix=(
                    "Open a Claude Code conversation in the Claude desktop app, "
                    "then re-run `bourdon claude-desktop-code export`."
                ),
            )
        return HealthStatus(
            status="ok",
            reason=None,
            details={
                "store_dir": str(self._store_dir),
                "conversation_count": len(state_files),
                "conversations_extracted": len(convs),
                "malformed_records": malformed,
            },
        )

    # -- Internal -------------------------------------------------------------

    def _collect_conversations(self) -> tuple[list[CodeConversation], int]:
        convs: list[CodeConversation] = []
        malformed = 0
        for state_path in iter_state_files(self._store_dir):
            state = load_state_json(state_path)
            if state is None:
                malformed += 1
                continue
            convs.append(_conversation_from_state(state_path, state))
        return convs, malformed

    def _conversations(self, since: datetime | None = None) -> list[CodeConversation]:
        convs, _ = self._collect_conversations()
        if since is not None:
            cutoff = since.astimezone(timezone.utc).date().isoformat()
            convs = [conv for conv in convs if not conv.date or conv.date >= cutoff]
        convs.sort(key=lambda conv: (conv.date, conv.conv_id), reverse=True)
        return convs
