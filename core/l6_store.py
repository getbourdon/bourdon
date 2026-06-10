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

Each `*.l5.yaml` file is an L5 manifest produced by a participant. See
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

import base64
import json
import logging
import os
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

# Forward reference: federation client. Imported under TYPE_CHECKING only so
# the store has no hard runtime dep on the MCP SDK / httpx.
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from core.l6_remote import RemoteL6Client


DEFAULT_LIBRARY_PATH = Path.home() / "agent-library"

# Pagination bounds for list_recent_work.
# - DEFAULT_LIMIT shipped at 20 to keep first-call payloads cheap; this is the
#   number that prevented the Layer 3 acceptance stall in Claude Desktop where
#   an unbounded 83-session response was choking the chat UI on serialize +
#   context inject.
# - MAX_LIMIT caps explicit asks to keep a hostile caller from pulling the
#   whole store in one go.
# - DEFAULT_SINCE_DAYS is the time-window throttle when the caller passes
#   neither `since` nor `cursor`. Callers who genuinely want everything pass
#   `since=2020-01-01` (or earlier) explicitly.
DEFAULT_LIMIT = 20
MAX_LIMIT = 100
DEFAULT_SINCE_DAYS = 14


def _encode_cursor(offset: int) -> str:
    """Encode a pagination position as an opaque base64 token.

    The cursor encodes an integer offset into the post-sort, post-filter
    session list. Sessions are sorted newest-first, deterministically
    within the same date by (agent, source index). The opaque format is
    base64-encoded JSON ``{"offset": N}`` so it stays both URL-safe and
    debuggable if anyone needs to look at one.

    Stale-cursor caveat: ``L6Store`` reload between paginated calls can
    shift offsets. For a single-user federation paginating quickly
    through a stable snapshot, this is fine. Callers paginating across
    reloads should re-issue the first call.
    """
    payload = json.dumps({"offset": offset}, separators=(",", ":"))
    return base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii")


def _decode_cursor(cursor: str | None) -> int:
    """Decode a cursor back to its offset. Returns 0 for None.

    Raises ``ValueError`` if the cursor is non-empty and unreadable -- the
    caller will surface that to the MCP client rather than silently
    pretending it was a fresh first page.
    """
    if cursor is None or cursor == "":
        return 0
    try:
        raw = base64.urlsafe_b64decode(cursor.encode("ascii"))
        data = json.loads(raw)
        offset = int(data["offset"])
        if offset < 0:
            raise ValueError("negative offset")
        return offset
    except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid cursor: {cursor!r}") from exc


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

    def to_dict(self, summary: bool = False) -> dict[str, Any]:
        """Serialize. ``summary=True`` omits the narrative-weight fields
        (``key_actions`` and ``files_touched``) for lightweight callers
        that only need the timeline shape."""
        base: dict[str, Any] = {
            "agent": self.agent,
            "date": self.date,
            "cwd": self.cwd,
            "project_focus": self.project_focus,
        }
        if not summary:
            base["key_actions"] = self.key_actions
            base["files_touched"] = self.files_touched
        return base


@dataclass
class PaginatedSessions:
    """Result of a paginated :meth:`L6Store.list_recent_work` call.

    ``sessions`` is one page of newest-first results. ``next_cursor`` is
    the opaque token to pass back for the next page, or ``None`` when
    this is the last page. ``has_more`` is the boolean form of the same
    signal for clients that prefer not to inspect the cursor.
    """

    sessions: list[SessionRef] = field(default_factory=list)
    next_cursor: str | None = None
    has_more: bool = False

    def to_dict(self, summary: bool = False) -> dict[str, Any]:
        return {
            "sessions": [s.to_dict(summary=summary) for s in self.sessions],
            "next_cursor": self.next_cursor,
            "has_more": self.has_more,
        }

    # Lightweight iteration / indexing / len support so call sites that just
    # want the session list keep working without explicit `.sessions` access.
    def __iter__(self):
        return iter(self.sessions)

    def __len__(self) -> int:
        return len(self.sessions)

    def __getitem__(self, index):
        return self.sessions[index]


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


