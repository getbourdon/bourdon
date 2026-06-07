"""Cursor automation participant -- publish background agent run memory as L5 evidence.

Reads the ``~/.cursor/automations/<id>/`` convention:

    automation.toml   -- id, name, status, schedule (rrule), kind, cwds
    memory.md         -- dated bullet entries, one block per run

Each ``automation.toml`` becomes a known Entity; each dated section of
``memory.md`` becomes a recent Session. This covers the federation gap
that the interactive-only ``participants.cursor`` participant leaves behind:
Cursor Cloud Agent background tasks whose work never touches the
interactive SQLite state.
"""

from __future__ import annotations

import logging
import os
import re
import socket
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

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

AGENT_ID = "cursor-automations"
AGENT_TYPE = "other"
ROLE_NARRATIVE = (
    "Publishes read-only Cursor Cloud Agent automation run memory into Bourdon "
    "so background PR reviews, code generation tasks, and scheduled agent runs "
    "become visible alongside interactive Cursor IDE sessions."
)

DEFAULT_POLICY = VisibilityPolicy(
    default=Visibility.TEAM,
    private_tags=["personal", "financial", "credential", "health", "family", "legal"],
    team_tags=["cursor-automation", "automation", "workspace"],
)

_AUTOMATIONS_DIR_NAME = "automations"
_AUTOMATION_TOML = "automation.toml"
_MEMORY_MD = "memory.md"
_MAX_MEMORY_CHARS = 160_000
_MAX_KEY_ACTIONS_PER_RUN = 6
_MAX_KEY_ACTION_CHARS = 280
_RUN_HEADER_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})(?:\b|$)(.*)$")
_PROJECT_HINTS = (
    "ShipStable", "ILTT", "Prun", "PRUN", "OMNIvour", "Castmore",
    "Bourdon", "RADLAB", "CHIP", "Claude Brain", "Cursor", "Copilot",
    "Codex", "Cascade",
)
_SIGNAL_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("human-dashboard-action", re.compile(r"\b(human|ryan|dashboard|manual)\b", re.I)),
    ("release-gate", re.compile(
        r"\b(release|store|app review|play console|testflight)\b", re.I,
    )),
    ("billing-drift", re.compile(
        r"\b(billing|stripe|revenuecat|iap|subscription)\b", re.I,
    )),
    ("memory-coverage-gap", re.compile(
        r"\b(memory|l5|manifest|federated|bourdon)\b", re.I,
    )),
    ("launch-decision", re.compile(
        r"\b(launch|go-live|pricing|prod|production)\b", re.I,
    )),
    ("ci-signal", re.compile(
        r"\b(github action|workflow run|ci|cd|deploy|pipeline)\b", re.I,
    )),
)


@dataclass(frozen=True)
class AutomationConfig:
    automation_id: str
    name: str
    status: str
    rrule: str
    kind: str
    cwds: tuple[str, ...]
    path: Path
    memory_path: Path | None


@dataclass(frozen=True)
class AutomationRun:
    automation: AutomationConfig
    date: str
    title: str
    key_actions: tuple[str, ...]
    projects: tuple[str, ...]
    signals: tuple[str, ...]


@dataclass(frozen=True)
class MergeResult:
    """Summary of a merge_automation_tree call."""

    automations_seen: int
    automations_created: int
    bullets_added: int
    sections_created: int
    skipped: tuple[str, ...]


_BULLET_RE = re.compile(r"^[-*]\s+(.*)$")
_VALID_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def default_cursor_automations_dir(cursor_home: Path | None = None) -> Path:
    """Return the default Cursor automations directory."""
    if cursor_home is not None:
        return cursor_home / _AUTOMATIONS_DIR_NAME
    env = os.environ.get("CURSOR_DIR") or os.environ.get("CURSOR_HOME")
    if env:
        return Path(env) / _AUTOMATIONS_DIR_NAME
    return Path.home() / ".cursor" / _AUTOMATIONS_DIR_NAME


# -- TOML + memory parsing ---------------------------------------------------


def _read_automation_toml(path: Path) -> dict[str, Any]:
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        logger.warning("CursorAutomationsParticipant: cannot parse %s: %s", path, exc)
        return {}


