"""
``bourdon dogfood`` -- end-to-end smoke test of the federation on a real machine.

What it does
------------
1. Generates a unique marker entity name (``BourdonDogfood-<short uuid>``).
2. For convention-file adapters (Copilot, Cascade) whose ``memory.md`` exists
   on this machine, appends the marker as a TEAM-visibility entity.
3. Runs ``export_l5()`` on every registered adapter against the real local
   stores, writing each manifest into an ephemeral ``agent-library/``.
4. Loads the manifests into ``L6Store`` and queries for the marker at the
   federation's realistic access level (default ``team``).
5. Prints a per-adapter matrix of (discovered, planted, exported, surfaced).
6. Removes the planted marker entries unless ``--keep-marker`` was passed.

Design choices
--------------
- **Plants only in convention-file adapters.** Cursor's SQLite and Codex's
  session index are owned by their IDE/CLI; writing to them is invasive.
  Claude Code's ``~/claude-brain`` is a real git repo; planting there commits
  to history if cleanup races. Those three adapters appear in the matrix as
  "snapshot" rows -- we export what's there but don't manipulate it.
- **Ephemeral library.** The exports land in a ``tempfile.mkdtemp`` directory
  that's deleted on exit. The user's real ``~/agent-library/`` is untouched.
- **Idempotent cleanup.** The cleanup filters the marker out of the
  ``entities`` list by exact name match. If the user manually added an
  entity with the same name (vanishingly unlikely given the UUID suffix),
  it would also be removed -- this is documented in the marker scheme.
- **Best-effort.** Any single adapter failure is reported in the matrix
  but does not abort the run. Exit code is 0 if every plantable adapter
  round-tripped its marker, 1 otherwise.

This is Layer 2 of the cross-agent test plan documented in
``claude-brain/PROJECTS/NEUROLAYER/NOTES.md`` (2026-05-11 entry). Layer 1
lives in ``tests/test_federation_roundtrip.py``; Layer 3 (the public
acceptance scenario) is a docs-and-config artifact, not code.
"""

from __future__ import annotations

import logging
import shutil
import tempfile
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import yaml

from adapters.base import Visibility
from adapters.cascade import CascadeAdapter, default_cascade_memory_path
from adapters.claude_code import ClaudeCodeAdapter
from adapters.codex import CodexAdapter
from adapters.copilot import CopilotAdapter, default_copilot_memory_path
from adapters.cursor import CursorAdapter
from core.codex_context import filter_manifest_for_access
from core.l5_io import write_l5_dict
from core.l6_store import L6Store

logger = logging.getLogger(__name__)

# Adapters dogfood will attempt to plant in. The rest are exported and
# observed but never written to.
PLANTABLE_AGENTS = {"copilot", "cascade"}

# Adapter registry kept in sync with cli.main._ADAPTER_REGISTRY. Mirroring
# rather than importing avoids a circular-import surface in main.py.
_REGISTRY: list[tuple[str, type]] = [
    ("claude-code", ClaudeCodeAdapter),
    ("codex", CodexAdapter),
    ("cursor", CursorAdapter),
    ("copilot", CopilotAdapter),
    ("cascade", CascadeAdapter),
]


@dataclass
class AdapterReport:
    """Per-adapter result row for the dogfood matrix."""

    agent_id: str
    plantable: bool
    discovered: bool = False
    planted: bool | None = None   # None when not plantable
    exported: bool = False
    surfaced: bool | None = None  # None when nothing to look for
    notes: list[str] = field(default_factory=list)


@dataclass
class DogfoodReport:
    """Full dogfood run result."""

    marker: str
    library: str
    access_level: str
    started_at: str
    finished_at: str
    adapters: list[AdapterReport]
    passed: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "marker": self.marker,
            "library": self.library,
            "access_level": self.access_level,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "passed": self.passed,
            "adapters": [asdict(r) for r in self.adapters],
        }


def _make_marker() -> str:
    """Unique-enough marker name for one dogfood run."""
    return f"BourdonDogfood-{uuid.uuid4().hex[:12]}"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _convention_file_path(agent_id: str) -> Path:
    if agent_id == "copilot":
        return default_copilot_memory_path()
    if agent_id == "cascade":
        return default_cascade_memory_path()
    raise ValueError(f"no convention file known for {agent_id}")


