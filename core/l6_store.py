"""
Bourdon L6 -- Federation Library Store.

L6 is the cross-agent federation layer. It aggregates L5 manifests from all
installed agents (Claude Code, Codex, Clyde, etc.) and provides query
primitives any agent can call to discover context the others have captured.

This module holds the pure-Python store. It has no MCP dependency -- the
:mod:`core.l6_server` module wraps this store in a fastmcp server.

Store layout on disk::

    ~/agent-library/
    +-- agents/
    |   +-- claude-code.l5.yaml
    |   +-- codex.l5.yaml
    |   +-- clyde.l5.yaml
    |   ...

Each `*.l5.yaml` file is an L5 manifest produced by an adapter. See
``spec/L5_schema.json`` for the manifest schema.

Design invariants
-----------------
- Query surfaces default to ``access_level="public"``. That means TEAM and
  PRIVATE rows stay hidden unless a caller explicitly asks for broader access.
- ``include_private`` remains as a one-release compatibility shim:
  ``False -> public`` and ``True -> private``.
- Malformed manifests are skipped with a warning; one broken file cannot
  take down the whole store.
- Reload is on-demand (``reload_agent`` / ``reload_all``). File-watching is
  out of scope for v0.0.5 and tracked separately.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)


DEFAULT_LIBRARY_PATH = Path.home() / "agent-library"


# -- Result types --------------------------------------------------------------


@dataclass
class EntityMatch:
    """One entity, with the agent(s) that know about it."""

    name: str
    agents: list[str] = field(default_factory=list)
    types: list[str] = field(default_factory=list)
    summaries: dict[str, str] = field(default_factory=dict)  # agent_id -> summary
    tags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "agents": self.agents,
            "types": self.types,
            "summaries": self.summaries,
            "tags": self.tags,
        }


@dataclass
class SessionRef:
    """One session from one agent's recent_sessions list, keyed back to the agent."""

    agent: str
    date: str
    cwd: str | None = None
    project_focus: list[str] = field(default_factory=list)
    key_actions: list[str] = field(default_factory=list)
    files_touched: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent": self.agent,
            "date": self.date,
            "cwd": self.cwd,
            "project_focus": self.project_focus,
            "key_actions": self.key_actions,
            "files_touched": self.files_touched,
        }


@dataclass
class ProjectSummary:
    """Cross-agent rollup for a single project / entity."""

    project: str
    agents: list[str] = field(default_factory=list)
    recent_sessions: list[SessionRef] = field(default_factory=list)
    entities: list[EntityMatch] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "project": self.project,
            "agents": self.agents,
            "recent_sessions": [s.to_dict() for s in self.recent_sessions],
            "entities": [e.to_dict() for e in self.entities],
        }


# -- Visibility helpers (module-local, independent of adapters.base) ----------


def _entity_visibility(entity: dict) -> str:
    """Return the entity's declared visibility, defaulting to PUBLIC."""
    if not isinstance(entity, dict):
        return "public"
    explicit = entity.get("visibility")
    if isinstance(explicit, str):
        return explicit.lower()
    return "public"


def _session_visibility(session: dict) -> str:
    """Return the session's declared visibility, defaulting to PUBLIC."""
    if not isinstance(session, dict):
        return "public"
    explicit = session.get("visibility")
    if isinstance(explicit, str):
        return explicit.lower()
    return "public"


def _resolve_access_level(
    include_private: bool = False, access_level: str | None = None
) -> str:
    """Resolve access level, keeping `include_private` as a compatibility shim."""
    if access_level is None:
        return "private" if include_private else "public"
    normalized = access_level.strip().lower()
    if normalized not in {"public", "team", "private"}:
        raise ValueError(f"unsupported access_level: {access_level}")
    return normalized


def _visibility_rank(value: str) -> int:
    return {"public": 0, "team": 1, "private": 2}[value]


def _is_visible(thing: dict, access_level: str) -> bool:
    """Return True if this entity/session is visible at the given access level."""
    vis = _entity_visibility(thing) if "name" in thing else _session_visibility(thing)
    normalized_vis = vis if vis in {"public", "team", "private"} else "public"
    return _visibility_rank(normalized_vis) <= _visibility_rank(access_level)


