"""Turn-scoped recognition compiler for Cascade (Windsurf).

This module builds a tiny, ranked recognition brief for one Cascade turn.
It is deliberately independent of Cascade's convention-file memory: native
Windsurf state health is reported as a routing signal, but the brief is
compiled from stronger surfaces such as cwd/repo identity, Windsurf editor
session metadata, workspace plans/workflows, and the L6 federation library.

Architecture mirrors ``core/codex_turn_compiler.py`` but adapted for
Windsurf:
- Native health = Windsurf state.vscdb availability (not Codex Stage 1)
- Local records = Cascade editor sessions + .windsurf/plans + workflows
- Federation = same L6 store shared across all participants
"""

from __future__ import annotations

import configparser
import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import yaml
from participants._windsurf_native import (
    NativeWindsurfState,
    read_native_windsurf_state,
)

from core.l6_store import DEFAULT_LIBRARY_PATH, L6Store
from participants.codex import _safe_native_memory_text

SCHEMA_VERSION = "cascade-turn-brief/v1"
STRATEGY = "turn_compiled"
ACCESS_LEVELS = {"public", "team", "private"}
DELIVERY_MODES = {"explicit", "mcp", "memory-md", "fallback", "all"}
MAX_PROMPT_CHARS = 8_000
DEFAULT_MAX_ITEMS = 6
DEFAULT_MAX_CHARS = 1_800
EXHAUSTED_PATHS = [
    "native_state_primary",
    "convention_file_only",
    "l5_export_only",
    "sync_native_only",
]

_TOKEN_RE = re.compile(r"[a-zA-Z0-9]+")
_GENERIC_NAMES = {
    "memory", "memories", "notes", "project", "session",
    "thread", "workspace", "repo", "repository",
}
_PROMPT_STOPWORDS = {
    "a", "about", "again", "am", "an", "and", "anything",
    "are", "as", "at", "be", "can", "do", "for", "from",
    "how", "i", "is", "it", "keep", "like", "me", "new",
    "of", "on", "or", "please", "remind", "should", "tell",
    "the", "there", "to", "was", "we", "what", "whats",
    "with", "working",
}


# -- Data classes --------------------------------------------------------------


@dataclass(frozen=True)
class RepoIdentity:
    name: str | None = None
    root: str | None = None
    remote: str | None = None

    def to_dict(self) -> dict[str, str | None]:
        return {
            "name": self.name, "root": self.root,
            "remote": self.remote,
        }


@dataclass(frozen=True)
class BriefHealth:
    native_state: str
    strategy: str = STRATEGY

    def to_dict(self) -> dict[str, str]:
        return {
            "native_state": self.native_state,
            "strategy": self.strategy,
        }


@dataclass
class BriefItem:
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
            "score": round(self.score, 1),
            "kind": self.kind,
            "name": self.name,
            "summary": self.summary,
            "reason": self.reason,
            "source": self.source,
            "source_agents": self.source_agents,
            "evidence": self.evidence,
        }


@dataclass
class TurnBrief:
    prompt: str
    cwd: str | None
    repo: RepoIdentity
    health: BriefHealth
    routing: dict[str, Any]
    items: list[BriefItem]
    delivery: dict[str, Any]
    trace: dict[str, Any]
    diagnostics: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "prompt": self.prompt,
            "cwd": self.cwd,
            "repo": self.repo.to_dict(),
            "health": self.health.to_dict(),
            "routing": self.routing,
            "items": [item.to_dict() for item in self.items],
            "delivery": self.delivery,
            "trace": self.trace,
            "diagnostics": self.diagnostics,
        }

    def to_yaml(self) -> str:
        return yaml.safe_dump(self.to_dict(), sort_keys=False)

    def to_json(self) -> str:
        return json.dumps(
            self.to_dict(), indent=2, sort_keys=False,
        ) + "\n"


@dataclass
class _Candidate:
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
    native_only: bool = False


# -- Public API ----------------------------------------------------------------


