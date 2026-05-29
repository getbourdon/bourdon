"""Turn-scoped recognition compiler for Cascade (Windsurf).

A thin wrapper over the agent-agnostic engine in ``core/turn_compiler.py``. This
reconciles Cascade's standalone ``cascade-turn-brief/v1`` compiler onto the
shared ``SessionSource`` engine — the third caller after Codex and Claude — so
all three agents share one scorer, router, and brief shape.

The Cascade-specific surface lives in ``CascadeSessionSource``:

- **native state health + local records** come from the Windsurf on-disk state
  (``adapters/_windsurf_native.read_native_windsurf_state``): Cascade editor
  sessions, active ``.windsurf/plans``, and ``.windsurf/workflows``. These are
  the live, not-yet-federated signal — the analogue of Codex's local threads
  and Claude's local transcripts.
- Cascade's **convention-file memory** (``~/.cascade-bourdon/memory.md``) flows
  in via the L6 federation library once exported (``bourdon cascade export``),
  exactly as Codex/Claude L5 does — so it is not re-read here.

Scoring is the shared additive scorer (uniform cross-agent ranking); Cascade's
former weight-based scorer is dropped. All reads are read-only. See
``docs/turn-compiler-architecture.md``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from adapters._windsurf_native import (
    NativeWindsurfState,
    _default_windsurf_data_dir,
    read_native_windsurf_state,
)
from core.turn_compiler import (  # re-exported for backwards compatibility
    DEFAULT_MAX_CHARS,
    DEFAULT_MAX_ITEMS,
    TurnBrief,
    _Candidate,
    _resolve_cwd,
    _safe_summary,
    compile_turn,
)

SCHEMA_VERSION = "cascade-turn-brief/v1"
EXHAUSTED_PATHS = [
    "native_state_primary",
    "convention_file_only",
    "l5_export_only",
]

__all__ = [
    "compile_cascade_turn",
    "CascadeSessionSource",
    "SCHEMA_VERSION",
    "EXHAUSTED_PATHS",
]


class CascadeSessionSource:
    """Cascade (Windsurf)-specific seam for the turn compiler.

    Native health + local records are read from Windsurf's on-disk state. The
    source carries ``cwd`` because ``.windsurf/plans`` and ``.windsurf/workflows``
    enrichment is workspace-relative; the read is memoized so ``inspect_native``
    and ``collect_local_records`` (which the engine always calls in that order)
    open the state DB once.
    """

    agent_id = "cascade"
    agent_display = "Cascade"
    schema_version = SCHEMA_VERSION
    l5_source_label = "cascade_l5"
    native_health_key = "native_state"
    native_health_noun = "native Windsurf state"
    local_record_noun = "Cascade session"
    exhausted_paths = EXHAUSTED_PATHS

    def __init__(self, *, cwd: str | Path | None = None) -> None:
        self._cwd = _resolve_cwd(cwd)
        self._cache: dict[str, NativeWindsurfState] = {}

    def _state(self, home: Path | None) -> NativeWindsurfState:
        key = str(home) if home else ""
        if key not in self._cache:
            self._cache[key] = read_native_windsurf_state(
                windsurf_data_dir=home, cwd=self._cwd
            )
        return self._cache[key]

    def resolve_home(self, override: str | Path | None) -> Path | None:
        """Resolve the Windsurf application data dir (override-able for tests)."""
        if override:
            return Path(override)
        return _default_windsurf_data_dir()

    def inspect_native(self, home: Path | None) -> dict[str, Any]:
        state = self._state(home)
        return {
            "available": state.available,
            "errors": list(state.errors),
            "summary": state.to_dict(),
        }

    def classify_native(self, report: dict[str, Any]) -> str:
        if not report.get("available"):
            return "unknown"
        if report.get("errors"):
            return "degraded"
        return "available"

    def collect_local_records(
        self, home: Path | None, *, limit: int
    ) -> list[_Candidate]:
        state = self._state(home)
        if not state.available:
            return []
        candidates: list[_Candidate] = []

        # Cascade editor sessions -> thread (gated like Codex/Claude local
        # records: they only surface on a direct prompt match, not vague
        # continuations).
        for session in state.cascade_sessions:
            title = _safe_summary(session.title, limit=120)
            if not title:
                continue
            candidates.append(
                _Candidate(
                    kind="thread",
                    name=title,
                    summary=_safe_summary(f"Cascade session: {session.title}"),
                    source="windsurf_native",
                    source_agents=["cascade"],
                    evidence=["windsurf editor session"],
                )
            )

        # Active plans -> plan (can surface on a vague prompt when they name the
        # current repo).
        for plan in state.plans:
            name = _safe_summary(plan.title, limit=120)
            if not name:
                continue
            candidates.append(
                _Candidate(
                    kind="plan",
                    name=name,
                    summary=_safe_summary(plan.content_preview or name, limit=200),
                    source="windsurf_workspace",
                    source_agents=["cascade"],
                    evidence=[f"plan file: {plan.filename}"],
                )
            )

        # Workflows -> workflow.
        for workflow in state.workflows:
            name = _safe_summary(workflow.filename.removesuffix(".md"), limit=120)
            if not name:
                continue
            candidates.append(
                _Candidate(
                    kind="workflow",
                    name=name,
                    summary=_safe_summary(workflow.description or name, limit=200),
                    source="windsurf_workspace",
                    source_agents=["cascade"],
                    evidence=[f"workflow file: {workflow.filename}"],
                )
            )

        return candidates[:limit]

    def native_diagnostics(self, report: dict[str, Any]) -> dict[str, Any]:
        return {"native_state": report.get("summary") or {}}


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

    The function is read-only. It does not write native Windsurf files, mutate
    the federation library, run model calls, or depend on native state health.
    """
    return compile_turn(
        prompt,
        source=CascadeSessionSource(cwd=cwd),
        cwd=cwd,
        home=windsurf_data_dir,
        library_path=library_path,
        access_level=access_level,
        max_items=max_items,
        max_chars=max_chars,
        delivery=delivery,
    )
