"""
Bourdon participants -- normalize native agent memory into L5 manifests.

A participant implements the BourdonParticipant Protocol defined in participants.base.
Participants are registered via Python entry points in pyproject.toml under the
`bourdon.participants` group.

First-party participants:
    - claude_code  -- Claude Code (reads claude-brain + auto-memory + MCP graph)
    - codex       -- OpenAI Codex CLI (session_index, rollouts, SQLite state)
    - cursor      -- Cursor IDE (SQLite workspace state)
    - copilot     -- GitHub Copilot (convention-based memory.md)
    - cascade     -- Cascade / Windsurf (convention-based memory.md)

Planned / native publishers:
    - clyde  -- RADLAB Clyde (native publisher, not external participant)
    - clair  -- RADLAB Clair (native publisher)
"""

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
]