def compile_cascade_turn(
    prompt: str,
    *,
    cwd: str | Path | None = None,
    windsurf_data_dir: str | Path | None = None,
    library_path: str | Path | None = None,
    access_level: str = "team",
    max_items: int = DEFAULT_MAX_ITEMS,
    max_chars: int = DEFAULT_MAX_CHARS,
    delivery: str = "all",
) -> TurnBrief:
    """Compile a turn-scoped Cascade recognition brief.

    Read-only. Does not write native Windsurf files, mutate the federation
    library, run model calls, or depend on native state being healthy.
    """
    prompt_text = _bounded_prompt(prompt)
    access = _validate_access_level(access_level)
    item_limit = _bounded_int(
        max_items, minimum=1, maximum=20, name="max_items",
    )
    char_limit = _bounded_int(
        max_chars, minimum=400, maximum=6_000, name="max_chars",
    )
    delivery_mode = _validate_delivery(delivery)
    cwd_path = _resolve_cwd(cwd)
    cwd_text = str(cwd_path) if cwd_path else None
    repo = _detect_repo(cwd_path)

    resolved_data_dir = (
        Path(windsurf_data_dir) if windsurf_data_dir else None
    )
    resolved_library = (
        Path(library_path) if library_path else DEFAULT_LIBRARY_PATH
    )

    native_state = read_native_windsurf_state(
        windsurf_data_dir=resolved_data_dir, cwd=cwd_path,
    )
    native_health = _classify_native_state(native_state)

    store = L6Store(resolved_library)
    manifest = store.build_recognition_manifest(access_level=access)
    candidates = _gather_candidates(
        prompt_text, manifest, native_state,
    )
    scored = _score_candidates(
        candidates, prompt_text, cwd_path, repo,
    )
    items = _rank_items(scored, item_limit)
    routing = _routing_decision(items, scored, repo, native_health)
    trace = _recognition_trace(
        items, scored, repo, native_health, routing,
    )

    explicit_text = _render_explicit_text(
        items, repo, native_health, char_limit,
    )
    delivery_payload = _delivery_payload(
        delivery_mode, explicit_text, items, repo, native_health,
    )
    diagnostics = _diagnostics(
        scored, native_state, delivery_mode, item_limit, char_limit,
    )

    return TurnBrief(
        prompt=prompt_text,
        cwd=cwd_text,
        repo=repo,
        health=BriefHealth(native_state=native_health),
        routing=routing,
        items=items,
        delivery=delivery_payload,
        trace=trace,
        diagnostics=diagnostics,
    )


# -- Validation ----------------------------------------------------------------


def _bounded_prompt(prompt: str) -> str:
    text = str(prompt or "").strip()
    if len(text) > MAX_PROMPT_CHARS:
        return text[:MAX_PROMPT_CHARS].rstrip()
    return text


def _validate_access_level(value: str) -> str:
    if value not in ACCESS_LEVELS:
        raise ValueError(
            f"access_level must be one of {sorted(ACCESS_LEVELS)}"
        )
    return value


def _validate_delivery(value: str) -> str:
    if value not in DELIVERY_MODES:
        raise ValueError(
            f"delivery must be one of {sorted(DELIVERY_MODES)}"
        )
    return value


def _bounded_int(
    value: int, *, minimum: int, maximum: int, name: str,
) -> int:
    number = int(value)
    if number < minimum or number > maximum:
        raise ValueError(
            f"{name} must be between {minimum} and {maximum}"
        )
    return number


def _resolve_cwd(cwd: str | Path | None) -> Path | None:
    if cwd is None:
        return Path.cwd()
    text = str(cwd).strip()
    if not text:
        return None
    return Path(text).expanduser().resolve(strict=False)


# -- Repo detection ------------------------------------------------------------


def _detect_repo(cwd: Path | None) -> RepoIdentity:
    if cwd is None:
        return RepoIdentity()
    root = _find_git_root(cwd)
    if root is None:
        return RepoIdentity(
            name=cwd.name or None, root=None, remote=None,
        )
    remote = _read_git_origin(root)
    return RepoIdentity(
        name=root.name, root=str(root), remote=remote,
    )


