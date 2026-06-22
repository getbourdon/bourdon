"""Turn-scoped recognition compiler for Cursor.

Builds a compact recognition brief for one Cursor turn. Draws from the L6
federation library (cross-agent entities matching the prompt) and the
workspace cwd (project identity). Designed to be consumed by Cursor's
system prompt or by a Bourdon MCP tool response.
"""

from __future__ import annotations

import time as _time
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from core.l6_store import DEFAULT_LIBRARY_PATH, L6Store
from core.recognition_contract import meaningful_terms, tokenize
from participants.codex import _safe_native_memory_text

SCHEMA_VERSION = "cursor-turn-brief/v1"
STRATEGY = "turn_compiled"
DEFAULT_MAX_ITEMS = 6
DEFAULT_MAX_CHARS = 1_800
# Input bounds (3-Star audit tests-P1-2): a negative max_items silently dropped
# the top-ranked entity via scored_entities[:negative]; an unbounded prompt let
# a multi-MB input drive O(tokens*entities) scanning. Mirror the codex compiler.
MAX_ITEMS_CEILING = 20
MAX_PROMPT_CHARS = 8_000

# Tokenizer + stopwords now live in the shared recognition contract, consumed
# via meaningful_terms() — removes the codex/cursor stopword-list drift.


@dataclass
class CursorTurnBrief:
    schema_version: str = SCHEMA_VERSION
    strategy: str = STRATEGY
    prompt_tokens: list[str] = field(default_factory=list)
    cwd_project: str = ""
    matched_entities: list[dict[str, Any]] = field(default_factory=list)
    routing: dict[str, str] = field(default_factory=dict)
    compile_latency_us: float = 0.0

    def to_text(self, max_chars: int = DEFAULT_MAX_CHARS) -> str:
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
    # Canonical stopwords + len>=3, from the shared recognition contract.
    return meaningful_terms(prompt)


def _project_from_cwd(cwd: str | None) -> str:
    if not cwd:
        return ""
    name = Path(cwd).name.strip()
    return name if name and name not in {".", "/", "~"} else ""


def _score_entity(
    entity: dict[str, Any],
    prompt_tokens: list[str],
    cwd_project: str,
) -> float:
    score = 0.0
    name = str(entity.get("name", "")).lower()
    aliases = [str(a).lower() for a in entity.get("aliases", [])]

    # Match DECISION on NAME + ALIASES only — NOT the summary. Scoring against a
    # summary haystack surfaced unrelated entities (a prompt about "memory"
    # matched anything whose summary mentioned memory, and out-ranked the named
    # "memory" entity). Whole-token (not substring) match, mirroring the shared
    # recognition contract (3-Star tests-P1-1).
    name_alias_tokens = set(tokenize(name))
    for alias in aliases:
        name_alias_tokens.update(tokenize(alias))
    name_alias_text = f"{name} {' '.join(aliases)}"

    for token in prompt_tokens:
        if token in name_alias_tokens:
            score += 2.0
    if cwd_project and cwd_project.lower() in name_alias_text:
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
    t0 = _time.perf_counter()

    # Clamp inputs before use (tests-P1-2): a negative/zero max_items would
    # mis-slice the ranked list; an oversized prompt would scan unbounded.
    try:
        max_items = max(1, min(int(max_items), MAX_ITEMS_CEILING))
    except (TypeError, ValueError):
        max_items = DEFAULT_MAX_ITEMS
    if isinstance(prompt, str) and len(prompt) > MAX_PROMPT_CHARS:
        prompt = prompt[:MAX_PROMPT_CHARS]

    prompt_tokens = _extract_prompt_tokens(prompt)
    cwd_project = _project_from_cwd(cwd)

    lib = library_path or DEFAULT_LIBRARY_PATH
    store = L6Store(lib)
    agents = store.list_agents()

    scored_entities: list[tuple[float, str, dict[str, Any]]] = []
    for agent_id in agents:
        manifest = store.get_agent_manifest(agent_id, access_level=access_level)
        if not manifest:
            continue
        for entity in manifest.get("known_entities") or []:
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
