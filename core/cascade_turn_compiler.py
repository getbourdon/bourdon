"""Cascade turn-scoped recognition compiler.

Compiles a per-turn recognition brief from:
- Prompt text (bounded at 8K chars)
- Current working directory / repo identity
- Cascade convention-file entities and sessions
- Native Windsurf state (workspace metadata, plans, workflows)
- L6 federation library

Produces a ``cascade-turn-brief/v1`` schema output that can be delivered via
explicit pre-turn text, MCP payload, or convention-file compatibility block.

Safety guarantees:
- **Read-only**: never mutates any files
- **Deterministic**: same inputs → same output (no model calls, no network)
- **Bounded**: prompt capped at 8K chars, output capped at max_chars
- **Fast**: targets <100ms on typical developer machine
"""

from __future__ import annotations

import configparser
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# -- Constants -----------------------------------------------------------------

SCHEMA_VERSION = "cascade-turn-brief/v1"
DELIVERY_MODES = frozenset({"explicit", "mcp", "convention-file", "all"})
MAX_PROMPT_CHARS = 8000
_SCORE_THRESHOLD = 2.0

# Scoring weights
_W_TOKEN_OVERLAP = 0.4
_W_CWD_AFFINITY = 0.25
_W_RECENCY = 0.2
_W_SOURCE_CONFIDENCE = 0.15

# Source confidence values
_SOURCE_CONFIDENCE = {
    "convention_file": 1.0,
    "native_windsurf": 0.9,
    "l6_federation": 0.8,
    "workspace_context": 0.7,
}


# -- Data classes --------------------------------------------------------------


@dataclass
class RepoIdentity:
    """Identity of the current git repository."""

    name: str | None = None
    root: str | None = None
    remote: str | None = None


@dataclass
class BriefItem:
    """A scored item in the compiled brief."""

    rank: int
    score: float
    kind: str
    name: str
    summary: str
    reason: str
    source: str
    source_agents: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "rank": self.rank,
            "score": round(self.score, 2),
            "kind": self.kind,
            "name": self.name,
            "summary": self.summary,
            "reason": self.reason,
            "source": self.source,
            "source_agents": self.source_agents,
            "evidence": self.evidence,
        }


@dataclass
class BriefHealth:
    """Health signals for the compilation."""

    convention_file: str  # available | degraded | missing
    native_state: str  # available | degraded | missing
    strategy: str = "turn_compiled"

    def to_dict(self) -> dict[str, Any]:
        return {
            "convention_file": self.convention_file,
            "native_state": self.native_state,
            "strategy": self.strategy,
        }


@dataclass
class BriefRouting:
    """Routing decision for the brief delivery."""

    primary_surface: str
    confidence: str  # high | medium | low
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "primary_surface": self.primary_surface,
            "confidence": self.confidence,
            "reason": self.reason,
        }


@dataclass
class BriefDelivery:
    """Delivery payloads for the compiled brief."""

    explicit_text: str = ""
    mcp_payload: dict[str, Any] = field(default_factory=dict)
    convention_file_block: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "explicit_text": self.explicit_text,
            "mcp_payload": self.mcp_payload,
            "convention_file_block": self.convention_file_block,
        }


@dataclass
class TurnBrief:
    """Complete turn-scoped recognition brief."""

    schema_version: str = SCHEMA_VERSION
    prompt: str = ""
    cwd: str | None = None
    repo: RepoIdentity = field(default_factory=RepoIdentity)
    health: BriefHealth = field(default_factory=lambda: BriefHealth("missing", "missing"))
    routing: BriefRouting = field(
        default_factory=lambda: BriefRouting("fallback", "low", "no items")
    )
    items: list[BriefItem] = field(default_factory=list)
    delivery: BriefDelivery = field(default_factory=BriefDelivery)
    trace: dict[str, Any] = field(default_factory=dict)
    diagnostics: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "prompt": self.prompt,
            "cwd": self.cwd,
            "repo": {
                "name": self.repo.name,
                "root": self.repo.root,
                "remote": self.repo.remote,
            },
            "health": self.health.to_dict(),
            "routing": self.routing.to_dict(),
            "items": [item.to_dict() for item in self.items],
            "delivery": self.delivery.to_dict(),
            "trace": self.trace,
            "diagnostics": self.diagnostics,
        }


