"""
Claude Code adapter -- reads claude-brain + auto-memory + MCP knowledge graph.

v0.0.3:
- discover() -- locates the three memory sources
- export_l5() -- builds a populated manifest from all three sources with dedupe
- export_sessions() -- extracts recent sessions from claude-brain/LOG/*.md
- health_check() -- grades source coverage as ok / degraded / blocked
- Privacy: entities typed as person and observations containing credential-like
  strings default to PRIVATE and are filtered before federation.

Paths scanned (in order of preference):

1. CLAUDE_BRAIN env var                -- explicit override
2. ~/claude-brain/                     -- RADLAB default
3. ./claude-brain/                     -- cwd-relative for project-scoped use

Auto-memory and knowledge graph paths are auto-detected from the user's home.
"""

from __future__ import annotations

import json
import logging
import os
import re
import socket
from datetime import date, datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Iterable, Optional

import yaml

from adapters.base import (
    AdapterDiscoveryError,
    AgentInfo,
    AgentStore,
    BourdonAdapter,
    Entity,
    HealthStatus,
    L5Manifest,
    Session,
    SPEC_VERSION,
    Visibility,
    VisibilityPolicy,
    apply_visibility,
    filter_for_federation,
)

logger = logging.getLogger(__name__)

AGENT_ID = "claude-code"
AGENT_TYPE = "code-assistant"
ROLE_NARRATIVE = (
    "Agentic manager and code-assistant. Coordinates the RADLAB agent fleet, "
    "reviews PRs, and consults on architectural decisions. Capable of authoring "
    "code but typically reviews + delegates to specialised code-assistants like "
    "Codex for prime-code execution."
)

# Default visibility policy -- conservative, tuned for personal/work blend
DEFAULT_POLICY = VisibilityPolicy(
    default=Visibility.PUBLIC,
    private_tags=[
        "personal",
        "financial",
        "credential",
        "health",
        "family",
        "legal",
    ],
    team_tags=["internal-roadmap"],
)

# Entity type values (from auto-memory frontmatter or knowledge-graph entityType)
# that default to PRIVATE unless the user explicitly promotes them.
PRIVATE_BY_DEFAULT_TYPES: frozenset[str] = frozenset(
    {"person", "user", "individual", "contact", "family-member", "family_member"}
)

# Patterns in observation text that trigger credential scrubbing.
# Matches are case-insensitive. Any match causes the observation to be redacted
# in the outgoing summary (the raw observation remains in personal memory).
CREDENTIAL_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\b(?:api[_-]?key|api_token)\b", re.IGNORECASE),
    re.compile(r"\b(?:service[_-]?role|access[_-]?token)\b", re.IGNORECASE),
    re.compile(r"\bstripe\s+(?:key|secret|token)", re.IGNORECASE),
    re.compile(r"\b(?:keystore|private[_-]?key|ssh[_-]?key)\b", re.IGNORECASE),
    re.compile(r"\.env\b", re.IGNORECASE),
    re.compile(r"\bpassword\b", re.IGNORECASE),
    re.compile(r"\bbearer\s+token\b", re.IGNORECASE),
    # Stripe / RevenueCat / Apple-style tokens with underscore-separated prefixes
    re.compile(r"\b(?:sk|pk)_(?:live|test)_\w+", re.IGNORECASE),
    re.compile(r"\bappl_\w+", re.IGNORECASE),
    re.compile(r"\bhf_\w{10,}", re.IGNORECASE),  # HuggingFace tokens
)

MAX_SUMMARY_CHARS = 500
MAX_OBSERVATIONS_IN_SUMMARY = 2


# -- Path resolution -----------------------------------------------------------


def _resolve_claude_brain_path() -> Optional[Path]:
    """Locate claude-brain repo using the documented precedence order."""
    env_override = os.environ.get("CLAUDE_BRAIN")
    if env_override:
        p = Path(env_override).expanduser()
        if p.is_dir():
            return p

    home_candidate = Path.home() / "claude-brain"
    if home_candidate.is_dir():
        return home_candidate

    cwd_candidate = Path.cwd() / "claude-brain"
    if cwd_candidate.is_dir():
        return cwd_candidate

    return None


