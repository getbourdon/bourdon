"""Bourdon participant for the Claude desktop app's Co-Work / local-agent mode.

This is the **richest** of the two Claude-desktop surfaces. The desktop app
stores each Co-Work run as a state file plus a sibling audit transcript:

    <desktop>/local-agent-mode-sessions/<acct>/<org>/local_<runUUID>.json   (state)
    <desktop>/local-agent-mode-sessions/<acct>/<org>/local_<runUUID>/audit.jsonl  (transcript)

This participant emits **recognition metadata only**. From the state file it
reads surface scalars (title, cwd, model, permission mode, timestamps,
``enabledMcpTools`` *count*, ``userSelectedFolders`` *basenames*). From the
sibling ``audit.jsonl`` it reads ONLY the ``system``/``init`` capability
*counts* and the ``result``/``success`` safe scalars (cost, turns, error flag).

It NEVER reads conversation content: ``audit.jsonl`` ``user``/``assistant``
message bodies, ``mcqAnswers`` answers, ``initialMessage``, ``result`` text, or
any tool input/output. See ``participants/_claude_desktop.py`` for the shared,
privacy-reviewed extraction helpers; every emitted string is run through the
shared redactor + a length cap.

Distinct from ``participants.claude_code`` (the interactive CLI) and
``participants.claude_desktop_code`` (the desktop GUI's Claude Code).
"""

from __future__ import annotations