# -- Internal candidate structure ----------------------------------------------


@dataclass
class _Candidate:
    """Internal scored candidate before ranking."""

    kind: str
    name: str
    summary: str
    source: str
    source_agents: list[str] = field(default_factory=list)
    aliases: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    date_text: str | None = None
    cwd: str | None = None
    project_focus: list[str] = field(default_factory=list)
    files_touched: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    score: float = 0.0


# -- Helper functions ----------------------------------------------------------


def _validate_delivery(value: str) -> str:
    if value not in DELIVERY_MODES:
        raise ValueError(f"delivery must be one of {sorted(DELIVERY_MODES)}")
    return value


def _bounded_int(value: int, *, minimum: int, maximum: int, name: str) -> int:
    number = int(value)
    if number < minimum or number > maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return number


def _resolve_cwd(cwd: str | Path | None) -> Path | None:
    if cwd is None:
        return Path.cwd()
    text = str(cwd).strip()
    if not text:
        return None
    return Path(text).expanduser().resolve(strict=False)


def _detect_repo(cwd: Path | None) -> RepoIdentity:
    if cwd is None:
        return RepoIdentity()
    root = _find_git_root(cwd)
    if root is None:
        return RepoIdentity(name=cwd.name or None, root=None, remote=None)
    remote = _read_git_origin(root)
    return RepoIdentity(name=root.name, root=str(root), remote=remote)