def _resolve_auto_memory_path() -> Optional[Path]:
    """Claude Code auto-memory usually lives at ~/.claude/projects/<workspace>/memory/.

    Returns None on PermissionError / OSError reading the directory tree --
    the CLI must stay importable even when HOME points at an unreadable path
    (e.g. running under sudo without -H, or in a sandboxed container).
    """
    try:
        base = Path.home() / ".claude" / "projects"
        if not base.is_dir():
            return None
        for child in base.iterdir():
            if (child / "memory" / "MEMORY.md").is_file():
                return child / "memory"
    except (PermissionError, OSError):
        return None
    return None


def _resolve_knowledge_graph_path() -> Optional[Path]:
    """MCP knowledge graph lives at ~/claude-memory/memory.jsonl by convention."""
    candidate = Path.home() / "claude-memory" / "memory.jsonl"
    if candidate.is_file():
        return candidate
    return None


# -- Helper: privacy scrubbing -------------------------------------------------


def _contains_credential_pattern(text: str) -> bool:
    """Return True if any credential regex matches the text."""
    return any(pat.search(text) for pat in CREDENTIAL_PATTERNS)


def _is_private_type(entity_type: Optional[str]) -> bool:
    """Return True if the entity_type slug indicates the entity is PRIVATE by default."""
    if not entity_type:
        return False
    # Normalize: "entity/person" -> "person", "type: person" -> "person"
    normalized = entity_type.rsplit("/", 1)[-1].strip().lower()
    return normalized in PRIVATE_BY_DEFAULT_TYPES


# -- Helper: YAML frontmatter --------------------------------------------------


_FRONTMATTER_OPEN = "---\n"
_FRONTMATTER_CLOSE = "\n---\n"


def _parse_frontmatter(
    text: str, source: Optional[Path] = None
) -> tuple[dict, str]:
    """
    Split YAML frontmatter from body.

    Accepts a file content string. If the content opens with ``---\\n`` and has
    a closing ``\\n---\\n``, returns (frontmatter_dict, body). Otherwise returns
    ({}, text) with no error.

    On YAML parse failure logs at WARNING with adapter id, source path (if
    provided), and a truncated exception detail so the offending file is
    discoverable. See issue #79.
    """
    if not text.startswith(_FRONTMATTER_OPEN):
        return {}, text
    end = text.find(_FRONTMATTER_CLOSE, len(_FRONTMATTER_OPEN))
    if end == -1:
        return {}, text
    fm_text = text[len(_FRONTMATTER_OPEN) : end]
    body = text[end + len(_FRONTMATTER_CLOSE) :]
    try:
        parsed = yaml.safe_load(fm_text)
    except yaml.YAMLError as exc:
        where = f" in {source}" if source is not None else ""
        detail = str(exc).replace("\n", " ")[:200]
        logger.warning(
            "ClaudeCodeAdapter: malformed YAML frontmatter%s; "
            "treating as no-frontmatter (%s)",
            where,
            detail,
        )
        return {}, text
    return (parsed if isinstance(parsed, dict) else {}), body


# -- Parser: claude-brain PROJECTS + LOG + CURRENT -----------------------------


_H1_RE = re.compile(r"^#[ \t]+(.+?)[ \t]*$", re.MULTILINE)
# Use [^\n]* for the optional suffix so the regex cannot accidentally consume
# past the heading into the next block (a plain \s* would eat newlines).
_STATUS_SECTION_RE = re.compile(
    r"^##[ \t]+(?:Current[ \t]+)?Status[^\n]*$", re.MULTILINE | re.IGNORECASE
)


def _extract_h1_title(body: str) -> Optional[str]:
    """Return the first H1 heading's text, or None if no H1 found."""
    match = _H1_RE.search(body)
    if not match:
        return None
    title = match.group(1).strip()
    # Strip common trailing separators ("-- ", "— ", ": ") that introduce a subtitle.
    # "Clyde -- AI Assistant (v0.10.0)"  -> "Clyde"
    # "ILTT: if_lift then_that"          -> "ILTT"
    title = re.split(r"\s*[-\u2014\u2013:]+\s+", title, maxsplit=1)[0]
    return title.strip()