import logging
import socket
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from participants._claude_desktop import (
    COWORK_STORE,
    bounded,
    count_enabled_mcp_tools,
    default_claude_desktop_dir,
    infer_projects,
    iter_state_files,
    load_state_json,
    read_audit_scalars,
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

AGENT_ID = "claude-desktop-cowork"
AGENT_TYPE = "code-assistant"
DISPLAY_NAME = "Claude Desktop · Co-Work"
SURFACE_ENTITY_NAME = "Claude Desktop Co-Work"
ROLE_NARRATIVE = (
    "Claude desktop app, Co-Work / local-agent mode. Bourdon reads the "
    "per-run local state and audit transcript to surface recognition metadata "
    "-- title, project, model, turn/cost scalars, capability counts -- never "
    "conversation content -- so Co-Work runs are visible to other agents."
)

DEFAULT_POLICY = VisibilityPolicy(
    default=Visibility.TEAM,
    private_tags=["personal", "financial", "credential", "health", "family", "legal"],
    team_tags=["claude-desktop", "claude-desktop-cowork", "agent-surface", "workspace"],
)

_MAX_KEY_ACTIONS = 8


@dataclass(frozen=True)
class CoworkRun:
    """Normalized, privacy-redacted view of a single Co-Work run."""

    run_id: str
    date: str
    cwd: str
    title: str
    model: str
    permission_mode: str
    is_archived: bool
    mcp_tool_count: int
    projects: tuple[str, ...]
    # Safe scalars sourced from audit.jsonl (all optional).
    num_turns: int | None = None
    total_cost_usd: float | None = None
    is_error: bool | None = None
    init_counts: dict[str, int] = field(default_factory=dict)


# -- Parsing ------------------------------------------------------------------


def _run_dir_for(state_path: Path) -> Path:
    """Sibling transcript dir: ``local_<id>.json`` -> ``local_<id>/``."""
    return state_path.with_suffix("")


def _run_from_state(state_path: Path, state: dict[str, Any]) -> CoworkRun:
    audit = read_audit_scalars(_run_dir_for(state_path))

    title = safe_label(state.get("title"), limit=160) or "(untitled run)"
    model = safe_label(state.get("model"), limit=80)
    permission_mode = safe_label(state.get("permissionMode"), limit=40)

    num_turns = audit.get("num_turns")
    cost = audit.get("total_cost_usd")
    init_counts = {
        k: v
        for k, v in audit.items()
        if k.startswith("init_") and isinstance(v, int)
    }

    return CoworkRun(
        run_id=str(state.get("sessionId") or state_path.stem),
        date=session_date(state),
        cwd=safe_label(state.get("cwd"), limit=300),
        title=title,
        model=model,
        permission_mode=permission_mode,
        is_archived=bool(state.get("isArchived")),
        mcp_tool_count=count_enabled_mcp_tools(state.get("enabledMcpTools")),
        projects=tuple(infer_projects(state)),
        num_turns=num_turns if isinstance(num_turns, int) else None,
        total_cost_usd=float(cost) if isinstance(cost, (int, float)) else None,
        is_error=audit.get("is_error") if isinstance(audit.get("is_error"), bool) else None,
        init_counts=init_counts,
    )


def _key_actions(run: CoworkRun) -> list[str]:
    """Build the bounded, redacted key-action list for a run.

    Per the contract: ``[title, "model: ...", "<n> turns", "$<cost>", "error"]``
    -- all metadata, never content.
    """
    actions: list[str] = [bounded(run.title, 160)]
    if run.model:
        actions.append(bounded(f"model: {run.model}", 120))
    if run.permission_mode:
        actions.append(bounded(f"permission: {run.permission_mode}", 80))
    if run.num_turns is not None:
        actions.append(f"{run.num_turns} turns")
    if run.total_cost_usd is not None:
        actions.append(f"${run.total_cost_usd:.2f}")
    if run.is_error:
        actions.append("error")
    if run.mcp_tool_count:
        actions.append(f"mcp-tools: {run.mcp_tool_count}")
    return actions[:_MAX_KEY_ACTIONS]


def _session_from_run(run: CoworkRun) -> Session:
    return Session(
        date=run.date,
        cwd=run.cwd or None,
        project_focus=list(run.projects),
        key_actions=_key_actions(run),
        files_touched=[],  # never list user files -- privacy
        visibility=Visibility.TEAM,
    )


def _capabilities(runs: list[CoworkRun]) -> list[str]:
    """Manifest-level capability *counts* only (no names)."""
    caps: list[str] = [AGENT_ID]
    max_mcp = max((run.mcp_tool_count for run in runs), default=0)
    caps.append(f"mcp-tools:{max_mcp}")
    # Surface the richest init snapshot's tool/skill counts, if any run had one.
    best_tools = max((run.init_counts.get("init_tools", 0) for run in runs), default=0)
    if best_tools:
        caps.append(f"tools:{best_tools}")
    best_skills = max((run.init_counts.get("init_skills", 0) for run in runs), default=0)
    if best_skills:
        caps.append(f"skills:{best_skills}")
    return caps


def _entities_from_runs(runs: list[CoworkRun]) -> list[Entity]:
    last_seen = max((run.date for run in runs), default=None)
    entities: dict[str, Entity] = {
        SURFACE_ENTITY_NAME: Entity(
            name=SURFACE_ENTITY_NAME,
            type="agent-surface",
            summary=bounded(
                "Claude desktop app Co-Work / local-agent mode surface "
                "(metadata-only federation).",
                260,
            ),
            last_touched=last_seen,
            tags=["claude-desktop", "claude-desktop-cowork", "agent-surface"],
            visibility=Visibility.TEAM,
        )
    }
    for run in runs:
        for project in run.projects:
            entities.setdefault(
                project,
                Entity(
                    name=project,
                    type="project",
                    summary="Project inferred from a Claude Desktop Co-Work run cwd.",
                    last_touched=run.date or None,
                    tags=["claude-desktop", "claude-desktop-cowork", "project"],
                    visibility=Visibility.TEAM,
                ),
            )
    return list(entities.values())


class ClaudeDesktopCoworkParticipant:
    """External participant for the Claude desktop app's Co-Work surface."""

    agent_id = AGENT_ID
    agent_type = AGENT_TYPE
    display_name = DISPLAY_NAME

    @classmethod
    def default_native_path(cls, home: Path | None = None) -> Path:
        """The Co-Work sub-store dir the setup wizard probes for presence.

        Resolves to ``<desktop>/local-agent-mode-sessions``. When the desktop
        dir cannot be resolved on this platform, falls back to a non-existent
        sentinel under ``home`` so the wizard reports "not found" rather than
        crashing.
        """
        desktop = default_claude_desktop_dir(home)
        if desktop is None:
            return (home or Path.home()) / "Claude" / COWORK_STORE
        return desktop / COWORK_STORE

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
                f"Claude Desktop Co-Work store not found at {self._store_dir}."
            )
        state_files = iter_state_files(self._store_dir)
        return AgentStore(
            path=str(self._store_dir),
            version="unknown",
            metadata={"runs": len(state_files)},
        )

    def export_sessions(self, since: datetime, limit: int = 100) -> list[Session]:
        runs = self._runs(since=since)
        sessions = [_session_from_run(run) for run in runs]
        sessions.sort(key=lambda s: s.date, reverse=True)
        return sessions[:limit]

    def export_l5(self, since: datetime | None = None) -> L5Manifest:
        if not self._store_dir.is_dir():
            raise ParticipantDiscoveryError(
                f"Claude Desktop Co-Work store not found at {self._store_dir}."
            )
        runs = self._runs(since=since)
        sessions = [_session_from_run(run) for run in runs]
        sessions.sort(key=lambda s: s.date, reverse=True)
        entities = _entities_from_runs(runs)
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
            capabilities=_capabilities(runs),
            recent_sessions=sessions,
            known_entities=visible_entities,
            visibility_policy=self._policy,
        )

    def health_check(self) -> HealthStatus:
        if not self._store_dir.is_dir():
            return HealthStatus(
                status="blocked",
                reason=f"Claude Desktop Co-Work store not found at {self._store_dir}.",
                details={"store_dir": str(self._store_dir)},
                proposed_fix=(
                    "Install the Claude desktop app and run a Co-Work / "
                    "local-agent session once. Set BOURDON_CLAUDE_DESKTOP_DIR "
                    "if the app stores state in a non-standard location."
                ),
            )
        try:
            state_files = iter_state_files(self._store_dir)
            runs, malformed = self._collect_runs()
        except Exception as exc:  # noqa: BLE001 -- health check must not raise
            logger.warning("ClaudeDesktopCoworkParticipant health_check failed: %s", exc)
            return HealthStatus(
                status="degraded",
                reason="Co-Work store present but extraction failed.",
                details={"error": str(exc)},
                proposed_fix=(
                    "Close the Claude desktop app (its state files may be "
                    "locked) and re-run `bourdon claude-desktop-cowork export`."
                ),
            )
        if not state_files:
            return HealthStatus(
                status="degraded",
                reason="No Co-Work runs found under the store directory.",
                details={"store_dir": str(self._store_dir)},
                proposed_fix=(
                    "Run a Co-Work / local-agent session in the Claude desktop "
                    "app, then re-run `bourdon claude-desktop-cowork export`."
                ),
            )
        return HealthStatus(
            status="ok",
            reason=None,
            details={
                "store_dir": str(self._store_dir),
                "run_count": len(state_files),
                "runs_extracted": len(runs),
                "malformed_records": malformed,
                "runs_with_scalars": sum(1 for r in runs if r.num_turns is not None),
            },
        )

    # -- Internal -------------------------------------------------------------

    def _collect_runs(self) -> tuple[list[CoworkRun], int]:
        runs: list[CoworkRun] = []
        malformed = 0
        for state_path in iter_state_files(self._store_dir):
            state = load_state_json(state_path)
            if state is None:
                malformed += 1
                continue
            runs.append(_run_from_state(state_path, state))
        return runs, malformed

    def _runs(self, since: datetime | None = None) -> list[CoworkRun]:
        runs, _ = self._collect_runs()
        if since is not None:
            cutoff = since.astimezone(timezone.utc).date().isoformat()
            runs = [run for run in runs if not run.date or run.date >= cutoff]
        runs.sort(key=lambda run: (run.date, run.run_id), reverse=True)
        return runs
