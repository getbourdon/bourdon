"""Turn-scoped recognition compiler for Codex.

This module builds a tiny, ranked recognition brief for one Codex turn. It is
deliberately independent of Codex native Stage 1 summarization: native health is
reported as a routing signal, but the brief is compiled from stronger surfaces
such as cwd/repo identity, local Codex thread metadata, and the L6 federation
library.
"""

from __future__ import annotations

import configparser
import json
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from adapters.codex import (
    _inspect_codex_state_db,
    _resolve_codex_home,
    _safe_native_memory_text,
)
from core.l6_store import DEFAULT_LIBRARY_PATH, L6Store

SCHEMA_VERSION = "codex-turn-brief/v1"
STRATEGY = "turn_compiled"
ACCESS_LEVELS = {"public", "team", "private"}
DELIVERY_MODES = {"explicit", "mcp", "memory-md", "fallback", "all"}
MAX_PROMPT_CHARS = 8_000
DEFAULT_MAX_ITEMS = 6
DEFAULT_MAX_CHARS = 1_800
EXHAUSTED_PATHS = [
    "native_stage1_primary",
    "static_fallback_primary",
    "l5_export_only",
    "sync_native_only",
]

_TOKEN_RE = re.compile(r"[a-zA-Z0-9]+")
_GENERIC_NAMES = {
    "memory",
    "memories",
    "notes",
    "project",
    "session",
    "thread",
    "workspace",
    "repo",
    "repository",
}
_PROMPT_STOPWORDS = {
    "a",
    "about",
    "again",
    "am",
    "an",
    "and",
    "anything",
    "are",
    "as",
    "at",
    "be",
    "can",
    "do",
    "for",
    "from",
    "how",
    "i",
    "is",
    "it",
    "keep",
    "like",
    "me",
    "new",
    "of",
    "on",
    "or",
    "please",
    "remind",
    "should",
    "tell",
    "the",
    "there",
    "to",
    "was",
    "we",
    "what",
    "whats",
    "with",
    "working",
}


@dataclass(frozen=True)
class RepoIdentity:
    name: str | None = None
    root: str | None = None
    remote: str | None = None

    def to_dict(self) -> dict[str, str | None]:
        return {"name": self.name, "root": self.root, "remote": self.remote}