def _extract_first_paragraph(body: str, max_chars: int = MAX_SUMMARY_CHARS) -> str:
    """Extract a short summary: first non-empty non-heading paragraph, truncated."""
    lines = body.splitlines()
    paragraph: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            if paragraph:
                break  # end of first paragraph
            continue
        if stripped.startswith("#"):
            continue  # skip headings
        paragraph.append(stripped)
        if sum(len(s) + 1 for s in paragraph) > max_chars:
            break
    joined = " ".join(paragraph).strip()
    return joined[:max_chars].strip()


def _extract_status_tag(body: str) -> list[str]:
    """
    Return tags derived from a ``## Status`` section if present.

    Covers both inline (``## Status: Archived``) and separate-line
    (``## Status\\n\\nArchived``) formats by scanning from ``match.start()``.
    """
    match = _STATUS_SECTION_RE.search(body)
    if not match:
        return []
    region = body[match.start() : match.start() + 400].lower()
    tags: list[str] = []
    for keyword in ("archived", "canceled", "cancelled", "active", "shipped", "blocked"):
        if keyword in region:
            tags.append(keyword.replace("cancelled", "canceled"))
    # Dedupe while preserving first-seen order (handles "canceled" + "cancelled")
    return list(dict.fromkeys(tags))


_END_OF_LIFE_TAGS = frozenset({"archived", "canceled"})

# Match an ISO 8601 date inside parens or after status keywords:
#   "Archived (2026-04-14)"  -> 2026-04-14
#   "Canceled 2026-04-14"    -> 2026-04-14
#   "Status: Archived 2026-04-14" -> 2026-04-14
_STATUS_DATE_RE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")


def _extract_status_date(body: str) -> Optional[str]:
    """
    Pull the ISO 8601 date that appears inside the ``## Status`` section,
    if any. Used for valid_to dating of archived / canceled entities.
    Returns None if no date is found in the status region.
    """
    match = _STATUS_SECTION_RE.search(body)
    if not match:
        return None
    region = body[match.start() : match.start() + 400]
    date_match = _STATUS_DATE_RE.search(region)
    return date_match.group(1) if date_match else None


def _parse_project_overview(overview_path: Path) -> Optional[Entity]:
    """Parse a single PROJECTS/<NAME>/OVERVIEW.md into an Entity."""
    try:
        text = overview_path.read_text(encoding="utf-8")
    except OSError:
        return None
    _, body = _parse_frontmatter(text, source=overview_path)
    title = _extract_h1_title(body) or overview_path.parent.name
    summary = _extract_first_paragraph(body)
    tags = _extract_status_tag(body)

    # Temporal validity: if the entity is end-of-life (archived/canceled),
    # try to recover a valid_to date from the status section. Falls back to
    # the file mtime when the status section doesn't carry an explicit date.
    valid_to: Optional[str] = None
    if any(t in _END_OF_LIFE_TAGS for t in tags):
        valid_to = _extract_status_date(body)
        if valid_to is None:
            try:
                mtime = overview_path.stat().st_mtime
                valid_to = date.fromtimestamp(mtime).isoformat()
            except OSError:
                valid_to = None

    return Entity(
        name=title,
        type="project",
        summary=summary or None,
        tags=tags,
        valid_to=valid_to,
    )


def _parse_projects_dir(brain_path: Path) -> list[Entity]:
    """Return one Entity per PROJECTS/<NAME>/OVERVIEW.md found."""
    projects_dir = brain_path / "PROJECTS"
    if not projects_dir.is_dir():
        return []
    entities: list[Entity] = []
    for project_dir in sorted(projects_dir.iterdir()):
        if not project_dir.is_dir():
            continue
        overview = project_dir / "OVERVIEW.md"
        if not overview.is_file():
            continue
        entity = _parse_project_overview(overview)
        if entity:
            entities.append(entity)
    return entities


_LOG_FILENAME_RE = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2})(?:-(?P<machine>\w+?))?(?:-.*)?\.md$"
)