def _read_memory_file(path: Path) -> tuple[dict[str, Any], str]:
    """Return (front_matter_dict, freeform_body) parsed from a convention file.

    Treats a missing file or absent front-matter as empty FM with empty body.
    Body retains its leading newline so a round-trip write is byte-identical
    when nothing is changed.
    """
    if not path.exists():
        return {}, ""
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        return {}, text
    close = text.find("\n---\n", len("---\n"))
    if close == -1:
        return {}, text
    fm_text = text[len("---\n") : close]
    body = text[close + len("\n---\n") :]
    try:
        fm = yaml.safe_load(fm_text) or {}
    except yaml.YAMLError as exc:
        logger.warning("Could not parse front-matter at %s: %s", path, exc)
        return {}, text
    if not isinstance(fm, dict):
        return {}, text
    return fm, body


def _write_memory_file(path: Path, front_matter: dict[str, Any], body: str) -> None:
    """Write a convention file with front-matter + body. Atomic via l5_io idiom."""
    fm_text = yaml.safe_dump(front_matter, sort_keys=False, default_flow_style=False)
    text = f"---\n{fm_text}---\n{body}"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def _plant_marker_in_convention_file(path: Path, marker: str) -> None:
    """Append a marker entity to the convention file's entities list."""
    fm, body = _read_memory_file(path)
    entities = fm.get("entities") or []
    if not isinstance(entities, list):
        entities = []
    entities.append(
        {
            "name": marker,
            "type": "project",
            "summary": "Bourdon dogfood marker. Safe to remove if found stranded.",
            "tags": ["bourdon-dogfood"],
            # Explicit TEAM so the marker is visible at team access regardless
            # of the adapter's default-policy resolution.
            "visibility": Visibility.TEAM.value,
        }
    )
    fm["entities"] = entities
    fm.setdefault("sessions", [])
    _write_memory_file(path, fm, body)


def _remove_marker_from_convention_file(path: Path, marker: str) -> bool:
    """Filter the marker out of the entities list. Returns True if anything changed."""
    fm, body = _read_memory_file(path)
    entities = fm.get("entities") or []
    if not isinstance(entities, list):
        return False
    new_entities = [
        e
        for e in entities
        if not (isinstance(e, dict) and e.get("name") == marker)
    ]
    if len(new_entities) == len(entities):
        return False
    fm["entities"] = new_entities
    _write_memory_file(path, fm, body)
    return True


def _export_one(
    agent_id: str,
    adapter_cls: type,
    library_agents_dir: Path,
    access_level: str,
) -> tuple[bool, bool, list[str]]:
    """
    Return (discovered, exported, notes) for one adapter against real local state.

    Discovery is a soft signal: ``adapter.discover()`` may raise
    ``AdapterDiscoveryError`` if no native store is present on this machine,
    which is the normal "agent isn't installed here" case. We surface it as
    ``discovered=False`` without aborting the run.
    """
    notes: list[str] = []
    adapter = adapter_cls()
    try:
        adapter.discover()
        discovered = True
    except Exception as exc:  # noqa: BLE001 -- adapters raise their own subclass
        notes.append(f"discover: {exc}")
        discovered = False

    try:
        manifest = adapter.export_l5()
        data = filter_manifest_for_access(manifest, access_level=access_level)
        write_l5_dict(data, library_agents_dir / f"{agent_id}.l5.yaml")
        exported = True
    except Exception as exc:  # noqa: BLE001 -- best-effort smoke test
        notes.append(f"export: {exc}")
        exported = False

    return discovered, exported, notes