@dataclass(frozen=True)
class BriefHealth:
    native_stage1: str
    strategy: str = STRATEGY

    def to_dict(self) -> dict[str, str]:
        return {
            "native_stage1": self.native_stage1,
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
        return json.dumps(self.to_dict(), indent=2, sort_keys=False) + "\n"


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
    native_stage1_only: bool = False


def compile_codex_turn(
    prompt: str,
    *,
    cwd: str | Path | None = None,
    codex_home: str | Path | None = None,
    library_path: str | Path | None = None,
    access_level: str = "team",
    max_items: int = DEFAULT_MAX_ITEMS,
    max_chars: int = DEFAULT_MAX_CHARS,
    delivery: str = "all",
) -> TurnBrief:
    """Compile a turn-scoped Codex recognition brief.

    The function is read-only. It does not write native Codex files, mutate the
    federation library, run model calls, or depend on native Stage 1 output.
    """
    prompt_text = _bounded_prompt(prompt)
    access = _validate_access_level(access_level)
    item_limit = _bounded_int(max_items, minimum=1, maximum=20, name="max_items")
    char_limit = _bounded_int(max_chars, minimum=400, maximum=6_000, name="max_chars")
    delivery_mode = _validate_delivery(delivery)
    cwd_path = _resolve_cwd(cwd)
    cwd_text = str(cwd_path) if cwd_path else None
    repo = _detect_repo(cwd_path)
    resolved_codex_home = Path(codex_home) if codex_home else _resolve_codex_home()
    resolved_library = Path(library_path) if library_path else DEFAULT_LIBRARY_PATH

    state_report = _inspect_codex_state_db(resolved_codex_home)
    native_stage1 = _classify_native_stage1(state_report)

    store = L6Store(resolved_library)
    manifest = store.build_recognition_manifest(access_level=access)
    candidates = _gather_candidates(
        prompt_text,
        manifest,
        resolved_codex_home,
    )
    scored = _score_candidates(candidates, prompt_text, cwd_path, repo)
    items = _rank_items(scored, item_limit)
    routing = _routing_decision(items, scored, repo, native_stage1)
    trace = _recognition_trace(items, scored, repo, native_stage1, routing)

    explicit_text = _render_explicit_text(items, repo, native_stage1, char_limit)
    delivery_payload = _delivery_payload(
        delivery_mode,
        explicit_text,
        items,
        repo,
        native_stage1,
    )
    diagnostics = _diagnostics(
        scored,
        state_report,
        delivery_mode,
        item_limit,
        char_limit,
    )

    return TurnBrief(
        prompt=prompt_text,
        cwd=cwd_text,
        repo=repo,
        health=BriefHealth(native_stage1=native_stage1),
        routing=routing,
        items=items,
        delivery=delivery_payload,
        trace=trace,
        diagnostics=diagnostics,
    )


def _bounded_prompt(prompt: str) -> str:
    text = str(prompt or "").strip()
    if len(text) > MAX_PROMPT_CHARS:
        return text[:MAX_PROMPT_CHARS].rstrip()
    return text


def _validate_access_level(value: str) -> str:
    if value not in ACCESS_LEVELS:
        raise ValueError(f"access_level must be one of {sorted(ACCESS_LEVELS)}")
    return value


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


def _classify_native_stage1(report: dict[str, Any]) -> str:
    if not report.get("present"):
        return "unknown"
    jobs = report.get("memory_stage1_jobs") or {}
    by_status = jobs.get("by_status") or {}
    errors = int(by_status.get("error") or 0)
    done = int(by_status.get("done") or 0)
    outputs = int((report.get("stage1_outputs") or {}).get("total") or 0)
    if errors > 0 and errors >= done:
        return "degraded"
    if done > 0 or outputs > 0:
        return "available"
    return "unknown"


def _gather_candidates(
    prompt: str,
    manifest: dict[str, Any],
    codex_home: Path | None,
) -> list[_Candidate]:
    candidates: list[_Candidate] = []
    for entity in manifest.get("known_entities") or []:
        if not isinstance(entity, dict):
            continue
        name = str(entity.get("name") or "").strip()
        if not name:
            continue
        source_agents = [
            str(agent)
            for agent in entity.get("source_agents") or []
            if isinstance(agent, str) and agent
        ]
        source = "codex_l5" if source_agents == ["codex"] else "l6_federation"
        candidates.append(
            _Candidate(
                kind=_entity_kind(entity),
                name=name,
                summary=_safe_summary(str(entity.get("summary") or "")),
                source=source,
                source_agents=source_agents,
                aliases=[
                    str(alias)
                    for alias in entity.get("aliases") or []
                    if isinstance(alias, str) and alias.strip()
                ],
                tags=[
                    str(tag)
                    for tag in entity.get("tags") or []
                    if isinstance(tag, str) and tag.strip()
                ],
                evidence=_entity_evidence(entity, source_agents),
            )
        )

    for session in manifest.get("recent_sessions") or []:
        if not isinstance(session, dict):
            continue
        name = _session_name(session)
        if not name:
            continue
        agent = str(session.get("agent") or "")
        source = "codex_l5" if agent == "codex" else "l6_federation"
        candidates.append(
            _Candidate(
                kind="session",
                name=name,
                summary=_safe_summary(_session_summary(session)),
                source=source,
                source_agents=[agent] if agent else [],
                date_text=str(session.get("date") or "") or None,
                cwd=session.get("cwd") if isinstance(session.get("cwd"), str) else None,
                project_focus=[
                    str(focus)
                    for focus in session.get("project_focus") or []
                    if isinstance(focus, str) and focus.strip()
                ],
                files_touched=[
                    str(path)
                    for path in session.get("files_touched") or []
                    if isinstance(path, str) and path.strip()
                ],
                evidence=_session_evidence(session),
            )
        )

    for record in _collect_lightweight_session_records(codex_home, limit=60):
        thread_name = str(record.get("thread_name") or "").strip()
        if not thread_name or thread_name == "(untitled)":
            continue
        source = "codex_rollout" if record.get("has_rollout") else "codex_state"
        candidates.append(
            _Candidate(
                kind="thread",
                name=_safe_summary(thread_name, limit=120),
                summary=_safe_summary(_record_summary(record)),
                source=source,
                source_agents=["codex"],
                date_text=str(record.get("date") or "") or None,
                cwd=record.get("cwd") if isinstance(record.get("cwd"), str) else None,
                files_touched=[
                    str(path)
                    for path in record.get("files_touched") or []
                    if isinstance(path, str) and path.strip()
                ],
                evidence=_record_evidence(record),
            )
        )

    return _dedupe_candidates(candidates, prompt)


def _collect_lightweight_session_records(
    codex_home: Path | None,
    *,
    limit: int,
) -> list[dict[str, Any]]:
    if codex_home is None:
        return []
    db_path = codex_home / "state_5.sqlite"
    if not db_path.is_file():
        return []
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
    except sqlite3.Error:
        return []

    try:
        if not _sqlite_table_exists(conn, "threads"):
            return []
        columns = _sqlite_table_columns(conn, "threads")
        if "id" not in columns:
            return []
        selectable = [
            column
            for column in (
                "id",
                "title",
                "first_user_message",
                "cwd",
                "rollout_path",
                "updated_at_ms",
                "updated_at",
                "created_at_ms",
                "created_at",
            )
            if column in columns
        ]
        if "title" not in selectable and "first_user_message" not in selectable:
            return []
        order_column = next(
            (
                column
                for column in (
                    "updated_at_ms",
                    "updated_at",
                    "created_at_ms",
                    "created_at",
                )
                if column in columns
            ),
            "id",
        )
        escaped_select = ", ".join(f'"{column}"' for column in selectable)
        query = f'SELECT {escaped_select} FROM threads ORDER BY "{order_column}" DESC LIMIT ?'
        rows = [dict(row) for row in conn.execute(query, (limit,)).fetchall()]
    except sqlite3.Error:
        return []
    finally:
        conn.close()

    records: list[dict[str, Any]] = []
    for row in rows:
        title = _safe_summary(str(row.get("title") or ""), limit=180)
        first_message = _safe_summary(str(row.get("first_user_message") or ""), limit=180)
        thread_name = title or first_message or "(untitled)"
        updated_at = str(
            row.get("updated_at_ms")
            or row.get("updated_at")
            or row.get("created_at_ms")
            or row.get("created_at")
            or ""
        )
        session_date = _date_from_state_timestamp(updated_at)
        if not session_date:
            continue
        records.append(
            {
                "id": str(row.get("id") or ""),
                "thread_name": thread_name,
                "updated_at": updated_at,
                "date": session_date,
                "cwd": row.get("cwd") if isinstance(row.get("cwd"), str) else None,
                "has_rollout": bool(row.get("rollout_path")),
                "files_touched": [],
            }
        )
    return records


def _sqlite_table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return row is not None


def _sqlite_table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f'PRAGMA table_info("{table}")')}