# -- Store ---------------------------------------------------------------------


class L6Store:
    """
    Filesystem-backed L6 federation store.

    Parameters
    ----------
    library_path : Path, optional
        Directory containing ``agents/*.l5.yaml`` files. Defaults to
        ``~/agent-library/``. The ``agents/`` subdirectory is created on
        first load if missing (makes the store usable immediately after
        ``neurolayer init`` creates the parent dir).
    """

    def __init__(self, library_path: Path | None = None) -> None:
        self.library_path = Path(library_path) if library_path else DEFAULT_LIBRARY_PATH
        self._manifests: dict[str, dict] = {}
        self._entity_index: dict[str, set[str]] = defaultdict(set)
        self.reload_all()

    # -- Load / reload ---------------------------------------------------------

    def _agents_dir(self) -> Path:
        return self.library_path / "agents"

    def reload_all(self) -> None:
        """Re-read every `*.l5.yaml` file in the agents directory."""
        self._manifests.clear()
        self._entity_index.clear()
        agents_dir = self._agents_dir()
        if not agents_dir.is_dir():
            return
        for path in sorted(agents_dir.glob("*.l5.yaml")):
            self._load_one(path)

    def reload_agent(self, agent_id: str) -> bool:
        """Re-read one agent's manifest. Returns True if found and loaded."""
        path = self._agents_dir() / f"{agent_id}.l5.yaml"
        if not path.is_file():
            # Agent removed -- drop from memory
            self._drop_agent(agent_id)
            return False
        self._drop_agent(agent_id)
        return self._load_one(path)

    def _drop_agent(self, agent_id: str) -> None:
        """Remove an agent from the in-memory index."""
        self._manifests.pop(agent_id, None)
        # Rebuild affected index entries
        stale_keys = [k for k, v in self._entity_index.items() if agent_id in v]
        for k in stale_keys:
            self._entity_index[k].discard(agent_id)
            if not self._entity_index[k]:
                del self._entity_index[k]

    def _load_one(self, path: Path) -> bool:
        """Load one L5 manifest. Returns True on success, False on any error."""
        try:
            with open(path, encoding="utf-8") as f:
                data = yaml.safe_load(f)
        except (yaml.YAMLError, OSError) as e:
            logger.warning("Failed to load L5 manifest %s: %s", path, e)
            return False
        if not isinstance(data, dict):
            logger.warning("L5 manifest %s is not a dict, skipping", path)
            return False

        agent = data.get("agent") or {}
        agent_id = agent.get("id")
        if not agent_id:
            # Fall back to filename (strip .l5.yaml) so bare files still load
            agent_id = path.stem.replace(".l5", "")
        self._manifests[agent_id] = data

        # Build / update entity index
        for entity in data.get("known_entities") or []:
            if not isinstance(entity, dict):
                continue
            name = entity.get("name")
            if not isinstance(name, str) or not name.strip():
                continue
            self._entity_index[name.strip().lower()].add(agent_id)
            for alias in entity.get("aliases") or []:
                if isinstance(alias, str) and alias.strip():
                    self._entity_index[alias.strip().lower()].add(agent_id)
        return True

    # -- Query primitives ------------------------------------------------------

    def list_agents(self) -> list[str]:
        """Return sorted list of agent IDs known to the store."""
        return sorted(self._manifests.keys())

    def get_agent_manifest(
        self,
        agent_id: str,
        include_private: bool = False,
        access_level: str | None = None,
    ) -> dict | None:
        """
        Return a copy of the agent's manifest, with private entities /
        sessions filtered unless ``include_private=True``.

        Returns None if the agent is not in the store.
        """
        manifest = self._manifests.get(agent_id)
        if manifest is None:
            return None
        resolved_access = _resolve_access_level(
            include_private=include_private,
            access_level=access_level,
        )
        filtered = dict(manifest)
        filtered["known_entities"] = [
            e
            for e in manifest.get("known_entities") or []
            if isinstance(e, dict) and _is_visible(e, resolved_access)
        ]
        filtered["recent_sessions"] = [
            s
            for s in manifest.get("recent_sessions") or []
            if isinstance(s, dict) and _is_visible(s, resolved_access)
        ]
        return filtered

    def build_recognition_manifest(
        self,
        include_private: bool = False,
        access_level: str | None = None,
    ) -> dict[str, Any]:
        """
        Build a visibility-filtered manifest for recognition-time matching.

        The result is intentionally small: known entities are merged across
        agents by name/type, with aliases and source-agent summaries preserved.
        It is shaped like an L5 manifest so ``recognition_first`` can consume it
        directly without knowing about the federation store.
        """
        resolved_access = _resolve_access_level(
            include_private=include_private,
            access_level=access_level,
        )
        entities_by_key: dict[tuple[str, str], dict[str, Any]] = {}
        recent_sessions: list[dict[str, Any]] = []

        for agent_id, manifest in sorted(self._manifests.items()):
            for entity in manifest.get("known_entities") or []:
                if not isinstance(entity, dict):
                    continue
                if not _is_visible(entity, resolved_access):
                    continue
                name = entity.get("name")
                if not isinstance(name, str) or not name.strip():
                    continue

                entity_type = str(entity.get("type") or "topic")
                key = (name.strip().lower(), entity_type.lower())
                merged = entities_by_key.setdefault(
                    key,
                    {
                        "name": name.strip(),
                        "type": entity_type,
                        "aliases": [],
                        "summary": str(entity.get("summary") or ""),
                        "summaries": {},
                        "source_agents": [],
                        "tags": [],
                        "visibility": resolved_access,
                    },
                )

                _append_unique(merged["source_agents"], agent_id)
                for alias in entity.get("aliases") or []:
                    if isinstance(alias, str) and alias.strip():
                        _append_unique(merged["aliases"], alias.strip())
                for tag in entity.get("tags") or []:
                    if isinstance(tag, str) and tag.strip():
                        _append_unique(merged["tags"], tag.strip())

                summary = entity.get("summary")
                if isinstance(summary, str) and summary.strip():
                    if not merged.get("summary"):
                        merged["summary"] = summary.strip()
                    merged["summaries"][agent_id] = summary.strip()

            for session in manifest.get("recent_sessions") or []:
                if not isinstance(session, dict):
                    continue
                if not _is_visible(session, resolved_access):
                    continue
                session_copy = dict(session)
                session_copy["agent"] = agent_id
                recent_sessions.append(session_copy)

        recent_sessions.sort(
            key=lambda session: str(session.get("date") or ""),
            reverse=True,
        )
        return {
            "spec_version": "0.1",
            "agent": {"id": "bourdon-l6", "type": "federation"},
            "last_updated": datetime.now(timezone.utc).isoformat(),
            "known_entities": list(entities_by_key.values()),
            "recent_sessions": recent_sessions,
        }

    def find_entity(
        self,
        name: str,
        include_private: bool = False,
        access_level: str | None = None,
    ) -> list[EntityMatch]:
        """
        Look up an entity by name (case-insensitive). Returns a list of
        matches -- usually one, but could be multiple if different entities
        share a name across agents.

        Each returned :class:`EntityMatch` aggregates every agent that knows
        about the entity and carries each agent's per-agent summary.
        """
        key = name.strip().lower()
        if not key:
            return []
        resolved_access = _resolve_access_level(
            include_private=include_private,
            access_level=access_level,
        )
        agent_ids = sorted(self._entity_index.get(key, set()))
        if not agent_ids:
            return []

        # Group matching entity dicts by their exact-cased name so we can
        # distinguish, e.g., the entity "ILTT" from some other entity that
        # happens to share the lowercased string (rare but worth preserving).
        by_exact_name: dict[str, EntityMatch] = {}
        for agent_id in agent_ids:
            manifest = self._manifests.get(agent_id) or {}
            for entity in manifest.get("known_entities") or []:
                if not isinstance(entity, dict):
                    continue
                ent_name = (entity.get("name") or "").strip()
                if not ent_name or ent_name.lower() != key:
                    # Check aliases
                    aliases = entity.get("aliases") or []
                    if not any(
                        isinstance(a, str) and a.strip().lower() == key
                        for a in aliases
                    ):
                        continue
                if not _is_visible(entity, resolved_access):
                    continue
                match = by_exact_name.setdefault(
                    ent_name, EntityMatch(name=ent_name)
                )
                if agent_id not in match.agents:
                    match.agents.append(agent_id)
                if entity.get("type"):
                    t = str(entity["type"])
                    if t not in match.types:
                        match.types.append(t)
                summary = entity.get("summary")
                if summary:
                    match.summaries[agent_id] = str(summary)
                for tag in entity.get("tags") or []:
                    if isinstance(tag, str) and tag not in match.tags:
                        match.tags.append(tag)
        return list(by_exact_name.values())

    def list_recent_work(
        self,
        since: datetime | None = None,
        agent: str | None = None,
        include_private: bool = False,
        access_level: str | None = None,
    ) -> list[SessionRef]:
        """
        Flatten sessions from one or all agents into a unified list.

        Parameters
        ----------
        since : datetime, optional
            Drop sessions whose ``date`` is earlier than this. ``date`` is
            ISO 8601 date-only; we compare against ``since.date()``.
        agent : str, optional
            Limit to this agent's sessions. If None, include all agents.
        include_private : bool
            Whether to include PRIVATE-tagged sessions. Default False.
        """
        cutoff: date | None = since.date() if since is not None else None
        resolved_access = _resolve_access_level(
            include_private=include_private,
            access_level=access_level,
        )
        results: list[SessionRef] = []
        manifests = (
            {agent: self._manifests[agent]}
            if agent and agent in self._manifests
            else self._manifests
        )
        for agent_id, manifest in manifests.items():
            for session in manifest.get("recent_sessions") or []:
                if not isinstance(session, dict):
                    continue
                if not _is_visible(session, resolved_access):
                    continue
                session_date = session.get("date")
                if cutoff is not None and isinstance(session_date, str):
                    try:
                        parsed = date.fromisoformat(session_date)
                    except ValueError:
                        parsed = None
                    if parsed is None or parsed < cutoff:
                        continue
                results.append(
                    SessionRef(
                        agent=agent_id,
                        date=str(session_date or ""),
                        cwd=session.get("cwd"),
                        project_focus=list(session.get("project_focus") or []),
                        key_actions=list(session.get("key_actions") or []),
                        files_touched=list(session.get("files_touched") or []),
                    )
                )
        results.sort(key=lambda s: s.date, reverse=True)
        return results

    def get_cross_agent_summary(
        self,
        project: str,
        include_private: bool = False,
        access_level: str | None = None,
    ) -> ProjectSummary:
        """
        Roll up everything the federation knows about a project (or entity).

        Aggregates entity matches + sessions whose ``project_focus`` lists
        the project. Useful for questions like "give me everything about
        ILTT across my agents."
        """
        key = project.strip()
        lowered = key.lower()
        resolved_access = _resolve_access_level(
            include_private=include_private,
            access_level=access_level,
        )
        entities = self.find_entity(
            key,
            include_private=include_private,
            access_level=resolved_access,
        )
        sessions: list[SessionRef] = []
        agent_set: set[str] = set()
        for e in entities:
            agent_set.update(e.agents)
        for agent_id, manifest in self._manifests.items():
            for session in manifest.get("recent_sessions") or []:
                if not isinstance(session, dict):
                    continue
                focus = session.get("project_focus") or []
                if not any(
                    isinstance(p, str) and p.strip().lower() == lowered for p in focus
                ):
                    continue
                if not _is_visible(session, resolved_access):
                    continue
                sessions.append(
                    SessionRef(
                        agent=agent_id,
                        date=str(session.get("date") or ""),
                        cwd=session.get("cwd"),
                        project_focus=list(focus),
                        key_actions=list(session.get("key_actions") or []),
                        files_touched=list(session.get("files_touched") or []),
                    )
                )
                agent_set.add(agent_id)
        sessions.sort(key=lambda s: s.date, reverse=True)
        return ProjectSummary(
            project=key,
            agents=sorted(agent_set),
            recent_sessions=sessions,
            entities=entities,
        )


def _append_unique(values: list[str], value: str) -> None:
    if value not in values:
        values.append(value)