def _find_git_root(path: Path) -> Path | None:
    current = path if path.is_dir() else path.parent
    for candidate in (current, *current.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


def _read_git_origin(root: Path) -> str | None:
    git_path = root / ".git"
    config_path = (
        git_path / "config" if git_path.is_dir() else None
    )
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
    return (
        remote.strip()
        if isinstance(remote, str) and remote.strip()
        else None
    )


# -- Native health classification ---------------------------------------------


def _classify_native_state(state: NativeWindsurfState) -> str:
    if not state.available:
        return "unknown"
    if state.errors:
        return "degraded"
    return "available"


# -- Candidate gathering -------------------------------------------------------


def _gather_candidates(
    prompt: str,
    manifest: dict[str, Any],
    native_state: NativeWindsurfState,
) -> list[_Candidate]:
    candidates: list[_Candidate] = []

    # L6 federation entities
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
        source = (
            "cascade_l5"
            if source_agents == ["cascade"]
            else "l6_federation"
        )
        candidates.append(
            _Candidate(
                kind=_entity_kind(entity),
                name=name,
                summary=_safe_summary(
                    str(entity.get("summary") or ""),
                ),
                source=source,
                source_agents=source_agents,
                aliases=[
                    str(a) for a in entity.get("aliases") or []
                    if isinstance(a, str) and a.strip()
                ],
                tags=[
                    str(t) for t in entity.get("tags") or []
                    if isinstance(t, str) and t.strip()
                ],
                evidence=_entity_evidence(entity, source_agents),
            )
        )

    # L6 federation sessions
    for session in manifest.get("recent_sessions") or []:
        if not isinstance(session, dict):
            continue
        name = _session_name(session)
        if not name:
            continue
        agent = str(session.get("agent") or "")
        source = (
            "cascade_l5"
            if agent == "cascade"
            else "l6_federation"
        )
        candidates.append(
            _Candidate(
                kind="session",
                name=name,
                summary=_safe_summary(_session_summary(session)),
                source=source,
                source_agents=[agent] if agent else [],
                date_text=(
                    str(session.get("date") or "") or None
                ),
                cwd=(
                    session.get("cwd")
                    if isinstance(session.get("cwd"), str)
                    else None
                ),
                project_focus=[
                    str(f) for f in session.get("project_focus") or []
                    if isinstance(f, str) and f.strip()
                ],
                files_touched=[
                    str(p) for p in session.get("files_touched") or []
                    if isinstance(p, str) and p.strip()
                ],
                evidence=_session_evidence(session),
            )
        )

    # Native Windsurf: Cascade editor sessions
    for session in native_state.cascade_sessions:
        title = _safe_summary(session.title, limit=120)
        if not title or title == "Cascade":
            continue
        candidates.append(
            _Candidate(
                kind="thread",
                name=title,
                summary=_safe_summary(
                    f"Cascade session: {session.title}",
                ),
                source="windsurf_native",
                source_agents=["cascade"],
                evidence=["windsurf editor session"],
                native_only=True,
            )
        )

    # Native Windsurf: plans
    for plan in native_state.plans:
        name = _safe_summary(plan.title, limit=120)
        if not name:
            continue
        candidates.append(
            _Candidate(
                kind="plan",
                name=name,
                summary=_safe_summary(
                    plan.content_preview or name, limit=200,
                ),
                source="windsurf_workspace",
                source_agents=["cascade"],
                evidence=[f"plan file: {plan.filename}"],
            )
        )

    # Native Windsurf: workflows
    for workflow in native_state.workflows:
        name = _safe_summary(
            workflow.filename.removesuffix(".md"), limit=120,
        )
        if not name:
            continue
        candidates.append(
            _Candidate(
                kind="workflow",
                name=name,
                summary=_safe_summary(
                    workflow.description or name, limit=200,
                ),
                source="windsurf_workspace",
                source_agents=["cascade"],
                evidence=[f"workflow file: {workflow.filename}"],
            )
        )

    return _dedupe_candidates(candidates, prompt)


def _entity_kind(entity: dict[str, Any]) -> str:
    entity_type = str(entity.get("type") or "entity")
    if entity_type in {"project", "preference"}:
        return entity_type
    tags = {str(t) for t in entity.get("tags") or []}
    if "workflow" in tags or "handoff" in tags:
        return "handoff"
    return "entity"


def _entity_evidence(
    entity: dict[str, Any], source_agents: list[str],
) -> list[str]:
    evidence: list[str] = []
    if source_agents:
        evidence.append(
            f"known by {', '.join(source_agents[:4])}"
        )
    aliases = [
        str(a) for a in entity.get("aliases") or []
        if isinstance(a, str)
    ]
    if aliases:
        evidence.append(f"aliases: {', '.join(aliases[:3])}")
    return evidence


def _session_name(session: dict[str, Any]) -> str:
    focus = [
        str(item) for item in session.get("project_focus") or []
        if isinstance(item, str) and item.strip()
    ]
    if focus:
        return focus[0]
    actions = [
        str(item) for item in session.get("key_actions") or []
        if isinstance(item, str) and item.strip()
    ]
    if actions:
        return actions[0]
    return ""


def _session_summary(session: dict[str, Any]) -> str:
    actions = [
        str(a) for a in session.get("key_actions") or []
        if isinstance(a, str) and a.strip()
    ]
    if actions:
        return "; ".join(actions[:2])
    focus = [
        str(item) for item in session.get("project_focus") or []
        if isinstance(item, str) and item.strip()
    ]
    if focus:
        return f"Recent work focused on {', '.join(focus[:3])}."
    return "Recent federated work item."


def _session_evidence(session: dict[str, Any]) -> list[str]:
    evidence: list[str] = []
    if session.get("date"):
        evidence.append(f"session date {session['date']}")
    if session.get("cwd"):
        evidence.append(f"cwd {session['cwd']}")
    files = [
        str(p) for p in session.get("files_touched") or []
        if isinstance(p, str)
    ]
    if files:
        evidence.append(
            f"files touched: {', '.join(files[:3])}"
        )
    return evidence


def _dedupe_candidates(
    candidates: list[_Candidate], prompt: str,
) -> list[_Candidate]:
    by_key: dict[tuple[str, str], _Candidate] = {}
    for candidate in candidates:
        key = (candidate.kind, candidate.name.lower())
        existing = by_key.get(key)
        if existing is None:
            by_key[key] = candidate
            continue
        new_score = _prompt_match_score(candidate, prompt)[0]
        old_score = _prompt_match_score(existing, prompt)[0]
        if new_score > old_score:
            by_key[key] = candidate
            continue
        for agent in candidate.source_agents:
            if agent not in existing.source_agents:
                existing.source_agents.append(agent)
        for ev in candidate.evidence:
            if ev not in existing.evidence:
                existing.evidence.append(ev)
    return list(by_key.values())


# -- Scoring -------------------------------------------------------------------


def _score_candidates(
    candidates: list[_Candidate],
    prompt: str,
    cwd: Path | None,
    repo: RepoIdentity,
) -> list[tuple[_Candidate, float, dict[str, float], str]]:
    scored: list[
        tuple[_Candidate, float, dict[str, float], str]
    ] = []
    for candidate in candidates:
        ps, pr = _prompt_match_score(candidate, prompt)
        cs, cr = _cwd_score(candidate, cwd, repo)
        rs = _recency_score(candidate.date_text)
        cas = _cross_agent_score(candidate.source_agents)
        cos = _continuity_score(candidate, cwd, repo)
        penalty = _penalty(candidate)
        components = {
            "prompt": ps,
            "cwd_repo": cs,
            "recency": rs,
            "cross_agent": cas,
            "continuity": cos,
            "penalty": penalty,
        }
        total = sum(components.values())
        reason = _reason(pr, cr, components)
        if not _passes_recognition_gate(
            candidate, prompt, repo, components,
        ):
            continue
        if total <= 0:
            continue
        scored.append((candidate, total, components, reason))
    scored.sort(
        key=lambda row: (
            -row[1],
            -_date_sort_value(row[0].date_text),
            row[0].name.lower(),
            row[0].source,
        )
    )
    return scored


def _passes_recognition_gate(
    candidate: _Candidate,
    prompt: str,
    repo: RepoIdentity,
    components: dict[str, float],
) -> bool:
    if components["prompt"] > 0:
        return True
    if not _is_vague_continuation_prompt(prompt):
        return False
    if candidate.kind == "thread":
        return False
    repo_name = (repo.name or "").lower()
    if not repo_name:
        return False
    semantic_text = " ".join(
        [candidate.name, candidate.summary,
         *candidate.project_focus]
    ).lower()
    return (
        re.search(
            rf"\b{re.escape(repo_name)}\b", semantic_text,
        )
        is not None
    )


def _is_vague_continuation_prompt(prompt: str) -> bool:
    tokens = set(_tokens(prompt))
    continuation_terms = {
        "continue", "next", "working", "work", "here",
        "this", "repo", "project", "focus", "resume", "again",
    }
    if tokens & continuation_terms:
        return True
    normalized = " ".join(_tokens(prompt))
    return normalized in {
        "what should i do", "what should i do next",
    }


def _prompt_match_score(
    candidate: _Candidate, prompt: str,
) -> tuple[float, str]:
    prompt_lower = prompt.lower()
    prompt_tokens = _tokens(prompt)
    prompt_terms = _meaningful_prompt_terms(prompt)
    names = (
        [candidate.name]
        + candidate.aliases
        + candidate.project_focus
    )
    best = 0.0
    best_name = ""
    for name in names:
        name_lower = name.lower().strip()
        if not name_lower:
            continue
        name_tokens = _tokens(name)
        if prompt_lower == name_lower:
            score = 40.0
        elif name_lower in prompt_lower:
            score = 36.0
        elif _contains_subsequence(prompt_tokens, name_tokens):
            score = 30.0
        else:
            name_terms = [
                t for t in name_tokens
                if t not in _PROMPT_STOPWORDS
            ]
            overlap = len(set(prompt_terms) & set(name_terms))
            score = min(24.0, overlap * 12.0)
        if score > best:
            best = score
            best_name = name
    if best:
        return best, f"prompt matched {best_name}"
    return 0.0, ""


def _meaningful_prompt_terms(prompt: str) -> list[str]:
    return [
        t for t in _tokens(prompt)
        if t not in _PROMPT_STOPWORDS and len(t) > 2
    ]


def _cwd_score(
    candidate: _Candidate,
    cwd: Path | None,
    repo: RepoIdentity,
) -> tuple[float, str]:
    if cwd is None and not repo.name:
        return 0.0, ""
    score = 0.0
    reasons: list[str] = []
    repo_name = (repo.name or "").lower()
    remote = (repo.remote or "").lower()
    candidate_text = " ".join(
        [candidate.name, candidate.summary,
         *candidate.project_focus, candidate.cwd or ""]
    ).lower()

    if candidate.cwd and cwd:
        candidate_path = Path(
            candidate.cwd,
        ).expanduser().resolve(strict=False)
        if _same_or_nested_path(candidate_path, cwd):
            score = max(score, 25.0)
            reasons.append("cwd matched prior Cascade session")
        elif repo.root and _same_or_nested_path(
            candidate_path, Path(repo.root),
        ):
            score = max(score, 22.0)
            reasons.append(
                "repo root matched prior Cascade session"
            )

    if repo_name and re.search(
        rf"\b{re.escape(repo_name)}\b", candidate_text,
    ):
        score = max(score, 18.0)
        reasons.append("repo name matched candidate")
    if (
        repo_name and remote
        and repo_name in remote
        and repo_name in candidate.name.lower()
    ):
        score = max(score, 14.0)
        reasons.append("git remote reinforced repo identity")
    if (
        cwd
        and cwd.name.lower() in candidate_text
        and cwd.name.lower() not in _GENERIC_NAMES
    ):
        score = max(score, 15.0)
        reasons.append("cwd basename matched candidate")
    return score, "; ".join(reasons)


def _same_or_nested_path(left: Path, right: Path) -> bool:
    try:
        lr = left.resolve(strict=False)
        rr = right.resolve(strict=False)
    except OSError:
        return False
    return lr == rr or rr in lr.parents


def _recency_score(date_text: str | None) -> float:
    if not date_text:
        return 0.0
    age = _age_days(date_text)
    if age is None:
        return 0.0
    if age <= 1:
        return 15.0
    if age <= 7:
        return 12.0
    if age <= 30:
        return 8.0
    if age <= 90:
        return 4.0
    return 1.0


def _cross_agent_score(source_agents: list[str]) -> float:
    count = len(set(source_agents))
    if count <= 1:
        return 0.0
    return min(10.0, 4.0 + (count - 1) * 2.0)


def _continuity_score(
    candidate: _Candidate, cwd: Path | None, repo: RepoIdentity,
) -> float:
    score = 0.0
    if candidate.files_touched:
        score += 5.0
    if (
        candidate.kind in {"thread", "session"}
        and candidate.date_text
    ):
        score += min(5.0, _recency_score(candidate.date_text) / 3)
    if repo.name and any(
        repo.name.lower() in p.lower()
        for p in candidate.files_touched
    ):
        score += 3.0
    if cwd and candidate.cwd:
        score += 2.0
    return min(10.0, score)


def _penalty(candidate: _Candidate) -> float:
    penalty = 0.0
    if candidate.name.lower() in _GENERIC_NAMES:
        penalty -= 10.0
    if len(candidate.summary) > 260:
        penalty -= 4.0
    if candidate.native_only:
        penalty -= 8.0
    if candidate.source == "cascade_l5" and candidate.date_text:
        age = _age_days(candidate.date_text)
        if age is not None and age > 30:
            penalty -= 4.0
    return penalty


# -- Ranking + routing ---------------------------------------------------------


def _rank_items(
    scored: list[tuple[_Candidate, float, dict[str, float], str]],
    max_items: int,
) -> list[BriefItem]:
    items: list[BriefItem] = []
    for rank, (candidate, score, _c, reason) in enumerate(
        scored[:max_items], start=1,
    ):
        items.append(
            BriefItem(
                rank=rank,
                score=score,
                kind=candidate.kind,
                name=_safe_summary(candidate.name, limit=120),
                summary=_safe_summary(
                    candidate.summary or "Recognition anchor.",
                    limit=260,
                ),
                reason=reason,
                source=candidate.source,
                source_agents=candidate.source_agents,
                evidence=[
                    _safe_summary(e, limit=160)
                    for e in candidate.evidence[:4]
                ],
            )
        )
    return items


def _routing_decision(
    items: list[BriefItem],
    scored: list[tuple[_Candidate, float, dict[str, float], str]],
    repo: RepoIdentity,
    native_state: str,
) -> dict[str, Any]:
    if not items:
        return {
            "mode": "observe",
            "primary_surface": "none",
            "surfaces": [],
            "confidence": "none",
            "reason": "no high-confidence recognition anchors",
            "suppressed_surfaces": [
                "native_state", "memory_md", "fallback_file",
            ],
            "next_action": (
                "continue without recognition injection"
            ),
        }

    top = items[0]
    confidence = _confidence(top.score)
    surfaces = _recommended_surfaces(top, repo, native_state)
    suppressed = _suppressed_surfaces(native_state, confidence)
    return {
        "mode": "inject",
        "primary_surface": surfaces[0],
        "surfaces": surfaces,
        "confidence": confidence,
        "reason": _routing_reason(
            top, repo, native_state, scored,
        ),
        "suppressed_surfaces": suppressed,
        "next_action": _routing_next_action(surfaces[0]),
    }


def _confidence(score: float) -> str:
    if score >= 50:
        return "high"
    if score >= 25:
        return "medium"
    return "low"


def _recommended_surfaces(
    top_item: BriefItem,
    repo: RepoIdentity,
    native_state: str,
) -> list[str]:
    surfaces = ["explicit_pre_turn"]
    if top_item.score >= 35:
        surfaces.append("mcp")
    if repo.name and top_item.score >= 45:
        surfaces.append("repo_overlay_candidate")
    if native_state == "available" and top_item.score >= 60:
        surfaces.append("native_memory_supporting")
    return surfaces


def _suppressed_surfaces(
    native_state: str, confidence: str,
) -> list[str]:
    suppressed: list[str] = []
    if native_state == "degraded":
        suppressed.append("native_state_primary")
    if confidence == "low":
        suppressed.extend(["memory_md", "fallback_file"])
    return suppressed


def _routing_reason(
    top_item: BriefItem,
    repo: RepoIdentity,
    native_state: str,
    scored: list[tuple[_Candidate, float, dict[str, float], str]],
) -> str:
    source_count = len({
        c.source for c, _s, _co, _r in scored
    })
    pieces = [
        f"top anchor scored {top_item.score:.1f}",
        f"source mix spans {source_count} surface(s)",
    ]
    if repo.name:
        pieces.append(
            f"repo identity available as {repo.name}"
        )
    if native_state == "degraded":
        pieces.append(
            "native Windsurf state is degraded, "
            "active injection preferred"
        )
    return "; ".join(pieces)


def _routing_next_action(primary_surface: str) -> str:
    if primary_surface == "explicit_pre_turn":
        return (
            "prepend delivery.explicit_text before "
            "the Cascade turn"
        )
    if primary_surface == "mcp":
        return "return delivery.mcp_payload to the MCP caller"
    return (
        "use routing.surfaces to choose the "
        "strongest available channel"
    )


# -- Delivery ------------------------------------------------------------------


def _render_explicit_text(
    items: list[BriefItem],
    repo: RepoIdentity,
    native_state: str,
    max_chars: int,
) -> str:
    lines = [
        "Bourdon turn recognition brief",
        f"Strategy: turn-scoped compiler; "
        f"native Windsurf state is {native_state}.",
    ]
    if repo.name:
        lines.append(f"Repo: {repo.name}")
    if not items:
        lines.append(
            "No high-confidence recognition anchors "
            "found for this turn."
        )
    else:
        lines.append(
            "Use these as recognition anchors, "
            "not as a final answer:"
        )
        for item in items:
            line = (
                f"{item.rank}. {item.name} "
                f"[{item.kind}, {item.source}, "
                f"score {item.score:.1f}]"
            )
            if item.source_agents:
                line += (
                    f" via {', '.join(item.source_agents[:4])}"
                )
            lines.append(line)
            lines.append(f"   {item.summary}")
            lines.append(f"   Why: {item.reason}")
    text = "\n".join(lines).strip()
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 3].rstrip() + "..."