def run_dogfood(
    *,
    keep_marker: bool = False,
    access_level: str = "team",
    library_dir: Path | None = None,
) -> DogfoodReport:
    """
    Execute one end-to-end dogfood run. Pure function; CLI handler is a
    thin wrapper that prints the result.

    Parameters
    ----------
    keep_marker
        If True, planted markers are NOT removed at the end. Useful for
        debugging or to leave a trail an external agent can pick up.
    access_level
        L6Store query access level. Defaults to ``team`` because three of
        five adapters default-tag entities as TEAM and ``public`` queries
        would silently miss them. See ``claude-brain`` Finding 2 for context.
    library_dir
        Override the ephemeral library path. When None (default), a fresh
        tempdir is created and removed on exit.
    """
    started = _now()
    marker = _make_marker()

    own_library = library_dir is None
    library = library_dir or Path(tempfile.mkdtemp(prefix="bourdon-dogfood-"))
    agents_dir = library / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)

    cleanups: list[Callable[[], None]] = []
    reports: dict[str, AdapterReport] = {
        agent_id: AdapterReport(agent_id=agent_id, plantable=(agent_id in PLANTABLE_AGENTS))
        for agent_id, _ in _REGISTRY
    }

    try:
        # Phase 1: plant marker in convention-file adapters whose memory.md exists.
        for agent_id in PLANTABLE_AGENTS:
            rep = reports[agent_id]
            try:
                path = _convention_file_path(agent_id)
            except ValueError as exc:
                rep.notes.append(f"plant-skip: {exc}")
                rep.planted = False
                continue
            if not path.exists():
                rep.notes.append(f"plant-skip: convention file absent at {path}")
                rep.planted = False
                continue
            try:
                _plant_marker_in_convention_file(path, marker)
                rep.planted = True

                def _cleanup(p: Path = path, m: str = marker) -> None:
                    _remove_marker_from_convention_file(p, m)

                if not keep_marker:
                    cleanups.append(_cleanup)
            except Exception as exc:  # noqa: BLE001
                rep.notes.append(f"plant: {exc}")
                rep.planted = False

        # Phase 2: export every adapter to the ephemeral library.
        for agent_id, adapter_cls in _REGISTRY:
            discovered, exported, notes = _export_one(
                agent_id, adapter_cls, agents_dir, access_level
            )
            rep = reports[agent_id]
            rep.discovered = discovered
            rep.exported = exported
            rep.notes.extend(notes)

        # Phase 3: query L6 for the marker.
        store = L6Store(library_path=library)
        store.reload_all()
        matches = store.find_entity(marker, access_level=access_level)
        agents_with_marker = {a for m in matches for a in m.agents}

        # Surfaced is meaningful only where we planted -- everywhere else it's None.
        passed = True
        for agent_id, rep in reports.items():
            if rep.plantable and rep.planted:
                rep.surfaced = agent_id in agents_with_marker
                if not rep.surfaced:
                    passed = False
            else:
                rep.surfaced = None

        # If we couldn't plant in any plantable adapter, the test isn't meaningful.
        any_planted = any(r.planted for r in reports.values() if r.plantable)
        if not any_planted:
            passed = False

        finished = _now()
        return DogfoodReport(
            marker=marker,
            library=str(library),
            access_level=access_level,
            started_at=started,
            finished_at=finished,
            adapters=[reports[a] for a, _ in _REGISTRY],
            passed=passed,
        )
    finally:
        for c in cleanups:
            try:
                c()
            except Exception as exc:  # noqa: BLE001
                logger.warning("cleanup raised: %s", exc)
        if own_library and not keep_marker:
            shutil.rmtree(library, ignore_errors=True)


def format_matrix(report: DogfoodReport) -> str:
    """Render the dogfood report as a human-readable matrix."""
    rows = []
    header = f"{'agent':<14} {'discovered':<11} {'planted':<9} {'exported':<9} {'surfaced':<9} notes"
    rows.append(header)
    rows.append("-" * len(header))

    def _cell(value: bool | None) -> str:
        if value is None:
            return "  --"
        return "  OK " if value else " FAIL"

    for r in report.adapters:
        notes = "; ".join(r.notes) if r.notes else ""
        rows.append(
            f"{r.agent_id:<14} "
            f"{_cell(r.discovered):<11} "
            f"{_cell(r.planted):<9} "
            f"{_cell(r.exported):<9} "
            f"{_cell(r.surfaced):<9} "
            f"{notes}"
        )
    rows.append("")
    rows.append(f"marker: {report.marker}")
    rows.append(f"library: {report.library}")
    rows.append(f"access:  {report.access_level}")
    rows.append(f"result:  {'PASS' if report.passed else 'FAIL'}")
    return "\n".join(rows)
