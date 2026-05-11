"""
Bourdon adapters -- normalize native agent memory into L5 manifests.

An adapter implements the BourdonAdapter Protocol defined in adapters.base.
Adapters are registered via Python entry points in pyproject.toml under the
`bourdon.adapters` group.

First-party adapters:
    - claude_code  -- Claude Code (reads claude-brain + auto-memory + MCP graph)
    - codex       -- OpenAI Codex CLI (session_index, rollouts, SQLite state)
    - cursor      -- Cursor IDE (SQLite workspace state)
    - copilot     -- GitHub Copilot (convention-based memory.md)
    - cascade     -- Cascade / Windsurf (convention-based memory.md)

Planned / native publishers:
    - clyde  -- RADLAB Clyde (native publisher, not external adapter)
    - clair  -- RADLAB Clair (native publisher)
"""

from adapters.base import (
    AdapterDiscoveryError,
    AdapterError,
    AdapterExportError,
    AdapterVersionMismatchError,
    AgentInfo,
    AgentStore,
    BourdonAdapter,
    Entity,
    HealthStatus,
    L5Manifest,
    Session,
    Visibility,
    VisibilityPolicy,
)

__all__ = [
    "AdapterDiscoveryError",
    "AdapterError",
    "AdapterExportError",
    "AdapterVersionMismatchError",
    "AgentInfo",
    "AgentStore",
    "BourdonAdapter",
    "Entity",
    "HealthStatus",
    "L5Manifest",
    "Session",
    "Visibility",
    "VisibilityPolicy",
]
