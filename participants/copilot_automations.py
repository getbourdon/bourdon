"""Bourdon participant for GitHub Copilot automation runs.

GitHub Copilot can be triggered by GitHub Actions workflows, scheduled tasks,
and event-driven automations (issue triage, PR review, etc.). These run on
a separate memory surface from both the CLI agent and the VS Code extension.

Since Copilot automations have no standardized local state dump, this
participant uses a **convention-based** approach (mirroring
``codex_automations.py``):

    ~/.copilot-bourdon/automations/<automation-id>/
        automation.toml   — id, name, status, trigger, kind, repos
        memory.md         — dated bullet entries, one block per run

Each ``automation.toml`` describes a recurring automation; each dated section
of ``memory.md`` captures what happened in that run. Users or CI scripts
maintain these files.

Usage::

    from participants.copilot_automations import CopilotAutomationsParticipant

    participant = CopilotAutomationsParticipant()
    store = participant.discover()
    manifest = participant.export_l5()
"""

from __future__ import annotations

import logging
import os
import re
import socket
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # pragma: no cover -- 3.10 path
    import tomli as tomllib  # type: ignore[no-redef]

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

AGENT_ID = "copilot-automations"
AGENT_TYPE = "other"
DISPLAY_NAME = "GitHub Copilot · Automations"
ROLE_NARRATIVE = (
    "Publishes read-only Copilot automation run memory into Bourdon so "
    "GitHub Actions-triggered Copilot tasks, scheduled PR reviews, issue "
    "triage, and other event-driven Copilot work are visible alongside "
    "interactive agent sessions."
)

DEFAULT_POLICY = VisibilityPolicy(
    default=Visibility.TEAM,
    private_tags=["personal", "financial", "credential", "health", "family", "legal"],
    team_tags=["copilot-automation", "automation", "github-actions", "workspace"],
)

_AUTOMATIONS_DIR = "automations"
_AUTOMATION_TOML = "automation.toml"
_MEMORY_MD = "memory.md"
_MAX_MEMORY_CHARS = 160_000
_MAX_KEY_ACTIONS_PER_RUN = 6
_MAX_KEY_ACTION_CHARS = 280
_RUN_HEADER_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})(?:\b|$)(.*)$")

# Starter template for `bourdon copilot-automations init`
_AUTOMATION_TOML_TEMPLATE = """\
# Copilot Automation definition
# Edit this file to describe a recurring Copilot automation.

id = "{automation_id}"
name = "{name}"
status = "ACTIVE"
trigger = "workflow_dispatch"  # or: schedule, issue_comment, pull_request, etc.
kind = "pr-review"           # or: issue-triage, code-generation, test-review, etc.
repos = []                   # list of repositories this automation targets
"""

_MEMORY_MD_TEMPLATE = """\
# {name} — Run Memory

Record automation run outcomes here. Each dated section is parsed by Bourdon.

# Format:
# YYYY-MM-DD <optional title>
# - bullet point of what happened
# - another action or outcome

"""


@dataclass(frozen=True)
class AutomationConfig:
    automation_id: str
    name: str
    status: str
    trigger: str
    kind: str
    repos: tuple[str, ...]
    path: Path
    memory_path: Optional[Path]


@dataclass(frozen=True)
class AutomationRun:
    automation: AutomationConfig
    date: str
    title: str
    key_actions: tuple[str, ...]
    repos: tuple[str, ...]


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


def default_copilot_automations_dir(copilot_bourdon_dir: Optional[Path] = None) -> Path:
    """Return the conventional ``~/.copilot-bourdon/automations/`` directory.

    Respects the ``COPILOT_AUTOMATIONS_HOME`` environment variable override.
    """
    env = os.environ.get("COPILOT_AUTOMATIONS_HOME")
    if env:
        return Path(env)
    base = copilot_bourdon_dir or Path.home() / ".copilot-bourdon"
    return base / _AUTOMATIONS_DIR


# ---------------------------------------------------------------------------
# Config/memory parsing
# ---------------------------------------------------------------------------


def _read_automation_toml(path: Path) -> dict[str, Any]:
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        logger.warning("CopilotAutomationsParticipant: cannot parse %s: %s", path, exc)
        return {}


def _build_config(toml_path: Path) -> Optional[AutomationConfig]:
    raw = _read_automation_toml(toml_path)
    automation_id = str(raw.get("id") or toml_path.parent.name).strip()
    if not automation_id:
        return None

    name = str(raw.get("name") or automation_id).strip()
    status = str(raw.get("status") or "UNKNOWN").strip().upper()
    trigger = str(raw.get("trigger") or "").strip()
    kind = str(raw.get("kind") or "").strip()
    repos_raw = raw.get("repos") or []
    repos = tuple(str(r) for r in repos_raw if isinstance(r, str) and r.strip())
    memory_path = toml_path.parent / _MEMORY_MD
    return AutomationConfig(
        automation_id=automation_id,
        name=name,
        status=status,
        trigger=trigger,
        kind=kind,
        repos=repos,
        path=toml_path,
        memory_path=memory_path if memory_path.is_file() else None,
    )


