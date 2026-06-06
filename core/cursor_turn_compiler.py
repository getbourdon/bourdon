"""Turn-scoped recognition compiler for Cursor.

Builds a compact recognition brief for one Cursor turn. Draws from:

1. The workspace cwd (project identity)
2. The L6 federation library (cross-agent entities matching the prompt)
3. The Cursor SQLite adapter's health status

The brief is intentionally small: a ranked list of entities and a one-line
routing decision. It is designed to be consumed by Cursor's system prompt
or by a Bourdon MCP tool response.
"""

from __future__ import annotations

import re
import time as _time
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from adapters.codex import _safe_native_memory_text
from core.l6_store import DEFAULT_LIBRARY_PATH, L6Store

SCHEMA_VERSION = "cursor-turn-brief/v1"
STRATEGY = "turn_compiled"
DEFAULT_MAX_ITEMS = 6
DEFAULT_MAX_CHARS = 1_800

_TOKEN_RE = re.compile(r"[a-zA-Z0-9]+")
_PROMPT_STOPWORDS = {
    "a", "about", "again", "am", "an", "and", "anything", "are", "as", "at",
    "be", "can", "do", "for", "from", "how", "i", "is", "it", "keep", "like",
    "me", "new", "of", "on", "or", "our", "please", "so", "some", "that",
    "the", "this", "to", "us", "want", "was", "we", "what", "when", "where",
    "which", "will", "with", "would", "you",
}


@dataclass
class CursorTurnBrief:
    """The compiled recognition brief for one Cursor turn."""

    schema_version: str = SCHEMA_VERSION
    strategy: str = STRATEGY
    prompt_tokens: list[str] = field(default_factory=list)
    cwd_project: str = ""
    matched_entities: list[dict[str, Any]] = field(default_factory=list)
    routing: dict[str, str] = field(default_factory=dict)
    compile_latency_us: float = 0.0

    def to_text(self, max_chars: int = DEFAULT_MAX_CHARS) -> str:
        """Render the brief as a human-readable text block."""
        lines: list[str] = []
        if self.cwd_project:
            lines.append(f"Project: {self.cwd_project}")
        if self.matched_entities:
            lines.append("Federation context:")
            for entity in self.matched_entities:
                name = entity.get("name", "?")
                agent = entity.get("agent", "?")
                summary = entity.get("summary", "")
                line = f"  - {name} (via {agent})"
                if summary:
                    line += f": {_safe_native_memory_text(summary, limit=160)}"
                lines.append(line)
        conf = self.routing.get("confidence", "none")
        lines.append(f"Confidence: {conf}")
        text = "\n".join(lines)
        if len(text) > max_chars:
            text = text[: max_chars - 3].rstrip() + "..."
        return text


def _extract_prompt_tokens(prompt: str) -> list[str]:
    """Extract meaningful tokens from the prompt."""
    tokens = []
    for match in _TOKEN_RE.finditer(prompt.lower()):
        token = match.group()
        if len(token) >= 3 and token not in _PROMPT_STOPWORDS:
            tokens.append(token)
    return tokens


def _project_from_cwd(cwd: str | None) -> str:
    """Extract a project name from a working directory path."""
    if not cwd:
        return ""
    name = Path(cwd).name.strip()
    return name if name and name not in {".", "/", "~"} else ""


def _score_entity(
    entity: dict[str, Any],
    prompt_tokens: list[str],
    cwd_project: str,
) -> float:
    """Score an entity for relevance to the current turn."""
    score = 0.0
    name = str(entity.get("name", "")).lower()
    summary = str(entity.get("summary", "")).lower()
    aliases = [str(a).lower() for a in entity.get("aliases", [])]
    searchable = f"{name} {summary} {' '.join(aliases)}"

    for token in prompt_tokens:
        if token in searchable:
            score += 2.0
    if cwd_project and cwd_project.lower() in searchable:
        score += 3.0

    last_touched = str(entity.get("last_touched", ""))
    if last_touched:
        try:
            entity_date = date.fromisoformat(last_touched)
            days_ago = (date.today() - entity_date).days
            if days_ago <= 7:
                score += 1.0
            elif days_ago <= 30:
                score += 0.5
        except ValueError:
            pass

    return score


def compile_cursor_turn(
    prompt: str,
    *,
    cwd: str | None = None,
    access_level: str = "team",
    library_path: Path | None = None,
    max_items: int = DEFAULT_MAX_ITEMS,
    max_chars: int = DEFAULT_MAX_CHARS,
) -> CursorTurnBrief:
    """Compile a turn-scoped recognition brief for Cursor."""
    t0 = _time.perf_counter()

    prompt_tokens = _extract_prompt_tokens(prompt)
    cwd_project = _project_from_cwd(cwd)

    lib = library_path or DEFAULT_LIBRARY_PATH
    store = L6Store(lib)
    agents = store.list_agents()

    scored_entities: list[tuple[float, str, dict[str, Any]]] = []
    for agent_id in agents:
        manifest = store.get_agent_manifest(agent_id)
        if not manifest:
            continue
        for entity in manifest.get("known_entities") or []:
            vis = str(entity.get("visibility", "team")).lower()
            if access_level == "public" and vis not in {"public"}:
                continue
            if access_level == "team" and vis not in {"public", "team"}:
                continue
            score = _score_entity(entity, prompt_tokens, cwd_project)
            if score > 0:
                scored_entities.append((score, agent_id, entity))

    scored_entities.sort(key=lambda t: t[0], reverse=True)
    top = scored_entities[:max_items]

    matched = []
    for score, agent_id, entity in top:
        matched.append({
            "name": entity.get("name", ""),
            "type": entity.get("type", "topic"),
            "agent": agent_id,
            "summary": entity.get("summary", ""),
            "score": round(score, 2),
        })

    if not matched:
        confidence = "none"
    elif top[0][0] >= 4.0:
        confidence = "high"
    elif top[0][0] >= 2.0:
        confidence = "medium"
    else:
        confidence = "low"

    elapsed_us = (_time.perf_counter() - t0) * 1_000_000

    return CursorTurnBrief(
        prompt_tokens=prompt_tokens,
        cwd_project=cwd_project,
        matched_entities=matched,
        routing={"confidence": confidence, "strategy": STRATEGY},
        compile_latency_us=round(elapsed_us, 1),
    )