# -- Visibility helpers (module-local, independent of participants.base) ----------


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
    """Resolve access level, keeping `include_private` as a compatibility shim.

    Default is ``public`` (most restrictive view of federation content).
    For single-user federations where the user trusts their own agents,
    set ``BOURDON_DEFAULT_ACCESS_LEVEL=team`` (or ``private``) in the
    environment. Without this override, three of five shipping participants
    (Codex always, Copilot + Cursor by default policy) tag entities
    ``team``, which are silently filtered from default ``public`` queries.
    Finding #2 from the Layer 1 federation work.

    Precedence:
      1. Explicit `access_level=` argument
      2. `include_private=True` -> "private"
      3. `BOURDON_DEFAULT_ACCESS_LEVEL` env var
      4. "public" (existing default)
    """
    if access_level is not None:
        normalized = access_level.strip().lower()
        if normalized not in {"public", "team", "private"}:
            raise ValueError(f"unsupported access_level: {access_level}")
        return normalized
    if include_private:
        return "private"
    env_default = os.environ.get("BOURDON_DEFAULT_ACCESS_LEVEL")
    if env_default:
        normalized = env_default.strip().lower()
        if normalized in {"public", "team", "private"}:
            return normalized
        logger.warning(
            "BOURDON_DEFAULT_ACCESS_LEVEL=%r is not public/team/private; "
            "falling back to public.",
            env_default,
        )
    return "public"


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

    def __init__(
        self,
        library_path: Path | None = None,
        peers: list["RemoteL6Client"] | None = None,
    ) -> None:
        self.library_path = Path(library_path) if library_path else DEFAULT_LIBRARY_PATH
        self.peers: list["RemoteL6Client"] = list(peers or [])
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
        agents by **name only** (case-insensitive), with aliases, types,
        and source-agent summaries preserved. It is shaped like an L5
        manifest so ``recognition_first`` can consume it directly without
        knowing about the federation store.

        Each merged entity row carries:
            - ``type``: canonical (first-seen) type for backward compat
            - ``types``: full list of types seen across agents (Finding #1)
            - ``source_agents``: every agent that contributed to this row
            - ``summaries``: per-agent summary strings keyed by agent_id

        Finding #1 from Layer 1 federation work: previously dedupe was by
        ``(name, type)``, so Cursor's inferred ``project`` and Copilot's
        ``concept`` for the same name produced two rows. Now they
        produce one row whose ``types`` lists both, matching the
        same-name-is-same-entity intuition.

        Finding #3 from Layer 1: this also brings ``build_recognition_manifest``
        in line with ``find_entity``, which already dedupes by name only.
        The two surfaces no longer disagree on row counts.
        """
        resolved_access = _resolve_access_level(
            include_private=include_private,
            access_level=access_level,
        )
        entities_by_key: dict[str, dict[str, Any]] = {}
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
                key = name.strip().lower()
                merged = entities_by_key.setdefault(
                    key,
                    {
                        "name": name.strip(),
                        # type stays as the first-seen value for backward
                        # compatibility (recognition_runtime + downstream
                        # consumers read entity["type"] as a single string).
                        "type": entity_type,
                        # types is the new federation-aware field: every
                        # distinct type any agent applied to this name.
                        "types": [],
                        "aliases": [],
                        "summary": str(entity.get("summary") or ""),
                        "summaries": {},
                        "source_agents": [],
                        "tags": [],
                        "visibility": resolved_access,
                    },
                )

                _append_unique(merged["types"], entity_type)
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
        limit: int | None = None,
        cursor: str | None = None,
    ) -> PaginatedSessions:
        """
        Flatten sessions from one or all agents into a unified, paginated list.

        Returns a :class:`PaginatedSessions` (iterable + len-aware) so simple
        callers can ``for s in result:`` while pagination-aware callers read
        ``result.next_cursor`` / ``result.has_more``.

        Parameters
        ----------
        since : datetime, optional
            Drop sessions whose ``date`` is earlier than this. When both
            ``since`` and ``cursor`` are ``None`` the store applies a
            14-day default window (``DEFAULT_SINCE_DAYS``) to keep first
            calls from pulling the entire store. Callers who genuinely
            want everything pass an explicit ``since`` far in the past.
        agent : str, optional
            Filter to one agent's sessions.
        include_private : bool
            Compatibility shim: ``True`` is equivalent to ``access_level="private"``.
        access_level : str, optional
            ``public`` / ``team`` / ``private``. See class docstring for invariants.
        limit : int, optional
            Max sessions to return for this page. Defaults to
            ``DEFAULT_LIMIT`` (20); caps at ``MAX_LIMIT`` (100). Values
            below 1 are coerced to 1.
        cursor : str, optional
            Opaque token from a previous call's ``next_cursor`` field.
            When present, the default-since window is NOT applied -- the
            caller is responsible for re-passing ``since`` if they want
            to keep the same filter across pages.
        """
        # Apply the default since window only on a fresh first call, not
        # mid-pagination. Pagination relies on caller passing the same
        # filter args; if they only pass cursor, we don't want to re-apply
        # the default-since on top of an in-flight cursor offset.
        if since is None and cursor is None:
            since = datetime.now(timezone.utc) - timedelta(days=DEFAULT_SINCE_DAYS)

        # Normalize limit -- clamp [1, MAX_LIMIT].
        effective_limit = DEFAULT_LIMIT if limit is None else int(limit)
        if effective_limit < 1:
            effective_limit = 1
        if effective_limit > MAX_LIMIT:
            effective_limit = MAX_LIMIT

        cutoff: date | None = since.date() if since is not None else None
        resolved_access = _resolve_access_level(
            include_private=include_private,
            access_level=access_level,
        )
        all_results: list[SessionRef] = []
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
                all_results.append(
                    SessionRef(
                        agent=agent_id,
                        date=str(session_date or ""),
                        cwd=session.get("cwd"),
                        project_focus=list(session.get("project_focus") or []),
                        key_actions=list(session.get("key_actions") or []),
                        files_touched=list(session.get("files_touched") or []),
                    )
                )
        # Stable sort: primary key date (newest-first), secondary key agent
        # (lexicographic) so same-date sessions paginate in a predictable
        # order. Cursor reliability depends on this ordering being stable
        # across reloads of the same store contents.
        all_results.sort(key=lambda s: (s.date, s.agent), reverse=True)

        # Slice the requested page off.
        offset = _decode_cursor(cursor)
        page = all_results[offset : offset + effective_limit]
        next_offset = offset + len(page)
        has_more = next_offset < len(all_results)
        next_cursor = _encode_cursor(next_offset) if has_more else None

        return PaginatedSessions(
            sessions=page,
            next_cursor=next_cursor,
            has_more=has_more,
        )

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

    # ------------------------------------------------------------------
    # Federated async surfaces (Phase 1.6)
    # ------------------------------------------------------------------
    #
    # These mirror the sync query methods above but, when ``self.peers`` is
    # non-empty, fan out to each peer via ``asyncio.gather`` and merge the
    # results back together.
    #
    # Merge rules (locked in ROADMAP Phase 1.6):
    # - Entities dedupe by ``name.lower()``; newest ``last_touched`` wins on
    #   conflicting summary/type. Agent lists, tags, summaries are unioned.
    # - Sessions dedupe by ``(date, cwd, agent)`` and unioned.
    # - All list-typed scalar fields (tags, aliases, project_focus,
    #   key_actions, files_touched) union on dedupe.
    #
    # Peer failures are logged and silently dropped — federation must
    # degrade gracefully when one peer is offline.

    async def list_agents_federated(self) -> list[str]:
        """Local + peer agent IDs, unioned and sorted."""
        agents: set[str] = set(self.list_agents())
        if not self.peers:
            return sorted(agents)
        import asyncio

        peer_results = await asyncio.gather(
            *(peer.list_agents() for peer in self.peers),
            return_exceptions=True,
        )
        for peer, result in zip(self.peers, peer_results):
            if isinstance(result, Exception):
                logger.warning("peer %s list_agents raised: %s", peer.name, result)
                continue
            for a in result or []:
                if isinstance(a, str):
                    agents.add(a)
        return sorted(agents)

    async def export_agents_federated(
        self,
        local_name: str | None = None,
    ) -> dict:
        """Source-attributed export of local + peer agents for the desktop tray.

        Produces the multi-machine ``bourdon.agents/v1`` envelope: this
        machine's own agents (``source_kind="local"``) plus each peer's
        ``export_agents`` output, with every peer agent's ``source`` re-tagged
        caller-side to the peer's configured name (``source_kind="peer"``).
        The peer's self-reported ``machine`` / per-agent ``source`` is never
        trusted -- this is what prevents the bidirectional-federation echo (the
        Mac federating the PC's agents back to the PC) and source spoofing.

        Agents dedupe by ``(source, id)`` so a same-named agent that exists on
        two machines (e.g. ``claude-code`` on both) is kept as two distinct
        rows, one per source.

        A peer that raises, returns ``None``, returns no agents, or is too old
        to expose ``export_agents`` contributes nothing and is reported with
        ``reachable: false`` / ``agent_count: 0`` -- never crashes the export.
        The local source is always ``reachable: true``.

        Parameters
        ----------
        local_name : str, optional
            Machine label for this server's own agents. Defaults to the
            resolved local name (``BOURDON_LOCAL_NAME`` or hostname).

        Returns
        -------
        dict
            ``{"schema", "agents": [...], "sources": [...]}`` where each source
            is ``{"name", "kind", "reachable", "agent_count"}``.
        """
        from core.agents_export import (
            AGENTS_SCHEMA,
            export_local_agents,
            resolve_local_name,
        )

        machine = local_name or resolve_local_name()
        local_export = export_local_agents(self._agents_dir(), machine)
        local_agents = list(local_export.get("agents") or [])

        merged: dict[tuple[str, str], dict] = {}

        def _ingest(agent: dict, source: str, source_kind: str) -> None:
            tagged = dict(agent)
            tagged["source"] = source
            tagged["source_kind"] = source_kind
            key = (source, str(tagged.get("id") or ""))
            merged.setdefault(key, tagged)

        for agent in local_agents:
            _ingest(agent, machine, "local")

        sources: list[dict] = [
            {
                "name": machine,
                "kind": "local",
                "reachable": True,
                "agent_count": len(local_agents),
            }
        ]

        if self.peers:
            import asyncio

            peer_results = await asyncio.gather(
                *(peer.export_agents() for peer in self.peers),
                return_exceptions=True,
            )
            for peer, payload in zip(self.peers, peer_results, strict=False):
                if isinstance(payload, Exception):
                    logger.warning(
                        "peer %s export_agents raised: %s", peer.name, payload
                    )
                    sources.append(
                        {
                            "name": peer.name,
                            "kind": "peer",
                            "reachable": False,
                            "agent_count": 0,
                        }
                    )
                    continue
                peer_agents = (
                    payload.get("agents")
                    if isinstance(payload, dict)
                    else None
                )
                if not peer_agents:
                    # None / empty / missing tool (un-upgraded peer) -> unreachable.
                    sources.append(
                        {
                            "name": peer.name,
                            "kind": "peer",
                            "reachable": False,
                            "agent_count": 0,
                        }
                    )
                    continue
                count = 0
                for agent in peer_agents:
                    if not isinstance(agent, dict):
                        continue
                    _ingest(agent, peer.name, "peer")
                    count += 1
                sources.append(
                    {
                        "name": peer.name,
                        "kind": "peer",
                        "reachable": True,
                        "agent_count": count,
                    }
                )

        agents = list(merged.values())
        agents.sort(key=lambda a: (a.get("last_updated") or ""), reverse=True)

        return {
            "schema": AGENTS_SCHEMA,
            "agents": agents,
            "sources": sources,
        }

    async def find_entity_federated(
        self,
        name: str,
        include_private: bool = False,
        access_level: str | None = None,
    ) -> list[EntityMatch]:
        """Local + peer entity matches, deduped by name and unioned across agents."""
        local = self.find_entity(name, include_private=include_private, access_level=access_level)
        if not self.peers:
            return local
        import asyncio

        peer_results = await asyncio.gather(
            *(
                peer.find_entity(
                    name,
                    access_level=access_level or ("private" if include_private else "public"),
                    include_private=include_private,
                )
                for peer in self.peers
            ),
            return_exceptions=True,
        )
        # Dedupe by entity .name (case-insensitive), union .agents / .types /
        # .tags / .summaries.
        by_key: dict[str, EntityMatch] = {m.name.lower(): m for m in local}
        for peer, peer_payload in zip(self.peers, peer_results):
            if isinstance(peer_payload, Exception):
                logger.warning("peer %s find_entity raised: %s", peer.name, peer_payload)
                continue
            for entry in peer_payload or []:
                if not isinstance(entry, dict):
                    continue
                ent_name = (entry.get("name") or "").strip()
                if not ent_name:
                    continue
                key = ent_name.lower()
                target = by_key.get(key)
                if target is None:
                    target = EntityMatch(name=ent_name)
                    by_key[key] = target
                # Tag agents with "peer:<name>" prefix to keep provenance clear.
                for a in entry.get("agents") or []:
                    if isinstance(a, str):
                        tagged = a if a.startswith("peer:") else f"peer:{peer.name}:{a}"
                        if tagged not in target.agents:
                            target.agents.append(tagged)
                for t in entry.get("types") or []:
                    if isinstance(t, str) and t not in target.types:
                        target.types.append(t)
                for tag in entry.get("tags") or []:
                    if isinstance(tag, str) and tag not in target.tags:
                        target.tags.append(tag)
                for agent_id, summary in (entry.get("summaries") or {}).items():
                    if isinstance(agent_id, str) and isinstance(summary, str):
                        tagged = f"peer:{peer.name}:{agent_id}"
                        target.summaries.setdefault(tagged, summary)
        return list(by_key.values())

    async def list_recent_work_federated(
        self,
        since: datetime | None = None,
        agent: str | None = None,
        include_private: bool = False,
        access_level: str | None = None,
        limit: int | None = None,
        cursor: str | None = None,
    ) -> "PaginatedSessions":
        """Local + peer sessions, deduped by (date, cwd, agent)."""
        local = self.list_recent_work(
            since=since,
            agent=agent,
            include_private=include_private,
            access_level=access_level,
            limit=limit,
            cursor=cursor,
        )
        if not self.peers:
            return local
        import asyncio

        # Convert datetime to ISO string for peer protocol.
        since_str = since.isoformat() if since is not None else None
        peer_results = await asyncio.gather(
            *(
                peer.list_recent_work(
                    since=since_str,
                    agent=agent,
                    access_level=access_level or ("private" if include_private else "public"),
                    include_private=include_private,
                    limit=limit,
                    cursor=cursor,
                )
                for peer in self.peers
            ),
            return_exceptions=True,
        )
        seen: set[tuple[str, str | None, str]] = set()
        merged: list[SessionRef] = []
        for s in local.sessions:
            key = (s.date, s.cwd, s.agent)
            seen.add(key)
            merged.append(s)
        for peer, payload in zip(self.peers, peer_results):
            if isinstance(payload, Exception):
                logger.warning("peer %s list_recent_work raised: %s", peer.name, payload)
                continue
            sessions = (payload or {}).get("sessions") or []
            for s in sessions:
                if not isinstance(s, dict):
                    continue
                tagged_agent = f"peer:{peer.name}:{s.get('agent', '')}"
                date_str = str(s.get("date") or "")
                cwd = s.get("cwd")
                key = (date_str, cwd, tagged_agent)
                if key in seen:
                    continue
                seen.add(key)
                merged.append(
                    SessionRef(
                        agent=tagged_agent,
                        date=date_str,
                        cwd=cwd,
                        project_focus=list(s.get("project_focus") or []),
                        key_actions=list(s.get("key_actions") or []),
                        files_touched=list(s.get("files_touched") or []),
                    )
                )
        merged.sort(key=lambda s: (s.date, s.agent), reverse=True)
        # Pagination after merge — re-apply limit if caller specified.
        effective_limit = (
            DEFAULT_LIMIT if limit is None else max(1, min(int(limit), MAX_LIMIT))
        )
        page = merged[:effective_limit]
        return PaginatedSessions(
            sessions=page,
            next_cursor=None,  # cross-peer cursoring not in v0
            has_more=len(merged) > len(page),
        )

    async def get_cross_agent_summary_federated(
        self,
        project: str,
        include_private: bool = False,
        access_level: str | None = None,
    ) -> "ProjectSummary":
        """Local + peer aggregates merged into one ProjectSummary."""
        local = self.get_cross_agent_summary(
            project,
            include_private=include_private,
            access_level=access_level,
        )
        if not self.peers:
            return local
        import asyncio

        peer_results = await asyncio.gather(
            *(
                peer.get_cross_agent_summary(
                    project,
                    access_level=access_level or ("private" if include_private else "public"),
                    include_private=include_private,
                )
                for peer in self.peers
            ),
            return_exceptions=True,
        )
        agent_set: set[str] = set(local.agents)
        # Deepen sessions union (dedupe by (date, cwd, agent)).
        seen: set[tuple[str, str | None, str]] = set()
        merged_sessions: list[SessionRef] = []
        for s in local.recent_sessions:
            seen.add((s.date, s.cwd, s.agent))
            merged_sessions.append(s)
        # Entities by name.lower().
        merged_entities: dict[str, EntityMatch] = {
            m.name.lower(): m for m in local.entities
        }
        for peer, payload in zip(self.peers, peer_results):
            if isinstance(payload, Exception):
                logger.warning("peer %s get_cross_agent_summary raised: %s", peer.name, payload)
                continue
            if not isinstance(payload, dict):
                continue
            for a in payload.get("agents") or []:
                if isinstance(a, str):
                    agent_set.add(f"peer:{peer.name}:{a}")
            for s in payload.get("recent_sessions") or []:
                if not isinstance(s, dict):
                    continue
                tagged_agent = f"peer:{peer.name}:{s.get('agent', '')}"
                key = (str(s.get("date") or ""), s.get("cwd"), tagged_agent)
                if key in seen:
                    continue
                seen.add(key)
                merged_sessions.append(
                    SessionRef(
                        agent=tagged_agent,
                        date=str(s.get("date") or ""),
                        cwd=s.get("cwd"),
                        project_focus=list(s.get("project_focus") or []),
                        key_actions=list(s.get("key_actions") or []),
                        files_touched=list(s.get("files_touched") or []),
                    )
                )
            for entry in payload.get("entities") or []:
                if not isinstance(entry, dict):
                    continue
                ent_name = (entry.get("name") or "").strip()
                if not ent_name:
                    continue
                key2 = ent_name.lower()
                target = merged_entities.get(key2)
                if target is None:
                    target = EntityMatch(name=ent_name)
                    merged_entities[key2] = target
                for a in entry.get("agents") or []:
                    if isinstance(a, str):
                        tagged = f"peer:{peer.name}:{a}"
                        if tagged not in target.agents:
                            target.agents.append(tagged)
                for t in entry.get("types") or []:
                    if isinstance(t, str) and t not in target.types:
                        target.types.append(t)
                for tag in entry.get("tags") or []:
                    if isinstance(tag, str) and tag not in target.tags:
                        target.tags.append(tag)
                for agent_id, summary in (entry.get("summaries") or {}).items():
                    if isinstance(agent_id, str) and isinstance(summary, str):
                        target.summaries.setdefault(
                            f"peer:{peer.name}:{agent_id}", summary
                        )
        merged_sessions.sort(key=lambda s: s.date, reverse=True)
        return ProjectSummary(
            project=project,
            agents=sorted(agent_set),
            recent_sessions=merged_sessions,
            entities=list(merged_entities.values()),
        )

    def commit_l5(
        self,
        agent_id: str,
        *,
        agent_type: str | None = None,
        instance: str | None = None,
        role_narrative: str | None = None,
        entities: list[dict] | None = None,
        sessions: list[dict] | None = None,
        mode: str = "merge",
    ) -> dict[str, Any]:
        """
        Write a contribution to ``~/agent-library/agents/<agent_id>.l5.yaml``.

        This is the write-side companion to the existing read APIs. It exists
        so cloud-only or webview-wrapper agents (Claude Desktop, ChatGPT
        desktop, etc.) -- which have no readable on-disk store for a Bourdon
        participant to scrape -- can contribute to federation by calling this
        method (via the ``commit_to_federation`` MCP tool).

        Parameters
        ----------
        agent_id : str
            Agent slug. Must match ``^[a-z0-9][a-z0-9_-]*$`` per L5 schema.
            This is the manifest filename and the cross-agent reference key.
        agent_type : str, optional
            Required when creating a NEW manifest. One of the agent-type
            enum values from ``spec/L5_schema.json`` (``code-assistant``,
            ``note-capture``, ``other``, etc.). When merging into an
            existing manifest, this parameter is ignored unless the
            existing manifest has no ``agent.type`` (recovery path).
        instance : str, optional
            Optional machine/deployment identifier. Survives across merges
            once set.
        role_narrative : str, optional
            Optional ``agent.role_narrative``. When provided, overwrites
            any prior value on the existing manifest (most recent wins).
        entities : list of dict, optional
            Entity rows. Each must have a non-empty ``name``. Other fields
            (``type``, ``summary``, ``tags``, ``visibility``, ``aliases``,
            ``valid_from``, ``valid_to``) are passed through as-is.
        sessions : list of dict, optional
            Session rows. Each must have a ``date`` (ISO 8601 date or
            datetime string). Other fields (``cwd``, ``project_focus``,
            ``key_actions``, ``files_touched``, ``visibility``) are
            passed through as-is.
        mode : "merge" or "replace"
            ``merge`` (default): union new entities/sessions with the
            existing manifest. Entities dedupe by ``name.lower()``;
            sessions dedupe by ``(date, cwd)`` tuple. For dupes, the
            new value wins for non-list fields; list fields (``tags``,
            ``aliases``, ``key_actions``, ``files_touched``,
            ``project_focus``) are unioned.
            ``replace``: discard existing content and write the provided
            entities/sessions as the whole manifest.

        Returns
        -------
        dict
            Summary of what was written: counts of added/updated rows,
            total counts post-write, path on disk, agent identity.

        Raises
        ------
        ValueError
            On invalid agent_id, invalid mode, missing agent_type for a
            new manifest, or malformed entity/session rows.
        """
        # -- validate inputs -------------------------------------------------
        if not _AGENT_ID_RE.match(agent_id or ""):
            raise ValueError(
                f"invalid agent_id {agent_id!r}: must match {_AGENT_ID_RE.pattern}"
            )
        if mode not in ("merge", "replace"):
            raise ValueError(f"invalid mode {mode!r}: must be 'merge' or 'replace'")

        new_entities = list(entities or [])
        new_sessions = list(sessions or [])
        for ent in new_entities:
            if not isinstance(ent, dict):
                raise ValueError(f"entity is not a dict: {ent!r}")
            name = ent.get("name")
            if not isinstance(name, str) or not name.strip():
                raise ValueError(f"entity missing non-empty 'name': {ent!r}")
        for ses in new_sessions:
            if not isinstance(ses, dict):
                raise ValueError(f"session is not a dict: {ses!r}")
            if not isinstance(ses.get("date"), str) or not ses["date"].strip():
                raise ValueError(f"session missing non-empty 'date': {ses!r}")

        existing = self._manifests.get(agent_id) if mode == "merge" else None

        # agent_type: required for new manifests; for merges, fall back to
        # the existing manifest's value; if neither, error.
        existing_type = None
        if existing:
            existing_agent = existing.get("agent") or {}
            existing_type = existing_agent.get("type")
        resolved_type = agent_type or existing_type
        if resolved_type is None:
            raise ValueError(
                f"agent_type is required for a new manifest "
                f"(agent_id={agent_id!r}, mode={mode!r})"
            )
        if resolved_type not in _ALLOWED_AGENT_TYPES:
            raise ValueError(
                f"agent_type {resolved_type!r} is not in the L5 schema enum: "
                f"{sorted(_ALLOWED_AGENT_TYPES)}"
            )

        # -- build the manifest dict ----------------------------------------
        if mode == "replace" or not existing:
            manifest: dict[str, Any] = {
                "spec_version": "0.1",
                "agent": {"id": agent_id, "type": resolved_type},
                "last_updated": datetime.now(timezone.utc).isoformat(),
                "recent_sessions": [],
                "known_entities": [],
            }
        else:
            # Deep-copy the existing manifest so we don't mutate the in-memory
            # cache before write. The cache is refreshed via reload_agent
            # after the write lands.
            manifest = json.loads(json.dumps(existing))
            manifest.setdefault("agent", {})
            manifest["agent"]["id"] = agent_id
            manifest["agent"]["type"] = resolved_type
            manifest.setdefault("spec_version", "0.1")
            manifest["last_updated"] = datetime.now(timezone.utc).isoformat()
            manifest.setdefault("known_entities", [])
            manifest.setdefault("recent_sessions", [])

        if instance is not None:
            manifest["agent"]["instance"] = instance
        if role_narrative is not None:
            manifest["agent"]["role_narrative"] = role_narrative

        # -- merge entities/sessions ----------------------------------------
        ent_added, ent_updated = _merge_entities(
            manifest["known_entities"], new_entities
        )
        ses_added, ses_updated = _merge_sessions(
            manifest["recent_sessions"], new_sessions
        )

        # Sort sessions newest-first, like list_recent_work expects.
        manifest["recent_sessions"].sort(
            key=lambda s: str(s.get("date") or ""), reverse=True
        )

        # -- write atomically -----------------------------------------------
        # Lazy import to avoid a circular dependency: core.l5_io imports
        # participants.base.L5Manifest which imports nothing in core, but the
        # lazy import keeps the import graph tidy in case that ever flips.
        from core.l5_io import write_l5_dict

        target = self._agents_dir() / f"{agent_id}.l5.yaml"
        write_l5_dict(manifest, target)

        # Refresh the in-memory cache so subsequent queries see the write.
        self.reload_agent(agent_id)

        return {
            "agent_id": agent_id,
            "path": str(target),
            "mode": mode,
            "entities_added": ent_added,
            "entities_updated": ent_updated,
            "sessions_added": ses_added,
            "sessions_updated": ses_updated,
            "total_entities": len(manifest["known_entities"]),
            "total_sessions": len(manifest["recent_sessions"]),
            "last_updated": manifest["last_updated"],
        }


# Subset of agent.id pattern from spec/L5_schema.json (kept inline to avoid
# pulling jsonschema as a runtime dep).
_AGENT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")

# Mirrors the agent.type enum in spec/L5_schema.json. Kept inline for the
# same reason -- if the schema enum changes, this list moves in lockstep.
_ALLOWED_AGENT_TYPES = frozenset(
    {
        "code-assistant",
        "note-capture",
        "local-swarm",
        "customer-support",
        "research-assistant",
        "creative-collaborator",
        "project-manager",
        "tutor",
        "other",
    }
)

# Entity dict fields that should be unioned (not overwritten) when merging.
_ENTITY_LIST_FIELDS = ("tags", "aliases")

# Session dict fields that should be unioned (not overwritten) when merging.
_SESSION_LIST_FIELDS = ("project_focus", "key_actions", "files_touched")


def _coerce_list(value: Any) -> list:
    """Normalize a list-typed manifest field that may arrive as a scalar.

    Reader-exported manifests can carry list fields as bare strings (e.g.
    ``project_focus: "Bourdon"``). Without coercion the merge union either
    crashes (`'str' object has no attribute 'append'` when the EXISTING side
    is a string) or silently corrupts (iterating an INCOMING string unions
    its characters). Issue #134.
    """
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _union_list_field(target: dict, field_name: str, value: Any) -> None:
    """Union ``value`` into ``target[field_name]``, coercing both sides."""
    current = _coerce_list(target.get(field_name))
    target[field_name] = current
    for item in _coerce_list(value):
        if item not in current:
            current.append(item)


def _normalized_row(row: dict, list_fields: tuple[str, ...]) -> dict:
    """Copy ``row`` with every present list field coerced via ``_coerce_list``.

    Applied on the add path so freshly written rows land schema-clean on
    disk — manifest consumers beyond the merge union (agents export,
    recognition, the tray) read these fields too (#134 follow-up).
    """
    out = dict(row)
    for field_name in list_fields:
        if field_name in out:
            out[field_name] = _coerce_list(out[field_name])
    return out


def _merge_entities(
    existing: list[dict], incoming: list[dict]
) -> tuple[int, int]:
    """Merge ``incoming`` into ``existing`` in-place. Dedupe by name.lower().

    Returns ``(added_count, updated_count)``. List fields (tags, aliases) are
    unioned; non-list fields are overwritten by the incoming value when the
    incoming side provides a non-None value.
    """
    by_key: dict[str, dict] = {}
    for e in existing:
        if isinstance(e, dict):
            name = e.get("name")
            if isinstance(name, str):
                by_key[name.strip().lower()] = e

    added = 0
    updated = 0
    for incoming_ent in incoming:
        key = str(incoming_ent["name"]).strip().lower()
        if key in by_key:
            target = by_key[key]
            for field_name, value in incoming_ent.items():
                if field_name in _ENTITY_LIST_FIELDS:
                    _union_list_field(target, field_name, value)
                elif value is not None:
                    target[field_name] = value
            updated += 1
        else:
            existing.append(_normalized_row(incoming_ent, _ENTITY_LIST_FIELDS))
            by_key[key] = existing[-1]
            added += 1
    return added, updated


def _merge_sessions(
    existing: list[dict], incoming: list[dict]
) -> tuple[int, int]:
    """Merge ``incoming`` into ``existing`` in-place. Dedupe by (date, cwd).

    Returns ``(added_count, updated_count)``. List fields (project_focus,
    key_actions, files_touched) are unioned; non-list fields are
    overwritten by the incoming value.
    """

    def _key(s: dict) -> tuple[str, str]:
        return (str(s.get("date") or ""), str(s.get("cwd") or ""))

    by_key: dict[tuple[str, str], dict] = {
        _key(s): s for s in existing if isinstance(s, dict)
    }
    added = 0
    updated = 0
    for incoming_ses in incoming:
        key = _key(incoming_ses)
        if key in by_key:
            target = by_key[key]
            for field_name, value in incoming_ses.items():
                if field_name in _SESSION_LIST_FIELDS:
                    _union_list_field(target, field_name, value)
                elif value is not None:
                    target[field_name] = value
            updated += 1
        else:
            existing.append(_normalized_row(incoming_ses, _SESSION_LIST_FIELDS))
            by_key[key] = existing[-1]
            added += 1
    return added, updated


def _append_unique(values: list[str], value: str) -> None:
    if value not in values:
        values.append(value)