def _delivery_payload(
    delivery_mode: str,
    explicit_text: str,
    items: list[BriefItem],
    repo: RepoIdentity,
    native_state: str,
) -> dict[str, Any]:
    mcp_payload = {
        "schema_version": SCHEMA_VERSION,
        "strategy": STRATEGY,
        "native_state": native_state,
        "repo": repo.to_dict(),
        "items": [item.to_dict() for item in items],
        "prompt_context": explicit_text,
    }
    memory_block = _bounded_memory_block(
        explicit_text, "BOURDON TURN BRIEF",
    )
    fallback_block = _bounded_memory_block(
        explicit_text, "Bourdon Turn Brief",
    )
    repo_overlay = _repo_overlay_block(
        explicit_text, repo, items,
    )
    return {
        "explicit_text": (
            explicit_text
            if delivery_mode in {"explicit", "all"}
            else ""
        ),
        "mcp_payload": (
            mcp_payload
            if delivery_mode in {"mcp", "all"}
            else {}
        ),
        "memory_md_block": (
            memory_block
            if delivery_mode in {"memory-md", "all"}
            else ""
        ),
        "fallback_block": (
            fallback_block
            if delivery_mode in {"fallback", "all"}
            else ""
        ),
        "repo_overlay_block": (
            repo_overlay if delivery_mode == "all" else ""
        ),
    }