def _parse_log_file(log_path: Path) -> Optional[Session]:
    """Parse one LOG/YYYY-MM-DD-<machine>(-sessionN)?.md into a Session."""
    m = _LOG_FILENAME_RE.match(log_path.name)
    if not m:
        return None
    session_date = m.group("date")
    machine = m.group("machine") or "unknown"
    try:
        text = log_path.read_text(encoding="utf-8")
    except OSError:
        return None
    _, body = _parse_frontmatter(text, source=log_path)
    # Extract first paragraph of content as key action summary
    headline = _extract_first_paragraph(body, max_chars=250)
    key_actions = [headline] if headline else []
    return Session(
        date=session_date,
        cwd=None,
        project_focus=[],
        key_actions=key_actions,
        files_touched=[f"LOG/{log_path.name}"],
    )


def _parse_logs_dir(
    brain_path: Path, since: Optional[datetime] = None, limit: int = 100
) -> list[Session]:
    """Return Session rows for recent LOG entries (newest first, up to ``limit``)."""
    log_dir = brain_path / "LOG"
    if not log_dir.is_dir():
        return []
    candidates: list[tuple[str, Path]] = []
    for log_file in log_dir.glob("*.md"):
        m = _LOG_FILENAME_RE.match(log_file.name)
        if not m:
            continue
        d = m.group("date")
        if since is not None:
            try:
                parsed = date.fromisoformat(d)
            except ValueError:
                continue
            if parsed < since.date():
                continue
        candidates.append((d, log_file))
    # Newest first
    candidates.sort(key=lambda x: x[0], reverse=True)
    sessions: list[Session] = []
    for _, log_file in candidates[:limit]:
        session = _parse_log_file(log_file)
        if session:
            sessions.append(session)
    return sessions


# -- Parser: auto-memory -------------------------------------------------------


def _parse_auto_memory_entity(md_path: Path) -> Optional[Entity]:
    """Parse a single auto-memory entity file (YAML frontmatter + markdown body)."""
    try:
        text = md_path.read_text(encoding="utf-8")
    except OSError:
        return None
    frontmatter, body = _parse_frontmatter(text, source=md_path)
    name = frontmatter.get("name") or _extract_h1_title(body) or md_path.stem
    entity_type = frontmatter.get("type")
    summary = frontmatter.get("description") or _extract_first_paragraph(body)
    raw_tags = frontmatter.get("tags", []) or []
    tags = [str(t) for t in raw_tags] if isinstance(raw_tags, list) else []

    visibility = None
    if _is_private_type(entity_type):
        visibility = Visibility.PRIVATE

    return Entity(
        name=str(name),
        type=str(entity_type) if entity_type else None,
        summary=str(summary)[:MAX_SUMMARY_CHARS].strip() if summary else None,
        tags=tags,
        visibility=visibility,
    )


def _parse_auto_memory(memory_path: Path) -> list[Entity]:
    """Parse every .md file in the auto-memory dir except MEMORY.md itself."""
    if not memory_path.is_dir():
        return []
    entities: list[Entity] = []
    for md_file in sorted(memory_path.glob("*.md")):
        if md_file.name == "MEMORY.md":
            continue
        entity = _parse_auto_memory_entity(md_file)
        if entity:
            entities.append(entity)
    return entities


# -- Parser: knowledge graph JSONL ---------------------------------------------


def _graph_entity_to_bourdon_entity(record: dict) -> Optional[Entity]:
    """Convert one MCP graph entity record to a Bourdon Entity."""
    name = record.get("name")
    if not name or not isinstance(name, str):
        return None
    entity_type_slug = record.get("entityType", "")
    normalized_type = entity_type_slug.rsplit("/", 1)[-1] if entity_type_slug else None
    observations = record.get("observations", []) or []

    # Build summary from the first N observations, scrubbing credential patterns
    safe_obs: list[str] = []
    for obs in observations[:MAX_OBSERVATIONS_IN_SUMMARY]:
        if not isinstance(obs, str):
            continue
        if _contains_credential_pattern(obs):
            safe_obs.append("[redacted -- contains credential-like content]")
        else:
            safe_obs.append(obs)
    summary = " | ".join(safe_obs)[:MAX_SUMMARY_CHARS].strip() or None

    visibility = None
    if _is_private_type(entity_type_slug):
        visibility = Visibility.PRIVATE
    # If any observation in the FULL list contains a credential, play it safe:
    # mark the entity PRIVATE even if we scrubbed the summary.
    elif any(isinstance(o, str) and _contains_credential_pattern(o) for o in observations):
        visibility = Visibility.PRIVATE

    tags = [normalized_type] if normalized_type and normalized_type != name.lower() else []

    return Entity(
        name=name,
        type=normalized_type,
        summary=summary,
        tags=tags,
        visibility=visibility,
    )


