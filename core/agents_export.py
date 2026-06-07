"""Source-attributed agent export — one shared L5-manifest summarizer.

This is the single place that turns a directory of ``*.l5.yaml`` manifests into
the stable, redacted JSON shape the desktop tray consumes (schema
``bourdon.agents/v1``). Both the local CLI path (``bourdon agents``) and the
federated path (``export_agents`` MCP tool / ``L6Store.export_agents_federated``)
call into here so there is exactly one summarizer and one redaction pipeline.

Each emitted agent carries the same per-agent fields the original
``cli.main._summarize_agent_manifest`` produced (back-compat) PLUS two
source-attribution fields so a tray fed by multiple machines can render which
machine each agent came from:

- ``source``      -- the machine label this agent was exported from.
- ``source_kind`` -- ``"local"`` for this machine's own agents, ``"peer"`` for
  agents re-tagged caller-side from a federated peer.

Redaction reuses the audited credential-redaction pipeline
(``participants.codex._safe_native_memory_text``) -- the tray never sees raw
YAML regardless of session visibility. This module is a leaf: it imports
``participants.codex`` exactly as ``core.codex_turn_compiler`` already does, so
no new import cycle is introduced (``cli`` -> ``core`` -> ``participants`` stays
one-directional).
"""

from __future__ import annotations

import os
import socket
from pathlib import Path
from typing import Any

import yaml

from participants.codex import _safe_native_memory_text

AGENTS_SCHEMA = "bourdon.agents/v1"
MAX_RECENT_SESSIONS = 10


def resolve_local_name() -> str:
    """Resolve this machine's label for source attribution.

    Honors ``BOURDON_LOCAL_NAME`` (so a deployment can pin a stable, friendly
    machine label), else falls back to ``socket.gethostname()``. Computed at
    call time so tests can monkeypatch either source.
    """
    env = os.environ.get("BOURDON_LOCAL_NAME")
    if env and env.strip():
        return env.strip()
    return socket.gethostname()


def _redact_field(value: Any) -> Any:
    """Run a single emitted string field through the canonical redaction.

    Reuses ``participants.codex._safe_native_memory_text`` -- the audited
    credential-redaction + URL-strip + length-cap pipeline -- so the tray never
    sees raw secrets regardless of session visibility. Non-strings pass
    through untouched.
    """
    if isinstance(value, str):
        return _safe_native_memory_text(value)
    return value


def _redact_str_list(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []
    return [_safe_native_memory_text(str(item)) for item in values]


def summarize_agent_manifest(
    manifest: dict[str, Any],
    *,
    source: str,
    source_kind: str = "local",
) -> dict[str, Any]:
    """Build one redacted, source-attributed summary from a parsed L5 manifest.

    Output is the canonical per-agent shape consumed by the tray plus the two
    source-attribution fields. ``source`` / ``source_kind`` are stamped by the
    caller, never read from the manifest -- the export tags trust the machine
    doing the summarizing, not the agent's self-report.
    """
    agent = manifest.get("agent") or {}
    sessions = manifest.get("recent_sessions") or []
    if not isinstance(sessions, list):
        sessions = []

    def _session_date(session: Any) -> str:
        if isinstance(session, dict):
            return str(session.get("date") or "")
        return ""

    sorted_sessions = sorted(sessions, key=_session_date, reverse=True)
    recent_activity = [
        {
            "date": _session_date(session),
            "project_focus": _redact_str_list(
                session.get("project_focus") if isinstance(session, dict) else None
            ),
            "key_actions": _redact_str_list(
                session.get("key_actions") if isinstance(session, dict) else None
            ),
            "visibility": (
                str(session.get("visibility") or "team")
                if isinstance(session, dict)
                else "team"
            ),
        }
        for session in sorted_sessions[:MAX_RECENT_SESSIONS]
    ]
    freshest = _session_date(sorted_sessions[0]) if sorted_sessions else None

    capabilities = manifest.get("capabilities") or []

    return {
        "id": _redact_field(str(agent.get("id") or "")),
        "type": _redact_field(str(agent.get("type") or "")) or None,
        "instance": _redact_field(str(agent.get("instance") or "")) or None,
        "role_narrative": (
            _redact_field(str(agent.get("role_narrative")))
            if agent.get("role_narrative")
            else None
        ),
        "last_updated": manifest.get("last_updated"),
        "capability_count": (
            len(capabilities) if isinstance(capabilities, list) else 0
        ),
        "session_count": len(sessions),
        "freshest_session_date": freshest or None,
        "recent_activity": recent_activity,
        "parse_error": None,
        "source": source,
        "source_kind": source_kind,
    }


def error_agent_entry(
    agent_id: str,
    message: str,
    *,
    source: str,
    source_kind: str = "local",
) -> dict[str, Any]:
    """Partial-failure entry so the tray can represent a broken manifest.

    Carries the same source-attribution fields as a healthy entry so a broken
    manifest is still attributed to the machine it came from.
    """
    return {
        "id": agent_id,
        "type": None,
        "instance": None,
        "role_narrative": None,
        "last_updated": None,
        "capability_count": None,
        "session_count": None,
        "freshest_session_date": None,
        "recent_activity": [],
        "parse_error": message,
        "source": source,
        "source_kind": source_kind,
    }


def export_local_agents(agents_dir: Path, local_name: str) -> dict[str, Any]:
    """Summarize every local ``*.l5.yaml`` manifest into the tray envelope.

    Parameters
    ----------
    agents_dir : Path
        Directory holding ``*.l5.yaml`` manifests (``~/agent-library/agents``).
        Must already exist and be readable -- callers are responsible for the
        missing-dir exit-code decision (the CLI exits nonzero, the server
        treats a missing dir as "no agents").
    local_name : str
        Machine label stamped on every emitted agent as ``source`` (with
        ``source_kind="local"``).

    Returns
    -------
    dict
        ``{"schema": ..., "machine": local_name, "generated_from": str(dir),
        "agents": [...]}``. Agents are sorted by ``last_updated`` descending.
        Per-manifest parse failures are represented inline (``parse_error``)
        rather than raised, so one broken file never sinks the whole export.
    """
    agents: list[dict[str, Any]] = []
    if agents_dir.is_dir():
        try:
            manifest_paths = sorted(
                p for p in agents_dir.glob("*.l5.yaml") if p.is_file()
            )
        except OSError:
            manifest_paths = []
        for path in manifest_paths:
            stem = path.name[: -len(".l5.yaml")]
            try:
                loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
            except (yaml.YAMLError, OSError, UnicodeDecodeError) as exc:
                agents.append(
                    error_agent_entry(stem, str(exc), source=local_name)
                )
                continue
            if not isinstance(loaded, dict):
                agents.append(
                    error_agent_entry(
                        stem, "manifest is not a YAML mapping", source=local_name
                    )
                )
                continue
            try:
                agents.append(
                    summarize_agent_manifest(loaded, source=local_name)
                )
            except Exception as exc:  # noqa: BLE001 -- partial failure must be representable
                agents.append(
                    error_agent_entry(stem, str(exc), source=local_name)
                )

    agents.sort(key=lambda a: (a.get("last_updated") or ""), reverse=True)

    return {
        "schema": AGENTS_SCHEMA,
        "machine": local_name,
        "generated_from": str(agents_dir),
        "agents": agents,
    }