def _repo_overlay_block(
    explicit_text: str,
    repo: RepoIdentity,
    items: list[BriefItem],
) -> str:
    if not repo.name or not items:
        return ""
    lines = [
        "<!-- BEGIN BOURDON REPO OVERLAY CANDIDATE -->",
        f"Repo overlay candidate for {repo.name}",
    ]
    if repo.root:
        lines.append(f"Repo root: {repo.root}")
    if repo.remote:
        lines.append(f"Remote: {repo.remote}")
    lines.extend([
        "Use only as an explicit, human-reviewed "
        "overlay candidate.",
        "",
        explicit_text,
        "<!-- END BOURDON REPO OVERLAY CANDIDATE -->",
    ])
    return "\n".join(lines)


def _bounded_memory_block(text: str, title: str) -> str:
    return (
        f"<!-- BEGIN {title} -->\n{text}\n"
        f"<!-- END {title} -->"
    )


# -- Trace + diagnostics -------------------------------------------------------


def _recognition_trace(
    items: list[BriefItem],
    scored: list[tuple[_Candidate, float, dict[str, float], str]],
    repo: RepoIdentity,
    native_state: str,
    routing: dict[str, Any],
) -> dict[str, Any]:
    selected_names = {
        (item.kind, item.name.lower()) for item in items
    }
    selected = []
    ignored_sources: dict[str, int] = {}
    candidate_source_mix: dict[str, int] = {}
    selected_source_mix: dict[str, int] = {}

    for item in items:
        selected_source_mix[item.source] = (
            selected_source_mix.get(item.source, 0) + 1
        )

    for candidate, score, components, reason in scored:
        candidate_source_mix[candidate.source] = (
            candidate_source_mix.get(candidate.source, 0) + 1
        )
        key = (candidate.kind, candidate.name.lower())
        if key in selected_names:
            selected.append({
                "name": candidate.name,
                "kind": candidate.kind,
                "score": round(score, 1),
                "dominant_components": _dominant_components(
                    components,
                ),
                "reason": reason,
            })
        else:
            ignored_sources[candidate.source] = (
                ignored_sources.get(candidate.source, 0) + 1
            )

    return {
        "routing_decision": {
            "primary_surface": routing["primary_surface"],
            "confidence": routing["confidence"],
            "reason": routing["reason"],
        },
        "surface_health": {
            "native_state": native_state,
            "repo_identity": (
                "available" if repo.name else "missing"
            ),
            "candidate_count": len(scored),
        },
        "source_mix": {
            "candidates": dict(
                sorted(candidate_source_mix.items())
            ),
            "selected": dict(
                sorted(selected_source_mix.items())
            ),
            "ignored": dict(
                sorted(ignored_sources.items())
            ),
        },
        "selected_items": selected[:10],
    }