def _date_from_state_timestamp(value: str) -> str:
    text = value.strip()
    if not text:
        return ""
    try:
        number = float(text)
    except ValueError:
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return ""
    else:
        if number > 10_000_000_000:
            number = number / 1000
        parsed = datetime.fromtimestamp(number, tz=timezone.utc)
    return parsed.date().isoformat()


def _entity_kind(entity: dict[str, Any]) -> str:
    entity_type = str(entity.get("type") or "entity")
    if entity_type in {"project", "preference"}:
        return entity_type
    tags = {str(tag) for tag in entity.get("tags") or []}
    if "workflow" in tags or "handoff" in tags:
        return "handoff"
    return "entity"


def _entity_evidence(entity: dict[str, Any], source_agents: list[str]) -> list[str]:
    evidence: list[str] = []
    if source_agents:
        evidence.append(f"known by {', '.join(source_agents[:4])}")
    aliases = [str(alias) for alias in entity.get("aliases") or [] if isinstance(alias, str)]
    if aliases:
        evidence.append(f"aliases: {', '.join(aliases[:3])}")
    return evidence


def _session_name(session: dict[str, Any]) -> str:
    focus = [
        str(item)
        for item in session.get("project_focus") or []
        if isinstance(item, str) and item.strip()
    ]
    if focus:
        return focus[0]
    actions = [
        str(item)
        for item in session.get("key_actions") or []
        if isinstance(item, str) and item.strip()
    ]
    if actions:
        return actions[0]
    return ""


