"""
Bourdon federation staging — quarantined writes await operator review (v0.9.0).

Quarantined members' ``commit_to_federation`` calls never touch the live
store. They land here instead::

    ~/agent-library/
    +-- agents/                      <- live store (L6Store globs ONLY this)
    +-- staging/
        +-- <caller_agent_id>/
            +-- <agent_id>.l5.yaml   <- staged contribution

``L6Store`` never reads ``staging/``, so staged content is invisible to every
read tool on every transport until the operator promotes it::

    bourdon staging list
    bourdon staging promote <agent>
    bourdon staging reject <agent>

Promotion merges the staged manifest into the live store through the same
``L6Store.commit_l5(mode="merge")`` path trusted writes use, then deletes the
staged file. Rejection just deletes.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from core.l5_io import write_l5_dict

logger = logging.getLogger(__name__)

STAGING_DIRNAME = "staging"


@dataclass
class StagedWrite:
    """One staged contribution awaiting review."""

    caller: str
    agent_id: str
    path: Path
    staged_at: datetime
    entities: int
    sessions: int

    @property
    def age_days(self) -> float:
        return (datetime.now(timezone.utc) - self.staged_at).total_seconds() / 86400.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "caller": self.caller,
            "agent_id": self.agent_id,
            "path": str(self.path),
            "staged_at": self.staged_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "age_days": round(self.age_days, 1),
            "entities": self.entities,
            "sessions": self.sessions,
        }


def staging_root(library_path: Path) -> Path:
    return Path(library_path) / STAGING_DIRNAME


def stage_manifest(library_path: Path, caller: str, manifest: dict[str, Any]) -> Path:
    """Write a quarantined contribution into staging (atomic). Returns the path.

    ``manifest`` is a full L5 dict (same shape ``commit_l5`` builds); the
    agent id is read from ``manifest["agent"]["id"]``.
    """
    agent_id = str(((manifest.get("agent") or {}).get("id")) or "").strip()
    if not agent_id:
        raise ValueError("staged manifest is missing agent.id")
    dest = staging_root(library_path) / caller / f"{agent_id}.l5.yaml"
    dest.parent.mkdir(parents=True, exist_ok=True)
    write_l5_dict(manifest, dest)
    return dest


def merge_into_staged(
    library_path: Path,
    caller: str,
    agent_id: str,
    entities: list[dict] | None,
    sessions: list[dict] | None,
    agent_type: str | None = None,
    instance: str | None = None,
    role_narrative: str | None = None,
) -> Path:
    """Merge a quarantined ``commit_to_federation`` call into its staged file.

    Repeated quarantined commits accumulate into one staged manifest per
    (caller, agent_id) rather than overwriting each other. Dedupe follows the
    live-store convention: entities by ``name.lower()``, sessions by
    ``(date, cwd)``.
    """
    dest = staging_root(library_path) / caller / f"{agent_id}.l5.yaml"
    existing: dict[str, Any] = {}
    if dest.exists():
        try:
            existing = yaml.safe_load(dest.read_text(encoding="utf-8")) or {}
        except Exception:  # noqa: BLE001 — a torn staged file is replaceable
            existing = {}

    agent_block = dict(existing.get("agent") or {})
    agent_block.setdefault("id", agent_id)
    if agent_type and not agent_block.get("type"):
        agent_block["type"] = agent_type
    if instance:
        agent_block["instance"] = instance
    if role_narrative:
        agent_block["role_narrative"] = role_narrative

    def _merge_by(rows: list, new_rows: list, key) -> list:
        merged: dict[Any, dict] = {}
        for row in rows:
            if isinstance(row, dict):
                merged[key(row)] = row
        for row in new_rows or []:
            if not isinstance(row, dict):
                continue
            k = key(row)
            if k in merged:
                merged[k].update(row)
            else:
                merged[k] = row
        return list(merged.values())

    manifest = {
        "spec_version": existing.get("spec_version") or "0.1",
        "agent": agent_block,
        "last_updated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "known_entities": _merge_by(
            list(existing.get("known_entities") or []),
            entities or [],
            lambda r: str(r.get("name") or "").lower(),
        ),
        "recent_sessions": _merge_by(
            list(existing.get("recent_sessions") or []),
            sessions or [],
            lambda r: (str(r.get("date") or ""), r.get("cwd")),
        ),
    }
    return stage_manifest(library_path, caller, manifest)


def list_staged(library_path: Path) -> list[StagedWrite]:
    root = staging_root(library_path)
    if not root.exists():
        return []
    staged: list[StagedWrite] = []
    for path in sorted(root.glob("*/*.l5.yaml")):
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except Exception:  # noqa: BLE001 — list the row anyway, with zero counts
            data = {}
        try:
            mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        except OSError:
            mtime = datetime.now(timezone.utc)
        staged.append(
            StagedWrite(
                caller=path.parent.name,
                agent_id=path.stem.replace(".l5", ""),
                path=path,
                staged_at=mtime,
                entities=len(data.get("known_entities") or []),
                sessions=len(data.get("recent_sessions") or []),
            )
        )
    return staged


def find_staged(library_path: Path, agent_id: str) -> list[StagedWrite]:
    return [s for s in list_staged(library_path) if s.agent_id == agent_id]


def promote(library_path: Path, agent_id: str) -> list[dict[str, Any]]:
    """Merge staged manifest(s) for ``agent_id`` into the live store.

    Goes through ``L6Store.commit_l5(mode="merge")`` — the same validated
    write path trusted contributions use — then deletes the staged file.
    Returns the per-file commit summaries.
    """
    from core.l6_store import L6Store

    staged = find_staged(library_path, agent_id)
    if not staged:
        raise ValueError(f"no staged writes for agent {agent_id!r}")
    store = L6Store(library_path)
    results: list[dict[str, Any]] = []
    for item in staged:
        data = yaml.safe_load(item.path.read_text(encoding="utf-8")) or {}
        agent_block = data.get("agent") or {}
        summary = store.commit_l5(
            agent_id=agent_id,
            agent_type=agent_block.get("type"),
            instance=agent_block.get("instance"),
            role_narrative=agent_block.get("role_narrative"),
            entities=list(data.get("known_entities") or []),
            sessions=list(data.get("recent_sessions") or []),
            mode="merge",
        )
        item.path.unlink()
        _prune_empty_dir(item.path.parent)
        results.append(summary)
    return results


def reject(library_path: Path, agent_id: str) -> int:
    """Delete staged manifest(s) for ``agent_id``. Returns the count removed."""
    staged = find_staged(library_path, agent_id)
    if not staged:
        raise ValueError(f"no staged writes for agent {agent_id!r}")
    for item in staged:
        item.path.unlink()
        _prune_empty_dir(item.path.parent)
    return len(staged)


def _prune_empty_dir(path: Path) -> None:
    try:
        next(path.iterdir())
    except StopIteration:
        try:
            path.rmdir()
        except OSError:
            pass
    except OSError:
        pass