def _build_config(toml_path: Path) -> AutomationConfig | None:
    raw = _read_automation_toml(toml_path)
    automation_id = str(raw.get("id") or toml_path.parent.name).strip()
    if not automation_id:
        return None
    name = str(raw.get("name") or automation_id).strip()
    status = str(raw.get("status") or "UNKNOWN").strip().upper()
    rrule = str(raw.get("rrule") or "").strip()
    kind = str(raw.get("kind") or "").strip()
    cwds_raw = raw.get("cwds") or []
    cwds = tuple(str(cwd) for cwd in cwds_raw if isinstance(cwd, str) and cwd.strip())
    memory_path = toml_path.parent / _MEMORY_MD
    return AutomationConfig(
        automation_id=automation_id, name=name, status=status, rrule=rrule,
        kind=kind, cwds=cwds, path=toml_path,
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


def _read_memory_text(path: Path | None) -> str:
    if path is None:
        return ""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("CursorAutomationsParticipant: cannot read %s: %s", path, exc)
        return ""
    return text[-_MAX_MEMORY_CHARS:]


def _extract_memory_runs(config: AutomationConfig) -> list[AutomationRun]:
    text = _read_memory_text(config.memory_path)
    if not text:
        return []
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
        body = " ".join(actions)
        title = _title_from_actions(config, actions)
        runs.append(AutomationRun(
            automation=config, date=run_date, title=title,
            key_actions=tuple(actions),
            projects=tuple(_infer_projects(body)),
            signals=tuple(_infer_signals(body)),
        ))
    return runs


def _actions_from_lines(lines: list[str]) -> list[str]:
    actions: list[str] = []
    for line in lines:
        cleaned = line.strip()
        if not cleaned:
            continue
        cleaned = cleaned.removeprefix("- ").strip()
        lowered = cleaned.lower()
        if not cleaned or lowered.startswith("runtime"):
            continue
        if lowered in {"first run", "follow-up"}:
            continue
        if lowered.startswith("run:"):
            cleaned = cleaned[4:].strip()
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


def _infer_projects(text: str) -> list[str]:
    projects: list[str] = []
    lowered = text.lower()
    seen: set[str] = set()
    for project in _PROJECT_HINTS:
        key = project.lower()
        if key in lowered and key not in seen:
            projects.append(project)
            seen.add(key)
    return projects


def _infer_signals(text: str) -> list[str]:
    return [name for name, pattern in _SIGNAL_PATTERNS if pattern.search(text)]


def _bounded(value: str, limit: int) -> str:
    normalized = " ".join(value.split())
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 1].rstrip() + "..."


# -- Merge / ingest -----------------------------------------------------------


def _parse_memory_sections(text: str) -> list[tuple[str, list[str]]]:
    """Split memory.md text into (date_header, list_of_bullets) sections."""
    sections: list[tuple[str, list[str]]] = []
    current_date = ""
    current_bullets: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        match = _RUN_HEADER_RE.match(line.strip())
        if match:
            if current_date:
                sections.append((current_date, current_bullets))
            current_date = match.group(1)
            suffix = match.group(2).strip(" -:\u2014")
            current_bullets = []
            if suffix:
                current_bullets.append(suffix)
            continue
        if not current_date:
            continue
        bullet_match = _BULLET_RE.match(line.strip())
        if bullet_match:
            current_bullets.append(bullet_match.group(1).strip())
    if current_date:
        sections.append((current_date, current_bullets))
    return sections


def _serialize_sections(sections: list[tuple[str, list[str]]]) -> str:
    """Render a list of (date, bullets) back to memory.md form."""
    blocks: list[str] = []
    for date_str, bullets in sections:
        lines = [date_str]
        lines.extend(f"- {b}" for b in bullets)
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks) + "\n"