def _dominant_components(
    components: dict[str, float],
) -> list[str]:
    positive = [
        (name, value)
        for name, value in components.items()
        if name != "penalty" and value > 0
    ]
    positive.sort(key=lambda item: (-item[1], item[0]))
    return [name for name, _v in positive[:3]]


def _diagnostics(
    scored: list[tuple[_Candidate, float, dict[str, float], str]],
    native_state: NativeWindsurfState,
    delivery_mode: str,
    max_items: int,
    max_chars: int,
) -> dict[str, Any]:
    components = {
        c.name: {
            k: round(v, 1) for k, v in comps.items()
        }
        for c, _s, comps, _r in scored[:max_items]
    }
    return {
        "scoring_components": components,
        "candidate_count": len(scored),
        "delivery": delivery_mode,
        "max_items": max_items,
        "max_chars": max_chars,
        "native_state": native_state.to_dict(),
        "exhausted_paths": EXHAUSTED_PATHS,
    }


# -- Shared utilities ----------------------------------------------------------


def _reason(
    prompt_reason: str,
    cwd_reason: str,
    components: dict[str, float],
) -> str:
    reasons = [r for r in (prompt_reason, cwd_reason) if r]
    if components["cross_agent"] > 0:
        reasons.append("cross-agent agreement")
    if components["recency"] >= 8:
        reasons.append("recent work")
    if components["continuity"] > 0:
        reasons.append("continuity evidence")
    if not reasons:
        reasons.append("weak but available recognition signal")
    return "; ".join(reasons)