def _find_git_root(path: Path) -> Path | None:
    current = path if path.is_dir() else path.parent
    for candidate in (current, *current.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


def _read_git_origin(root: Path) -> str | None:
    git_path = root / ".git"
    config_path = git_path / "config" if git_path.is_dir() else None
    if config_path is None or not config_path.is_file():
        return None
    parser = configparser.ConfigParser()
    try:
        parser.read(config_path, encoding="utf-8")
    except configparser.Error:
        return None
    section = 'remote "origin"'
    if not parser.has_section(section):
        return None
    remote = parser.get(section, "url", fallback=None)
    return remote.strip() if isinstance(remote, str) and remote.strip() else None


def _safe_summary(text: str, limit: int = 200) -> str:
    """Truncate and clean a summary string."""
    text = text.strip()
    if len(text) > limit:
        text = text[:limit].rsplit(" ", 1)[0] + "…"
    return text


def _tokenize(text: str) -> set[str]:
    """Simple word-boundary tokenization for scoring."""
    return set(re.findall(r"[a-zA-Z0-9_-]+", text.lower()))


def _token_overlap_score(prompt_tokens: set[str], candidate: _Candidate) -> float:
    """Score based on prompt-token overlap with candidate name + aliases + summary."""
    if not prompt_tokens:
        return 0.0
    candidate_text = " ".join([
        candidate.name,
        " ".join(candidate.aliases),
        candidate.summary,
        " ".join(candidate.tags),
    ])
    candidate_tokens = _tokenize(candidate_text)
    if not candidate_tokens:
        return 0.0
    overlap = prompt_tokens & candidate_tokens
    if not overlap:
        return 0.0
    # Boost for name match
    name_tokens = _tokenize(candidate.name)
    name_overlap = prompt_tokens & name_tokens
    name_bonus = 3.0 if name_overlap else 0.0
    return min(10.0, (len(overlap) / max(len(prompt_tokens), 1)) * 10.0 + name_bonus)


def _cwd_affinity_score(repo: RepoIdentity, candidate: _Candidate) -> float:
    """Score based on cwd/repo match with candidate."""
    if repo.name is None:
        return 0.0
    score = 0.0
    repo_name_lower = repo.name.lower()
    # Check project_focus match
    for focus in candidate.project_focus:
        if focus.lower() == repo_name_lower:
            score = 10.0
            break
    # Check cwd match
    if candidate.cwd and repo.root:
        if repo.root in candidate.cwd or candidate.cwd in repo.root:
            score = max(score, 8.0)
    # Check files_touched for repo name
    for path in candidate.files_touched:
        if repo_name_lower in path.lower():
            score = max(score, 5.0)
            break
    return score


def _recency_score(candidate: _Candidate) -> float:
    """Score based on recency of last touch."""
    if not candidate.date_text:
        return 5.0  # neutral if no date
    try:
        if len(candidate.date_text) == 10:
            dt = datetime.strptime(candidate.date_text, "%Y-%m-%d").replace(
                tzinfo=timezone.utc
            )
        else:
            dt = datetime.fromisoformat(candidate.date_text)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return 5.0
    now = datetime.now(timezone.utc)
    days_ago = (now - dt).days
    if days_ago <= 1:
        return 10.0
    if days_ago <= 7:
        return 8.0
    if days_ago <= 30:
        return 6.0
    if days_ago <= 90:
        return 4.0
    return 2.0


def _source_confidence_score(source: str) -> float:
    """Score based on source type confidence."""
    return _SOURCE_CONFIDENCE.get(source, 0.5) * 10.0


def _score_candidate(
    candidate: _Candidate,
    prompt_tokens: set[str],
    repo: RepoIdentity,
) -> float:
    """Compute weighted score for a candidate."""
    token_score = _token_overlap_score(prompt_tokens, candidate)
    cwd_score = _cwd_affinity_score(repo, candidate)
    recency = _recency_score(candidate)
    source_conf = _source_confidence_score(candidate.source)

    return (
        _W_TOKEN_OVERLAP * token_score
        + _W_CWD_AFFINITY * cwd_score
        + _W_RECENCY * recency
        + _W_SOURCE_CONFIDENCE * source_conf
    )


def _build_evidence(
    candidate: _Candidate,
    prompt_tokens: set[str],
    repo: RepoIdentity,
) -> list[str]:
    """Build human-readable evidence list for why a candidate scored high."""
    evidence = list(candidate.evidence)
    candidate_tokens = _tokenize(candidate.name + " " + " ".join(candidate.aliases))
    overlap = prompt_tokens & candidate_tokens
    if overlap:
        evidence.append(f"token overlap: {', '.join(sorted(overlap)[:5])}")
    if repo.name and any(
        focus.lower() == repo.name.lower() for focus in candidate.project_focus
    ):
        evidence.append(f"project_focus match: {repo.name}")
    if candidate.cwd and repo.root and repo.root in candidate.cwd:
        evidence.append("cwd match")
    return evidence[:6]


# -- Candidate gathering -------------------------------------------------------


def _gather_from_convention_file(
    cascade_dir: Path | None,
) -> list[_Candidate]:
    """Gather candidates from the Cascade convention file."""
    from adapters.cascade import (
        _MEMORY_FILENAME,
        _parse_frontmatter,
        default_cascade_dir,
    )

    target_dir = cascade_dir or default_cascade_dir()
    memory_path = target_dir / _MEMORY_FILENAME
    if not memory_path.is_file():
        return []

    try:
        text = memory_path.read_text(encoding="utf-8")
        data = _parse_frontmatter(text, source=memory_path)
    except (OSError, Exception):
        return []

    candidates: list[_Candidate] = []

    for entity in data.get("entities") or []:
        if not isinstance(entity, dict):
            continue
        name = str(entity.get("name") or "").strip()
        if not name:
            continue
        candidates.append(
            _Candidate(
                kind=str(entity.get("type") or "topic"),
                name=name,
                summary=_safe_summary(str(entity.get("summary") or "")),
                source="convention_file",
                source_agents=["cascade"],
                aliases=[
                    str(a) for a in entity.get("aliases") or []
                    if isinstance(a, str) and a.strip()
                ],
                tags=[
                    str(t) for t in entity.get("tags") or []
                    if isinstance(t, str) and t.strip()
                ],
                date_text=str(entity.get("last_touched") or "") or None,
            )
        )

    for session in data.get("sessions") or []:
        if not isinstance(session, dict):
            continue
        date_str = str(session.get("date") or "")
        actions = session.get("key_actions") or []
        name = "; ".join(str(a) for a in actions[:2]) if actions else date_str
        if not name.strip():
            continue
        candidates.append(
            _Candidate(
                kind="session",
                name=_safe_summary(name, limit=120),
                summary=_safe_summary("; ".join(str(a) for a in actions[:4])),
                source="convention_file",
                source_agents=["cascade"],
                date_text=date_str or None,
                cwd=session.get("cwd") if isinstance(session.get("cwd"), str) else None,
                project_focus=[
                    str(f) for f in session.get("project_focus") or []
                    if isinstance(f, str) and f.strip()
                ],
                files_touched=[
                    str(p) for p in session.get("files_touched") or []
                    if isinstance(p, str) and p.strip()
                ],
            )
        )

    return candidates


def _gather_from_native_state(
    cwd: Path | None,
    windsurf_data_dir: Path | None = None,
) -> list[_Candidate]:
    """Gather candidates from native Windsurf state."""
    from adapters._windsurf_native import read_native_windsurf_state

    state = read_native_windsurf_state(windsurf_data_dir=windsurf_data_dir, cwd=cwd)
    if not state.available:
        return []

    candidates: list[_Candidate] = []

    # Cascade sessions from editor state
    for session in state.cascade_sessions:
        candidates.append(
            _Candidate(
                kind="session",
                name=_safe_summary(session.title, limit=120),
                summary=f"Cascade session: {session.title}",
                source="native_windsurf",
                source_agents=["cascade"],
            )
        )

    # Plans as entity candidates
    for plan in state.plans:
        candidates.append(
            _Candidate(
                kind="plan",
                name=plan.title,
                summary=_safe_summary(plan.content_preview, limit=200),
                source="workspace_context",
                source_agents=["cascade"],
                evidence=[f"plan file: {plan.filename}"],
            )
        )

    # Workflows as entity candidates
    for workflow in state.workflows:
        candidates.append(
            _Candidate(
                kind="workflow",
                name=workflow.filename.replace(".md", ""),
                summary=workflow.description,
                source="workspace_context",
                source_agents=["cascade"],
                evidence=[f"workflow file: {workflow.filename}"],
            )
        )

    return candidates


def _gather_from_federation(
    library_path: Path | None,
    access_level: str,
) -> list[_Candidate]:
    """Gather candidates from the L6 federation library."""
    from core.l6_store import DEFAULT_LIBRARY_PATH, L6Store

    lib_path = library_path or DEFAULT_LIBRARY_PATH
    if not lib_path.is_dir():
        return []

    store = L6Store(lib_path)
    manifest = store.build_recognition_manifest(access_level=access_level)

    candidates: list[_Candidate] = []

    for entity in manifest.get("known_entities") or []:
        if not isinstance(entity, dict):
            continue
        name = str(entity.get("name") or "").strip()
        if not name:
            continue
        source_agents = [
            str(a) for a in entity.get("source_agents") or []
            if isinstance(a, str) and a
        ]
        candidates.append(
            _Candidate(
                kind=str(entity.get("type") or "topic"),
                name=name,
                summary=_safe_summary(str(entity.get("summary") or "")),
                source="l6_federation",
                source_agents=source_agents,
                aliases=[
                    str(a) for a in entity.get("aliases") or []
                    if isinstance(a, str) and a.strip()
                ],
                tags=[
                    str(t) for t in entity.get("tags") or []
                    if isinstance(t, str) and t.strip()
                ],
            )
        )

    for session in manifest.get("recent_sessions") or []:
        if not isinstance(session, dict):
            continue
        agent = str(session.get("agent") or "")
        actions = session.get("key_actions") or []
        name = "; ".join(str(a) for a in actions[:2]) if actions else str(session.get("date") or "")
        if not name.strip():
            continue
        candidates.append(
            _Candidate(
                kind="session",
                name=_safe_summary(name, limit=120),
                summary=_safe_summary("; ".join(str(a) for a in actions[:4])),
                source="l6_federation",
                source_agents=[agent] if agent else [],
                date_text=str(session.get("date") or "") or None,
                cwd=session.get("cwd") if isinstance(session.get("cwd"), str) else None,
                project_focus=[
                    str(f) for f in session.get("project_focus") or []
                    if isinstance(f, str) and f.strip()
                ],
                files_touched=[
                    str(p) for p in session.get("files_touched") or []
                    if isinstance(p, str) and p.strip()
                ],
            )
        )

    return candidates


def _dedupe_candidates(candidates: list[_Candidate]) -> list[_Candidate]:
    """Remove duplicate candidates by name (prefer higher-confidence source)."""
    seen: dict[str, _Candidate] = {}
    source_priority = {"convention_file": 3, "native_windsurf": 2, "workspace_context": 1, "l6_federation": 0}
    for candidate in candidates:
        key = candidate.name.lower().strip()
        if not key:
            continue
        existing = seen.get(key)
        if existing is None:
            seen[key] = candidate
        else:
            # Keep the one with higher source priority
            existing_priority = source_priority.get(existing.source, 0)
            new_priority = source_priority.get(candidate.source, 0)
            if new_priority > existing_priority:
                seen[key] = candidate
    return list(seen.values())


# -- Delivery rendering --------------------------------------------------------


def _render_explicit_text(items: list[BriefItem], max_chars: int) -> str:
    """Render items into bounded explicit pre-turn text."""
    if not items:
        return ""
    lines = ["[Bourdon Cascade Recognition Brief]", ""]
    chars = sum(len(line) for line in lines)

    for item in items:
        line = f"• {item.name} ({item.kind}): {item.summary}"
        if chars + len(line) + 1 > max_chars:
            break
        lines.append(line)
        chars += len(line) + 1

    lines.append("")
    return "\n".join(lines)


def _render_mcp_payload(brief: TurnBrief) -> dict[str, Any]:
    """Render the MCP-compatible payload."""
    return {
        "schema_version": brief.schema_version,
        "items": [item.to_dict() for item in brief.items],
        "routing": brief.routing.to_dict(),
        "health": brief.health.to_dict(),
    }


def _render_convention_file_block(items: list[BriefItem]) -> str:
    """Render an idempotent block for convention-file injection."""
    if not items:
        return ""
    lines = []
    for item in items[:10]:
        lines.append(f"- **{item.name}** ({item.kind}): {item.summary}")
    return "\n".join(lines)


# -- Public API ----------------------------------------------------------------


def compile_cascade_turn(
    prompt: str,
    *,
    cwd: str | Path | None = None,
    cascade_dir: Path | None = None,
    windsurf_data_dir: Path | None = None,
    library_path: Path | None = None,
    access_level: str = "team",
    max_items: int = 6,
    max_chars: int = 1800,
    delivery: str = "all",
) -> TurnBrief:
    """Compile a turn-scoped recognition brief for Cascade.

    Parameters
    ----------
    prompt : str
        The user prompt text (bounded at 8K chars).
    cwd : str or Path, optional
        Current working directory for repo detection and workspace enrichment.
    cascade_dir : Path, optional
        Override Cascade-Bourdon convention directory.
    windsurf_data_dir : Path, optional
        Override Windsurf application data directory.
    library_path : Path, optional
        Override agent-library path.
    access_level : str
        Visibility filter: public, team, or private.
    max_items : int
        Maximum items to include in the brief (1–20).
    max_chars : int
        Maximum characters for explicit delivery text (200–8000).
    delivery : str
        Delivery mode: explicit, mcp, convention-file, or all.

    Returns
    -------
    TurnBrief
        Compiled recognition brief.

    Raises
    ------
    ValueError
        If parameters are invalid.
    """
    # Validate inputs
    delivery = _validate_delivery(delivery)
    max_items = _bounded_int(max_items, minimum=1, maximum=20, name="max_items")
    max_chars = _bounded_int(max_chars, minimum=200, maximum=8000, name="max_chars")

    # Bound prompt
    bounded_prompt = prompt[:MAX_PROMPT_CHARS] if prompt else ""
    prompt_tokens = _tokenize(bounded_prompt)

    # Resolve CWD and detect repo
    resolved_cwd = _resolve_cwd(cwd)
    repo = _detect_repo(resolved_cwd)

    # Gather candidates from all sources
    convention_candidates = _gather_from_convention_file(cascade_dir)
    native_candidates = _gather_from_native_state(resolved_cwd, windsurf_data_dir)
    federation_candidates = _gather_from_federation(library_path, access_level)

    all_candidates = convention_candidates + native_candidates + federation_candidates
    all_candidates = _dedupe_candidates(all_candidates)

    # Score candidates
    for candidate in all_candidates:
        candidate.score = _score_candidate(candidate, prompt_tokens, repo)

    # Filter and sort
    scored = [c for c in all_candidates if c.score >= _SCORE_THRESHOLD]
    scored.sort(key=lambda c: c.score, reverse=True)
    top = scored[:max_items]

    # Build items
    items: list[BriefItem] = []
    for rank, candidate in enumerate(top, start=1):
        evidence = _build_evidence(candidate, prompt_tokens, repo)
        items.append(
            BriefItem(
                rank=rank,
                score=candidate.score,
                kind=candidate.kind,
                name=candidate.name,
                summary=candidate.summary,
                reason=f"score {candidate.score:.2f} via {candidate.source}",
                source=candidate.source,
                source_agents=candidate.source_agents,
                evidence=evidence,
            )
        )

    # Health
    convention_status = "available" if convention_candidates else "missing"
    native_status = "available" if native_candidates else "missing"
    health = BriefHealth(
        convention_file=convention_status,
        native_state=native_status,
    )

    # Routing
    if items:
        top_score = items[0].score
        if top_score >= 5.0:
            confidence = "high"
        elif top_score >= 3.0:
            confidence = "medium"
        else:
            confidence = "low"
        primary_surface = "explicit"
        reason = f"top item '{items[0].name}' scored {top_score:.2f}"
    else:
        confidence = "low"
        primary_surface = "fallback"
        reason = "no items above threshold"

    routing = BriefRouting(
        primary_surface=primary_surface,
        confidence=confidence,
        reason=reason,
    )

    # Delivery
    brief_delivery = BriefDelivery()
    if delivery in ("explicit", "all"):
        brief_delivery.explicit_text = _render_explicit_text(items, max_chars)
    if delivery in ("convention-file", "all"):
        brief_delivery.convention_file_block = _render_convention_file_block(items)

    # Build the brief first without MCP (need the brief object for MCP rendering)
    brief = TurnBrief(
        prompt=bounded_prompt,
        cwd=str(resolved_cwd) if resolved_cwd else None,
        repo=repo,
        health=health,
        routing=routing,
        items=items,
        delivery=brief_delivery,
        trace={
            "candidates_gathered": len(all_candidates),
            "candidates_scored": len(all_candidates),
            "candidates_above_threshold": len(scored),
            "scoring_method": "token_overlap_recency_affinity",
        },
        diagnostics={
            "convention_file_entities": len([c for c in convention_candidates if c.kind != "session"]),
            "convention_file_sessions": len([c for c in convention_candidates if c.kind == "session"]),
            "native_state_sessions": len(native_candidates),
            "federation_entities": len([c for c in federation_candidates if c.kind != "session"]),
            "federation_sessions": len([c for c in federation_candidates if c.kind == "session"]),
        },
    )

    if delivery in ("mcp", "all"):
        brief.delivery.mcp_payload = _render_mcp_payload(brief)

    return brief