def merge_automation_tree(
    source_dir: Path,
    dest_dir: Path,
    default_kind: str = "cursor-cloud-agent",
) -> MergeResult:
    """Merge an automations tree from ``source_dir`` into ``dest_dir``.

    Idempotent — calling twice on the same source is a no-op.
    """
    if not source_dir.is_dir():
        raise FileNotFoundError(f"merge source not found: {source_dir}")
    dest_dir.mkdir(parents=True, exist_ok=True)

    seen = 0
    created = 0
    bullets_added = 0
    sections_created = 0
    skipped: list[str] = []

    for src_id_dir in sorted(source_dir.iterdir()):
        if not src_id_dir.is_dir():
            continue
        seen += 1
        automation_id = src_id_dir.name
        if not _VALID_ID_RE.match(automation_id):
            skipped.append(automation_id)
            continue

        dest_id_dir = dest_dir / automation_id
        src_toml = src_id_dir / _AUTOMATION_TOML
        src_memory = src_id_dir / _MEMORY_MD

        if not dest_id_dir.exists():
            dest_id_dir.mkdir(parents=True)
            created += 1
            if src_toml.is_file():
                dest_id_dir.joinpath(_AUTOMATION_TOML).write_text(
                    src_toml.read_text(encoding="utf-8"), encoding="utf-8",
                )
            else:
                dest_id_dir.joinpath(_AUTOMATION_TOML).write_text(
                    f'version = 1\nid = "{automation_id}"\n'
                    f'name = "{automation_id}"\nstatus = "ACTIVE"\n'
                    f'kind = "{default_kind}"\nrrule = ""\ncwds = []\n',
                    encoding="utf-8",
                )

        if not src_memory.is_file():
            continue

        src_sections = _parse_memory_sections(
            src_memory.read_text(encoding="utf-8"),
        )
        dest_memory = dest_id_dir / _MEMORY_MD
        dest_sections = (
            _parse_memory_sections(dest_memory.read_text(encoding="utf-8"))
            if dest_memory.is_file() else []
        )
        dest_by_date: dict[str, list[str]] = dict(dest_sections)

        for date_str, bullets in src_sections:
            if date_str not in dest_by_date:
                dest_by_date[date_str] = []
                dest_sections.append((date_str, dest_by_date[date_str]))
                sections_created += 1
            existing = dest_by_date[date_str]
            for bullet in bullets:
                if bullet not in existing:
                    existing.append(bullet)
                    bullets_added += 1

        dest_sections.sort(key=lambda pair: pair[0])
        dest_memory.write_text(
            _serialize_sections(dest_sections), encoding="utf-8",
        )

    return MergeResult(
        automations_seen=seen, automations_created=created,
        bullets_added=bullets_added, sections_created=sections_created,
        skipped=tuple(skipped),
    )


# -- Init helper --------------------------------------------------------------


_INIT_TOML_TEMPLATE = """\
id = "{automation_id}"
name = "{automation_id}"
status = "ACTIVE"
rrule = ""
kind = ""
cwds = []
"""

_INIT_MEMORY_TEMPLATE = """\
# {automation_id}
#
# Append dated sections below. Each YYYY-MM-DD header starts a new run;
# bullets under it become key_actions in the L5 manifest.
#
# Example:
# 2026-06-01
# - Reviewed open PRs in Bourdon and ILTT.
# - No critical issues found.
"""


def init_automations_dir(
    automations_dir: Path | None = None,
    automation_id: str = "cursor-cloud-agent",
    force: bool = False,
) -> Path:
    """Create a starter automation directory with toml + memory.md."""
    base = automations_dir or default_cursor_automations_dir()
    target = base / automation_id
    if target.exists() and not force:
        raise FileExistsError(f"{target} already exists. Pass --force to overwrite.")
    target.mkdir(parents=True, exist_ok=True)
    (target / _AUTOMATION_TOML).write_text(
        _INIT_TOML_TEMPLATE.format(automation_id=automation_id), encoding="utf-8",
    )
    (target / _MEMORY_MD).write_text(
        _INIT_MEMORY_TEMPLATE.format(automation_id=automation_id), encoding="utf-8",
    )
    return target


# -- L5 helpers ---------------------------------------------------------------


def _session_from_run(run: AutomationRun) -> Session:
    config = run.automation
    key_actions = [
        f"automation_id: {config.automation_id}",
        f"run: {run.title}",
        *run.key_actions,
    ]
    files_touched = [str(config.path)]
    if config.memory_path is not None:
        files_touched.append(str(config.memory_path))
    return Session(
        date=run.date,
        cwd=config.cwds[0] if config.cwds else str(config.path.parent),
        project_focus=list(run.projects),
        key_actions=key_actions[: _MAX_KEY_ACTIONS_PER_RUN + 2],
        files_touched=files_touched,
        visibility=Visibility.TEAM,
    )