def _tokens(value: str) -> list[str]:
    return [
        match.group(0).lower()
        for match in _TOKEN_RE.finditer(value)
    ]


def _contains_subsequence(
    haystack: list[str], needle: list[str],
) -> bool:
    if not needle or len(needle) > len(haystack):
        return False
    width = len(needle)
    return any(
        haystack[i : i + width] == needle
        for i in range(len(haystack) - width + 1)
    )


def _safe_summary(value: str, limit: int = 220) -> str:
    return _safe_native_memory_text(value, limit=limit)


def _age_days(date_text: str) -> int | None:
    parsed = _parse_date(date_text)
    if parsed is None:
        return None
    return max(
        0, (datetime.now(timezone.utc).date() - parsed).days,
    )


def _date_sort_value(date_text: str | None) -> int:
    if not date_text:
        return 0
    parsed = _parse_date(date_text)
    if parsed is None:
        return 0
    return parsed.toordinal()


def _parse_date(date_text: str) -> date | None:
    text = str(date_text or "").strip()
    if not text:
        return None
    if (
        len(text) >= 10
        and text[4] == "-"
        and text[7] == "-"
    ):
        try:
            return date.fromisoformat(text[:10])
        except ValueError:
            return None
    try:
        return datetime.fromisoformat(
            text.replace("Z", "+00:00"),
        ).date()
    except ValueError:
        return None
