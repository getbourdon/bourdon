"""
Bourdon participants -- normalize native agent memory into L5 manifests.

A participant implements the BourdonParticipant Protocol defined in participants.base.
Participants are registered via Python entry points in pyproject.toml under the
`bourdon.participants` group.

First-party participants:
    - claude_code            -- Claude Code (reads claude-brain + auto-memory + MCP graph)
    - claude_code_automations -- Claude Code automation runs (reads automation.toml + memory.md)
    - claude_desktop_code    -- Claude Desktop GUI Code surface (metadata-only state)
    - claude_desktop_cowork  -- Claude Desktop Co-Work surface
    - codex                  -- OpenAI Codex CLI (session_index, rollouts, SQLite state)
    - codex_automations      -- Codex automation runs (reads automation.toml + memory.md)
    - cursor                 -- Cursor IDE (SQLite workspace state)
    - copilot                -- GitHub Copilot (convention-based memory.md — fallback surface)
    - copilot_cli            -- GitHub Copilot CLI (session-store.db SQLite)
    - copilot_vscode         -- GitHub Copilot VS Code GUI (transcripts + memory-tool)
    - copilot_automations    -- GitHub Copilot automations (convention-based memory)
    - cascade                -- Cascade / Windsurf (convention-based memory.md)

Planned / native publishers:
    - clyde  -- RADLAB Clyde (native publisher, not external participant)
    - clair  -- RADLAB Clair (native publisher)
"""

from __future__ import annotations

import importlib
import inspect
import logging
import pkgutil

from participants.base import (
    ParticipantDiscoveryError,
    ParticipantError,
    ParticipantExportError,
    ParticipantVersionMismatchError,
    AgentInfo,
    AgentStore,
    BourdonParticipant,
    Entity,
    HealthStatus,
    L5Manifest,
    Session,
    Visibility,
    VisibilityPolicy,
)

logger = logging.getLogger(__name__)

# Attributes that mark a class as a Bourdon participant. A class in a
# participants/ submodule that has all of these is auto-registered.
_PARTICIPANT_MARKER_ATTRS = ("agent_id", "agent_type", "export_l5", "health_check")


def discover_participants() -> list[tuple[str, type]]:
    """All registered participants as (agent_id, class), sorted by agent_id.

    Single source of truth: a scan of the participants/ package (entry-point
    metadata is empty when bourdon runs from source). Add a participant = drop a
    module whose class has agent_id/agent_type/export_l5/health_check -- no list
    edits.

    Modules whose name starts with ``_`` or equals ``base`` are skipped (private
    helpers and the Protocol module). A module that fails to import is logged at
    WARNING and skipped rather than aborting discovery, so one broken participant
    never takes the whole CLI down.
    """
    found: dict[str, type] = {}
    for module_info in pkgutil.iter_modules(__path__):
        name = module_info.name
        if name.startswith("_") or name == "base":
            continue
        try:
            module = importlib.import_module(f"{__name__}.{name}")
        except Exception as exc:  # noqa: BLE001 -- never let one bad module break discovery
            logger.warning("Skipping participant module %r: import failed: %s", name, exc)
            continue
        for _, obj in inspect.getmembers(module, inspect.isclass):
            # Only classes *defined* in this module, not ones it imported.
            if obj.__module__ != module.__name__:
                continue
            if all(hasattr(obj, attr) for attr in _PARTICIPANT_MARKER_ATTRS):
                agent_id = obj.agent_id
                found.setdefault(agent_id, obj)
    return sorted(found.items(), key=lambda item: item[0])


__all__ = [
    "ParticipantDiscoveryError",
    "ParticipantError",
    "ParticipantExportError",
    "ParticipantVersionMismatchError",
    "AgentInfo",
    "AgentStore",
    "BourdonParticipant",
    "Entity",
    "HealthStatus",
    "L5Manifest",
    "Session",
    "Visibility",
    "VisibilityPolicy",
    "discover_participants",
]