def _entities_from_configs_and_runs(
    configs: list[AutomationConfig],
    runs: list[AutomationRun],
) -> list[Entity]:
    entities_by_name: dict[str, Entity] = {}
    for config in configs:
        entities_by_name[config.automation_id] = Entity(
            name=config.automation_id, type="automation",
            summary=_bounded(
                f"Cursor automation '{config.name}' ({config.status}). "
                f"Schedule: {config.rrule or 'unspecified'}.", 260,
            ),
            last_touched=None,
            tags=["cursor-automation", "automation", config.status.lower()],
            visibility=Visibility.TEAM,
        )
    for run in runs:
        for project in run.projects:
            entities_by_name.setdefault(project, Entity(
                name=project, type="project",
                summary="Project mentioned by Cursor automation run memory.",
                last_touched=run.date,
                tags=["cursor-automation", "automation-evidence"],
                visibility=Visibility.TEAM,
            ))
        for signal in run.signals:
            entities_by_name.setdefault(signal, Entity(
                name=signal, type="automation-signal",
                summary="Signal class inferred from Cursor automation run memory.",
                last_touched=run.date,
                tags=["cursor-automation", "automation-signal"],
                visibility=Visibility.TEAM,
            ))
    return list(entities_by_name.values())


# -- Participant class --------------------------------------------------------


class CursorAutomationsParticipant:
    """External participant for Cursor Cloud Agent automation memory artifacts."""

    agent_id = AGENT_ID
    agent_type = AGENT_TYPE

    def __init__(
        self,
        automations_dir: Path | None = None,
        cursor_home: Path | None = None,
    ) -> None:
        self._automations_dir = (
            automations_dir or default_cursor_automations_dir(cursor_home)
        )
        self._policy = DEFAULT_POLICY

    @property
    def native_path(self) -> str:
        return str(self._automations_dir)

    def discover(self) -> AgentStore:
        if not self._automations_dir.is_dir():
            raise ParticipantDiscoveryError(
                f"Cursor automations directory not found at {self._automations_dir}.",
            )
        configs = _iter_configs(self._automations_dir)
        return AgentStore(
            path=str(self._automations_dir), version="unknown",
            metadata={
                "automations": len(configs),
                "with_memory": sum(
                    1 for c in configs if c.memory_path is not None
                ),
            },
        )

    def export_sessions(self, since: datetime, limit: int = 100) -> list[Session]:
        sessions = [_session_from_run(r) for r in self._runs(since=since)]
        sessions.sort(key=lambda s: s.date, reverse=True)
        return sessions[:limit]

    def export_l5(self, since: datetime | None = None) -> L5Manifest:
        configs = _iter_configs(self._automations_dir)
        runs = self._runs(configs=configs, since=since)
        sessions = [_session_from_run(r) for r in runs]
        sessions.sort(key=lambda s: s.date, reverse=True)
        entities = _entities_from_configs_and_runs(configs, runs)
        visible_entities = filter_for_federation(entities, self._policy)
        return L5Manifest(
            spec_version=SPEC_VERSION,
            agent=AgentInfo(
                id=AGENT_ID, type=AGENT_TYPE,
                instance=socket.gethostname(),
                spec_version_compat=SPEC_VERSION,
                role_narrative=ROLE_NARRATIVE,
            ),
            last_updated=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            capabilities=["cursor-automation-memory", "run-summary-publication"],
            recent_sessions=sessions,
            known_entities=visible_entities,
            visibility_policy=self._policy,
        )

    def health_check(self) -> HealthStatus:
        if not self._automations_dir.is_dir():
            return HealthStatus(
                status="blocked",
                reason=(
                    f"Cursor automations directory not found at "
                    f"{self._automations_dir}."
                ),
                details={"automations_dir": str(self._automations_dir)},
                proposed_fix="Create Cursor automations or pass --automations-dir.",
            )
        configs = _iter_configs(self._automations_dir)
        runs = self._runs(configs=configs)
        status = "ok" if configs else "degraded"
        reason = None if configs else "No automation.toml files found."
        return HealthStatus(
            status=status, reason=reason,
            details={
                "automations_dir": str(self._automations_dir),
                "automation_count": len(configs),
                "memory_files": sum(
                    1 for c in configs if c.memory_path is not None
                ),
                "runs_extracted": len(runs),
                "active_automations": sum(
                    1 for c in configs if c.status == "ACTIVE"
                ),
            },
            proposed_fix=None if configs else "Add Cursor automation.toml files.",
        )

    def _runs(
        self,
        configs: list[AutomationConfig] | None = None,
        since: datetime | None = None,
    ) -> list[AutomationRun]:
        run_cutoff = (
            since.astimezone(timezone.utc).date().isoformat() if since else None
        )
        runs: list[AutomationRun] = []
        for config in configs or _iter_configs(self._automations_dir):
            for run in _extract_memory_runs(config):
                if run_cutoff and run.date < run_cutoff:
                    continue
                runs.append(run)
        runs.sort(
            key=lambda r: (r.date, r.automation.automation_id), reverse=True,
        )
        return runs