def _iter_configs(automations_dir: Path) -> list[AutomationConfig]:
    configs: list[AutomationConfig] = []
    if not automations_dir.is_dir():
        return configs
    for toml_path in sorted(automations_dir.glob(f"*/{_AUTOMATION_TOML}")):
        config = _build_config(toml_path)
        if config is not None:
            configs.append(config)
    return configs


def _extract_memory_runs(config: AutomationConfig) -> list[AutomationRun]:
    if config.memory_path is None:
        return []
    try:
        text = config.memory_path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("CopilotAutomationsParticipant: cannot read %s: %s", config.memory_path, exc)
        return []

    text = text[-_MAX_MEMORY_CHARS:]
    chunks: list[tuple[str, list[str]]] = []
    current_date = ""
    current_lines: list[str] = []

    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        match = _RUN_HEADER_RE.match(line.strip())
        if match:
            if current_date:
                chunks.append((current_date, current_lines))
            current_date = match.group(1)
            suffix = match.group(2).strip(" -:\u2014")
            current_lines = [suffix] if suffix else []
            continue
        if current_date:
            current_lines.append(line)
    if current_date:
        chunks.append((current_date, current_lines))

    runs: list[AutomationRun] = []
    for run_date, lines in chunks:
        actions = _actions_from_lines(lines)
        if not actions:
            continue
        title = _title_from_actions(config, actions)
        runs.append(
            AutomationRun(
                automation=config,
                date=run_date,
                title=title,
                key_actions=tuple(actions),
                repos=config.repos,
            )
        )
    return runs


def _actions_from_lines(lines: list[str]) -> list[str]:
    actions: list[str] = []
    for line in lines:
        cleaned = line.strip()
        if not cleaned:
            continue
        cleaned = cleaned.removeprefix("- ").strip()
        if not cleaned or cleaned.lower().startswith("runtime"):
            continue
        safe = _bounded(_safe_native_memory_text(cleaned), _MAX_KEY_ACTION_CHARS)
        if safe and safe not in actions:
            actions.append(safe)
        if len(actions) >= _MAX_KEY_ACTIONS_PER_RUN:
            break
    return actions


def _title_from_actions(config: AutomationConfig, actions: list[str]) -> str:
    if not actions:
        return config.name
    first = actions[0]
    prefix = f"{config.name}: "
    if first.startswith(prefix):
        return _bounded(first, 120)
    return _bounded(prefix + first, 120)


def _bounded(value: str, limit: int) -> str:
    normalized = " ".join(value.split())
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 1].rstrip() + "…"


# ---------------------------------------------------------------------------
# Conversion to Bourdon types
# ---------------------------------------------------------------------------


def _session_from_run(run: AutomationRun) -> Session:
    config = run.automation
    key_actions = [
        f"automation: {config.automation_id}",
        f"trigger: {config.trigger}" if config.trigger else f"kind: {config.kind}",
        f"run: {run.title}",
        *run.key_actions,
    ]
    return Session(
        date=run.date,
        cwd=str(config.path.parent),
        project_focus=list(run.repos),
        key_actions=key_actions[:_MAX_KEY_ACTIONS_PER_RUN + 3],
        files_touched=[str(config.path)],
        visibility=Visibility.TEAM,
    )


def _entities_from_configs_and_runs(
    configs: list[AutomationConfig],
    runs: list[AutomationRun],
) -> list[Entity]:
    entities: dict[str, Entity] = {}

    for config in configs:
        entities[config.automation_id] = Entity(
            name=config.automation_id,
            type="automation",
            summary=_bounded(
                f"Copilot automation '{config.name}' ({config.status}). "
                f"Trigger: {config.trigger or 'unspecified'}. Kind: {config.kind or 'unspecified'}.",
                260,
            ),
            last_touched=None,
            tags=["copilot-automation", "automation", config.status.lower()],
            visibility=Visibility.TEAM,
        )

    # Repos mentioned across all runs
    for run in runs:
        for repo in run.repos:
            entities.setdefault(
                repo,
                Entity(
                    name=repo,
                    type="project",
                    summary="Repository targeted by Copilot automation.",
                    last_touched=run.date,
                    tags=["copilot-automation", "project"],
                    visibility=Visibility.TEAM,
                ),
            )

    return list(entities.values())


# ---------------------------------------------------------------------------
# Participant
# ---------------------------------------------------------------------------