def _session_summary(session: dict[str, Any]) -> str:
    actions = [
        str(action)
        for action in session.get("key_actions") or []
        if isinstance(action, str) and action.strip()
    ]
    if actions:
        return "; ".join(actions[:2])
    focus = [
        str(item)
        for item in session.get("project_focus") or []
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
    files = [str(path) for path in session.get("files_touched") or [] if isinstance(path, str)]
    if files:
        evidence.append(f"files touched: {', '.join(files[:3])}")
    return evidence


def _record_summary(record: dict[str, Any]) -> str:
    parts: list[str] = []
    if record.get("cwd"):
        parts.append(f"cwd {record['cwd']}")
    files = [str(path) for path in record.get("files_touched") or [] if isinstance(path, str)]
    if files:
        parts.append(f"touched {', '.join(files[:3])}")
    concepts = [
        str(item)
        for item in record.get("fallback_concepts") or []
        if isinstance(item, str)
    ]
    if concepts:
        parts.append(f"concepts {', '.join(concepts[:3])}")
    return "; ".join(parts) if parts else "Recent Codex thread metadata."


def _record_evidence(record: dict[str, Any]) -> list[str]:
    evidence: list[str] = []
    if record.get("date"):
        evidence.append(f"thread date {record['date']}")
    if record.get("has_rollout"):
        evidence.append("rollout available")
    if record.get("cwd"):
        evidence.append(f"cwd {record['cwd']}")
    return evidence


def _dedupe_candidates(candidates: list[_Candidate], prompt: str) -> list[_Candidate]:
    by_key: dict[tuple[str, str], _Candidate] = {}
    for candidate in candidates:
        key = (candidate.kind, candidate.name.lower())
        existing = by_key.get(key)
        if existing is None:
            by_key[key] = candidate
            continue
        if _prompt_match_score(candidate, prompt)[0] > _prompt_match_score(existing, prompt)[0]:
            by_key[key] = candidate
            continue
        for agent in candidate.source_agents:
            if agent not in existing.source_agents:
                existing.source_agents.append(agent)
        for evidence in candidate.evidence:
            if evidence not in existing.evidence:
                existing.evidence.append(evidence)
    return list(by_key.values())


def _score_candidates(
    candidates: list[_Candidate],
    prompt: str,
    cwd: Path | None,
    repo: RepoIdentity,
) -> list[tuple[_Candidate, float, dict[str, float], str]]:
    scored: list[tuple[_Candidate, float, dict[str, float], str]] = []
    for candidate in candidates:
        prompt_score, prompt_reason = _prompt_match_score(candidate, prompt)
        cwd_score, cwd_reason = _cwd_score(candidate, cwd, repo)
        recency_score = _recency_score(candidate.date_text)
        cross_agent_score = _cross_agent_score(candidate.source_agents)
        continuity_score = _continuity_score(candidate, cwd, repo)
        penalty = _penalty(candidate)
        components = {
            "prompt": prompt_score,
            "cwd_repo": cwd_score,
            "recency": recency_score,
            "cross_agent": cross_agent_score,
            "continuity": continuity_score,
            "penalty": penalty,
        }
        total = sum(components.values())
        reason = _reason(prompt_reason, cwd_reason, components)
        if not _passes_recognition_gate(candidate, prompt, repo, components):
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
        [candidate.name, candidate.summary, *candidate.project_focus]
    ).lower()
    return re.search(rf"\b{re.escape(repo_name)}\b", semantic_text) is not None


def _is_vague_continuation_prompt(prompt: str) -> bool:
    tokens = set(_tokens(prompt))
    continuation_terms = {
        "continue",
        "next",
        "working",
        "work",
        "here",
        "this",
        "repo",
        "project",
        "focus",
        "resume",
        "again",
    }
    if tokens & continuation_terms:
        return True
    normalized = " ".join(_tokens(prompt))
    return normalized in {"what should i do", "what should i do next"}