def _parse_knowledge_graph(graph_path: Path) -> list[Entity]:
    """Parse the MCP knowledge graph JSONL into Entities (relations skipped)."""
    if not graph_path.is_file():
        return []
    entities: list[Entity] = []
    try:
        with open(graph_path, "r", encoding="utf-8") as f:
            for line_num, line in enumerate(f, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError as e:
                    logger.warning(
                        "Skipping malformed JSONL at %s:%d -- %s",
                        graph_path,
                        line_num,
                        e,
                    )
                    continue
                if record.get("type") != "entity":
                    continue
                entity = _graph_entity_to_bourdon_entity(record)
                if entity:
                    entities.append(entity)
    except OSError:
        return []
    return entities


# -- Dedupe + merge ------------------------------------------------------------


def _merge_entities(into: Entity, other: Entity) -> Entity:
    """
    Merge ``other`` into ``into`` in place, returning ``into``.

    Rules:
      - Type: keep ``into.type`` if set, otherwise adopt other.type
      - Summary: keep the longer non-empty one
      - Tags: union
      - Visibility: keep the more restrictive (PRIVATE > TEAM > PUBLIC)
    """
    if not into.type and other.type:
        into.type = other.type

    if (other.summary and len(other.summary) > len(into.summary or "")) and other.summary.strip():
        into.summary = other.summary

    union_tags = list(dict.fromkeys([*into.tags, *(other.tags or [])]))
    into.tags = union_tags

    # Visibility: PRIVATE wins, then TEAM, then PUBLIC
    order = {Visibility.PRIVATE: 0, Visibility.TEAM: 1, Visibility.PUBLIC: 2, None: 3}
    if order[other.visibility] < order[into.visibility]:
        into.visibility = other.visibility

    return into


def _dedupe_entities(source_lists: Iterable[list[Entity]]) -> list[Entity]:
    """
    Merge multiple entity lists into one, deduping by case-insensitive name.

    Later sources merge into earlier-seen entities. Ordering of source_lists
    determines which source's metadata wins when conflicts occur.
    """
    by_key: dict[str, Entity] = {}
    for source in source_lists:
        for entity in source:
            key = entity.name.strip().lower()
            if not key:
                continue
            if key in by_key:
                _merge_entities(by_key[key], entity)
            else:
                # Copy into a fresh Entity so the merge doesn't mutate sources
                by_key[key] = Entity(
                    name=entity.name,
                    type=entity.type,
                    aliases=list(entity.aliases),
                    summary=entity.summary,
                    last_touched=entity.last_touched,
                    tags=list(entity.tags),
                    visibility=entity.visibility,
                )
    # Stable alphabetical order for deterministic output
    return sorted(by_key.values(), key=lambda e: e.name.lower())


# -- Adapter class -------------------------------------------------------------


class ClaudeCodeAdapter:
    """
    External adapter for Anthropic's Claude Code CLI.

    Reads three memory sources (all optional; adapter degrades gracefully):
        1. claude-brain/                   -- git-synced markdown records
        2. ~/.claude/projects/*/memory/    -- per-machine auto-memory
        3. ~/claude-memory/memory.jsonl    -- MCP knowledge graph JSONL
    """

    agent_id = AGENT_ID
    agent_type = AGENT_TYPE

    def __init__(self) -> None:
        self.native_path = str(Path.home() / "claude-brain")  # primary anchor
        self._brain_path = _resolve_claude_brain_path()
        self._auto_memory_path = _resolve_auto_memory_path()
        self._knowledge_graph_path = _resolve_knowledge_graph_path()

    # -- Protocol methods ------------------------------------------------------

    def discover(self) -> AgentStore:
        """Locate Claude Code memory stores. Raises if none are reachable."""
        sources = {
            "claude_brain": str(self._brain_path) if self._brain_path else None,
            "auto_memory": str(self._auto_memory_path) if self._auto_memory_path else None,
            "knowledge_graph": str(self._knowledge_graph_path) if self._knowledge_graph_path else None,
        }
        if not any(sources.values()):
            raise AdapterDiscoveryError(
                "No Claude Code memory sources found. "
                "Expected one of: ~/claude-brain/, ~/.claude/projects/*/memory/, "
                "~/claude-memory/memory.jsonl."
            )
        return AgentStore(
            path=self.native_path,
            version="claude-code-memory-v1",
            metadata={"sources": sources},
        )

    def export_l5(self, since: Optional[datetime] = None) -> L5Manifest:
        """Build L5 manifest from all three sources, deduped and visibility-filtered."""
        store = self.discover()  # re-raises on missing
        capabilities = sorted(k for k, v in store.metadata["sources"].items() if v)

        # Gather entities from each source (empty list if source missing)
        auto_memory_entities = (
            _parse_auto_memory(self._auto_memory_path)
            if self._auto_memory_path
            else []
        )
        graph_entities = (
            _parse_knowledge_graph(self._knowledge_graph_path)
            if self._knowledge_graph_path
            else []
        )
        project_entities = (
            _parse_projects_dir(self._brain_path) if self._brain_path else []
        )

        # Priority order: auto-memory (richest structured metadata) > graph > brain projects.
        # First source in the list wins on type/summary when names collide.
        all_entities = _dedupe_entities(
            [auto_memory_entities, graph_entities, project_entities]
        )

        # Apply visibility filter -- private entities never leave this function.
        visible_entities = filter_for_federation(all_entities, DEFAULT_POLICY)

        # Sessions from the LOG dir (brain only -- auto-memory + graph have no sessions)
        sessions = (
            _parse_logs_dir(self._brain_path, since=since)
            if self._brain_path
            else []
        )

        return L5Manifest(
            spec_version=SPEC_VERSION,
            agent=AgentInfo(
                id=self.agent_id,
                type=self.agent_type,
                instance=socket.gethostname() or "unknown",
                spec_version_compat=f">={SPEC_VERSION}",
                role_narrative=ROLE_NARRATIVE,
            ),
            last_updated=datetime.now(timezone.utc).isoformat(),
            capabilities=capabilities,
            recent_sessions=sessions,
            known_entities=visible_entities,
            visibility_policy=DEFAULT_POLICY,
        )

    def export_sessions(
        self, since: datetime, limit: int = 100
    ) -> list[Session]:
        """Export recent LOG sessions from claude-brain/LOG/*.md."""
        if not self._brain_path:
            return []
        return _parse_logs_dir(self._brain_path, since=since, limit=limit)

    def health_check(self) -> HealthStatus:
        """
        Classification:
            - ok        -- all 3 sources reachable
            - degraded  -- 1 or 2 sources missing
            - blocked   -- 0 sources reachable
        """
        found = sum(
            1
            for p in (
                self._brain_path,
                self._auto_memory_path,
                self._knowledge_graph_path,
            )
            if p is not None
        )
        details = {
            "claude_brain": str(self._brain_path) if self._brain_path else "missing",
            "auto_memory": str(self._auto_memory_path) if self._auto_memory_path else "missing",
            "knowledge_graph": str(self._knowledge_graph_path) if self._knowledge_graph_path else "missing",
        }
        if found == 3:
            return HealthStatus(status="ok", details=details)
        if found == 0:
            return HealthStatus(
                status="blocked",
                reason="No Claude Code memory sources found on this machine",
                details=details,
            )
        return HealthStatus(
            status="degraded",
            reason=f"{found}/3 Claude Code memory sources found",
            details=details,
        )


# Protocol conformance check -- evaluated by static type checkers (mypy/pyright)
# at lint time only. Does not instantiate at import time, so the CLI stays
# importable even when HOME points at an unreadable directory.
if TYPE_CHECKING:
    _: BourdonAdapter = ClaudeCodeAdapter()