class CopilotAutomationsParticipant:
    """External participant for Copilot automation memory artifacts.

    Convention-based: users or CI scripts maintain automation.toml + memory.md
    files at ``~/.copilot-bourdon/automations/<id>/``.
    """

    agent_id = AGENT_ID
    agent_type = AGENT_TYPE
    display_name = DISPLAY_NAME

    @classmethod
    def default_native_path(cls, home: Path | None = None) -> Path:
        """Conventional automations dir (``~/.copilot-bourdon/automations``)."""
        base = (home or Path.home()) / ".copilot-bourdon"
        return base / _AUTOMATIONS_DIR

    def __init__(self, automations_dir: Optional[Path] = None) -> None:
        self._automations_dir = automations_dir or default_copilot_automations_dir()
        self._policy = DEFAULT_POLICY

    @property
    def native_path(self) -> str:
        return str(self._automations_dir)

    # -- Protocol surface -----------------------------------------------------

    def discover(self) -> AgentStore:
        if not self._automations_dir.is_dir():
            raise ParticipantDiscoveryError(
                f"Copilot automations directory not found at {self._automations_dir}. "
                "Run `bourdon copilot-automations init <name>` to create one."
            )
        configs = _iter_configs(self._automations_dir)
        return AgentStore(
            path=str(self._automations_dir),
            version="convention-v1",
            metadata={
                "automations": len(configs),
                "with_memory": sum(1 for c in configs if c.memory_path is not None),
            },
        )

    def export_sessions(self, since: datetime, limit: int = 100) -> list[Session]:
        sessions = [_session_from_run(run) for run in self._runs(since=since)]
        sessions.sort(key=lambda s: s.date, reverse=True)
        return sessions[:limit]

    def export_l5(self, since: Optional[datetime] = None) -> L5Manifest:
        configs = _iter_configs(self._automations_dir)
        runs = self._runs(configs=configs, since=since)
        sessions = [_session_from_run(run) for run in runs]
        sessions.sort(key=lambda s: s.date, reverse=True)
        entities = _entities_from_configs_and_runs(configs, runs)
        visible_entities = filter_for_federation(entities, self._policy)
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
            capabilities=["copilot-automation-memory", "run-summary-publication"],
            recent_sessions=sessions,
            known_entities=visible_entities,
            visibility_policy=self._policy,
        )

    def health_check(self) -> HealthStatus:
        if not self._automations_dir.is_dir():
            return HealthStatus(
                status="blocked",
                reason=f"Copilot automations directory not found at {self._automations_dir}.",
                details={"automations_dir": str(self._automations_dir)},
                proposed_fix="Run `bourdon copilot-automations init <name>` to create an automation.",
            )
        configs = _iter_configs(self._automations_dir)
        runs = self._runs(configs=configs)
        status = "ok" if configs else "degraded"
        reason = None if configs else "No automation.toml files found."
        return HealthStatus(
            status=status,
            reason=reason,
            details={
                "automations_dir": str(self._automations_dir),
                "automation_count": len(configs),
                "memory_files": sum(1 for c in configs if c.memory_path is not None),
                "runs_extracted": len(runs),
                "active_automations": sum(1 for c in configs if c.status == "ACTIVE"),
            },
            proposed_fix=None if configs else (
                "Run `bourdon copilot-automations init <name>` to create an automation, "
                "or add automation.toml files manually."
            ),
        )

    # -- Internal -------------------------------------------------------------

    def _runs(
        self,
        configs: Optional[list[AutomationConfig]] = None,
        since: Optional[datetime] = None,
    ) -> list[AutomationRun]:
        run_cutoff = since.astimezone(timezone.utc).date().isoformat() if since else None
        runs: list[AutomationRun] = []
        for config in configs or _iter_configs(self._automations_dir):
            for run in _extract_memory_runs(config):
                if run_cutoff and run.date < run_cutoff:
                    continue
                runs.append(run)
        runs.sort(key=lambda r: (r.date, r.automation.automation_id), reverse=True)
        return runs


# ---------------------------------------------------------------------------
# Init helpers
# ---------------------------------------------------------------------------


def init_automation(
    automations_dir: Optional[Path] = None,
    automation_id: str = "my-automation",
    name: str = "My Copilot Automation",
    force: bool = False,
) -> Path:
    """Create an automation scaffold at ``<automations_dir>/<id>/``.

    Returns the path of the created automation directory.
    """
    target_dir = (automations_dir or default_copilot_automations_dir()) / automation_id
    toml_path = target_dir / _AUTOMATION_TOML
    memory_path = target_dir / _MEMORY_MD

    if toml_path.exists() and not force:
        raise FileExistsError(
            f"{toml_path} already exists. Pass --force to overwrite."
        )

    target_dir.mkdir(parents=True, exist_ok=True)
    toml_path.write_text(
        _AUTOMATION_TOML_TEMPLATE.format(automation_id=automation_id, name=name),
        encoding="utf-8",
    )
    if not memory_path.exists() or force:
        memory_path.write_text(
            _MEMORY_MD_TEMPLATE.format(name=name),
            encoding="utf-8",
        )
    return target_dir