def _prompt_match_score(candidate: _Candidate, prompt: str) -> tuple[float, str]:
    prompt_lower = prompt.lower()
    prompt_tokens = _tokens(prompt)
    prompt_terms = _meaningful_prompt_terms(prompt)
    names = [candidate.name] + candidate.aliases + candidate.project_focus
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
            name_terms = [token for token in name_tokens if token not in _PROMPT_STOPWORDS]
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
        token
        for token in _tokens(prompt)
        if token not in _PROMPT_STOPWORDS and len(token) > 2
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
        [candidate.name, candidate.summary, *candidate.project_focus, candidate.cwd or ""]
    ).lower()

    if candidate.cwd and cwd:
        candidate_path = Path(candidate.cwd).expanduser().resolve(strict=False)
        if _same_or_nested_path(candidate_path, cwd):
            score = max(score, 25.0)
            reasons.append("cwd matched prior Codex thread")
        elif repo.root and _same_or_nested_path(candidate_path, Path(repo.root)):
            score = max(score, 22.0)
            reasons.append("repo root matched prior Codex thread")

    if repo_name and re.search(rf"\b{re.escape(repo_name)}\b", candidate_text):
        score = max(score, 18.0)
        reasons.append("repo name matched candidate")
    if repo_name and remote and repo_name in remote and repo_name in candidate.name.lower():
        score = max(score, 14.0)
        reasons.append("git remote reinforced repo identity")
    if cwd and cwd.name.lower() in candidate_text and cwd.name.lower() not in _GENERIC_NAMES:
        score = max(score, 15.0)
        reasons.append("cwd basename matched candidate")
    return score, "; ".join(reasons)


def _same_or_nested_path(left: Path, right: Path) -> bool:
    try:
        left_resolved = left.resolve(strict=False)
        right_resolved = right.resolve(strict=False)
    except OSError:
        return False
    return left_resolved == right_resolved or right_resolved in left_resolved.parents


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
    candidate: _Candidate,
    cwd: Path | None,
    repo: RepoIdentity,
) -> float:
    score = 0.0
    if candidate.files_touched:
        score += 5.0
    if candidate.kind in {"thread", "session"} and candidate.date_text:
        score += min(5.0, _recency_score(candidate.date_text) / 3)
    if repo.name and any(repo.name.lower() in path.lower() for path in candidate.files_touched):
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
    if candidate.native_stage1_only:
        penalty -= 8.0
    if candidate.source == "codex_l5" and candidate.date_text:
        age = _age_days(candidate.date_text)
        if age is not None and age > 30:
            penalty -= 4.0
    return penalty


def _rank_items(
    scored: list[tuple[_Candidate, float, dict[str, float], str]],
    max_items: int,
) -> list[BriefItem]:
    items: list[BriefItem] = []
    for rank, (candidate, score, _components, reason) in enumerate(scored[:max_items], start=1):
        items.append(
            BriefItem(
                rank=rank,
                score=score,
                kind=candidate.kind,
                name=_safe_summary(candidate.name, limit=120),
                summary=_safe_summary(candidate.summary or "Recognition anchor.", limit=260),
                reason=reason,
                source=candidate.source,
                source_agents=candidate.source_agents,
                evidence=[_safe_summary(item, limit=160) for item in candidate.evidence[:4]],
            )
        )
    return items


def _render_explicit_text(
    items: list[BriefItem],
    repo: RepoIdentity,
    native_stage1: str,
    max_chars: int,
) -> str:
    lines = [
        "Bourdon turn recognition brief",
        f"Strategy: turn-scoped compiler; native Stage 1 is {native_stage1}.",
    ]
    if repo.name:
        lines.append(f"Repo: {repo.name}")
    if not items:
        lines.append("No high-confidence recognition anchors found for this turn.")
    else:
        lines.append("Use these as recognition anchors, not as a final answer:")
        for item in items:
            line = f"{item.rank}. {item.name} [{item.kind}, {item.source}, score {item.score:.1f}]"
            if item.source_agents:
                line += f" via {', '.join(item.source_agents[:4])}"
            lines.append(line)
            lines.append(f"   {item.summary}")
            lines.append(f"   Why: {item.reason}")
    text = "\n".join(lines).strip()
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 3].rstrip() + "..."


def _routing_decision(
    items: list[BriefItem],
    scored: list[tuple[_Candidate, float, dict[str, float], str]],
    repo: RepoIdentity,
    native_stage1: str,
) -> dict[str, Any]:
    if not items:
        return {
            "mode": "observe",
            "primary_surface": "none",
            "surfaces": [],
            "confidence": "none",
            "reason": "no high-confidence recognition anchors",
            "suppressed_surfaces": ["native_stage1", "memory_md", "fallback_file"],
            "next_action": "continue without recognition injection",
        }

    top_item = items[0]
    confidence = _confidence(top_item.score)
    surfaces = _recommended_surfaces(top_item, repo, native_stage1)
    suppressed = _suppressed_surfaces(native_stage1, confidence)
    return {
        "mode": "inject",
        "primary_surface": surfaces[0],
        "surfaces": surfaces,
        "confidence": confidence,
        "reason": _routing_reason(top_item, repo, native_stage1, scored),
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
    native_stage1: str,
) -> list[str]:
    surfaces = ["explicit_pre_turn"]
    if top_item.score >= 35:
        surfaces.append("mcp")
    if repo.name and top_item.score >= 45:
        surfaces.append("repo_overlay_candidate")
    if native_stage1 == "available" and top_item.score >= 60:
        surfaces.append("native_memory_supporting")
    return surfaces


def _suppressed_surfaces(native_stage1: str, confidence: str) -> list[str]:
    suppressed: list[str] = []
    if native_stage1 == "degraded":
        suppressed.append("native_stage1_primary")
    if confidence == "low":
        suppressed.extend(["memory_md", "fallback_file"])
    return suppressed


def _routing_reason(
    top_item: BriefItem,
    repo: RepoIdentity,
    native_stage1: str,
    scored: list[tuple[_Candidate, float, dict[str, float], str]],
) -> str:
    source_count = len({candidate.source for candidate, _score, _components, _reason in scored})
    pieces = [
        f"top anchor scored {top_item.score:.1f}",
        f"source mix spans {source_count} surface(s)",
    ]
    if repo.name:
        pieces.append(f"repo identity available as {repo.name}")
    if native_stage1 == "degraded":
        pieces.append("native Stage 1 is degraded, so active injection is preferred")
    return "; ".join(pieces)


def _routing_next_action(primary_surface: str) -> str:
    if primary_surface == "explicit_pre_turn":
        return "prepend delivery.explicit_text before the Codex turn"
    if primary_surface == "mcp":
        return "return delivery.mcp_payload to the MCP caller"
    return "use routing.surfaces to choose the strongest available channel"


def _recognition_trace(
    items: list[BriefItem],
    scored: list[tuple[_Candidate, float, dict[str, float], str]],
    repo: RepoIdentity,
    native_stage1: str,
    routing: dict[str, Any],
) -> dict[str, Any]:
    selected_names = {(item.kind, item.name.lower()) for item in items}
    selected = []
    ignored_sources: dict[str, int] = {}
    candidate_source_mix: dict[str, int] = {}
    selected_source_mix: dict[str, int] = {}

    for item in items:
        selected_source_mix[item.source] = selected_source_mix.get(item.source, 0) + 1

    for candidate, score, components, reason in scored:
        candidate_source_mix[candidate.source] = candidate_source_mix.get(candidate.source, 0) + 1
        key = (candidate.kind, candidate.name.lower())
        if key in selected_names:
            selected.append(
                {
                    "name": candidate.name,
                    "kind": candidate.kind,
                    "score": round(score, 1),
                    "dominant_components": _dominant_components(components),
                    "reason": reason,
                }
            )
        else:
            ignored_sources[candidate.source] = ignored_sources.get(candidate.source, 0) + 1

    return {
        "routing_decision": {
            "primary_surface": routing["primary_surface"],
            "confidence": routing["confidence"],
            "reason": routing["reason"],
        },
        "surface_health": {
            "native_stage1": native_stage1,
            "repo_identity": "available" if repo.name else "missing",
            "candidate_count": len(scored),
        },
        "source_mix": {
            "candidates": dict(sorted(candidate_source_mix.items())),
            "selected": dict(sorted(selected_source_mix.items())),
            "ignored": dict(sorted(ignored_sources.items())),
        },
        "selected_items": selected[:10],
    }


def _dominant_components(components: dict[str, float]) -> list[str]:
    positive = [
        (name, value)
        for name, value in components.items()
        if name != "penalty" and value > 0
    ]
    positive.sort(key=lambda item: (-item[1], item[0]))
    return [name for name, _value in positive[:3]]


def _delivery_payload(
    delivery_mode: str,
    explicit_text: str,
    items: list[BriefItem],
    repo: RepoIdentity,
    native_stage1: str,
) -> dict[str, Any]:
    mcp_payload = {
        "schema_version": SCHEMA_VERSION,
        "strategy": STRATEGY,
        "native_stage1": native_stage1,
        "repo": repo.to_dict(),
        "items": [item.to_dict() for item in items],
        "prompt_context": explicit_text,
    }
    memory_block = _bounded_memory_block(explicit_text, "BOURDON TURN BRIEF")
    fallback_block = _bounded_memory_block(explicit_text, "Bourdon Turn Brief")
    repo_overlay_block = _repo_overlay_block(explicit_text, repo, items)
    payload = {
        "explicit_text": explicit_text if delivery_mode in {"explicit", "all"} else "",
        "mcp_payload": mcp_payload if delivery_mode in {"mcp", "all"} else {},
        "memory_md_block": memory_block if delivery_mode in {"memory-md", "all"} else "",
        "fallback_block": fallback_block if delivery_mode in {"fallback", "all"} else "",
        "repo_overlay_block": repo_overlay_block if delivery_mode == "all" else "",
    }
    return payload


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
    lines.extend(
        [
            "Use only as an explicit, human-reviewed overlay candidate.",
            "",
            explicit_text,
            "<!-- END BOURDON REPO OVERLAY CANDIDATE -->",
        ]
    )
    return "\n".join(lines)


def _bounded_memory_block(text: str, title: str) -> str:
    return f"<!-- BEGIN {title} -->\n{text}\n<!-- END {title} -->"


def _diagnostics(
    scored: list[tuple[_Candidate, float, dict[str, float], str]],
    state_report: dict[str, Any],
    delivery_mode: str,
    max_items: int,
    max_chars: int,
) -> dict[str, Any]:
    components = {
        candidate.name: {key: round(value, 1) for key, value in components.items()}
        for candidate, _score, components, _reason in scored[:max_items]
    }
    return {
        "scoring_components": components,
        "candidate_count": len(scored),
        "delivery": delivery_mode,
        "max_items": max_items,
        "max_chars": max_chars,
        "stage1_jobs": _stage1_job_summary(state_report),
        "exhausted_paths": EXHAUSTED_PATHS,
    }


def _stage1_job_summary(state_report: dict[str, Any]) -> dict[str, Any]:
    jobs = state_report.get("memory_stage1_jobs") or {}
    error_classes: dict[str, int] = {}
    for error in jobs.get("errors") or []:
        text = str(error.get("last_error") or "").lower()
        if "usage limit" in text:
            key = "usage_limit"
        elif "context window" in text or "ran out of room" in text:
            key = "context_window"
        elif text:
            key = "other"
        else:
            key = "unknown"
        error_classes[key] = error_classes.get(key, 0) + 1
    return {
        "total": int(jobs.get("total") or 0),
        "by_status": dict(jobs.get("by_status") or {}),
        "error_classes": dict(sorted(error_classes.items())),
    }


def _reason(
    prompt_reason: str,
    cwd_reason: str,
    components: dict[str, float],
) -> str:
    reasons = [reason for reason in (prompt_reason, cwd_reason) if reason]
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
    return [match.group(0).lower() for match in _TOKEN_RE.finditer(value)]


def _contains_subsequence(haystack: list[str], needle: list[str]) -> bool:
    if not needle or len(needle) > len(haystack):
        return False
    width = len(needle)
    return any(
        haystack[index : index + width] == needle
        for index in range(len(haystack) - width + 1)
    )


def _safe_summary(value: str, limit: int = 220) -> str:
    return _safe_native_memory_text(value, limit=limit)


def _age_days(date_text: str) -> int | None:
    parsed = _parse_date(date_text)
    if parsed is None:
        return None
    return max(0, (datetime.now(timezone.utc).date() - parsed).days)


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
    if len(text) >= 10 and text[4] == "-" and text[7] == "-":
        try:
            return date.fromisoformat(text[:10])
        except ValueError:
            return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        return None
