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


_VISIBILITY_RANK = {"public": 0, "team": 1, "private": 2}


def _session_visible(session: Any, access_level: str) -> bool:
    """True if a session at its declared visibility is allowed at ``access_level``.

    Unmarked sessions default to ``team`` (matching the label this function
    stamps), so an unmarked session never escapes to a ``public``/untrusted
    caller. ``access_level="private"`` (the local default) admits everything.
    """
    vis = "team"
    if isinstance(session, dict):
        declared = str(session.get("visibility") or "team").strip().lower()
        if declared in _VISIBILITY_RANK:
            vis = declared
    return _VISIBILITY_RANK[vis] <= _VISIBILITY_RANK.get(access_level, 2)


def summarize_agent_manifest(
    manifest: dict[str, Any],
    *,
    source: str,
    source_kind: str = "local",
    access_level: str = "private",
) -> dict[str, Any]:
    """Build one redacted, source-attributed summary from a parsed L5 manifest.

    ``access_level`` gates which sessions are emitted (3-Star audit P0-1): the
    peer-facing export path passes ``team``/``public`` so PRIVATE session
    content never crosses the federation wire. The default ``private`` admits
    everything for the operator's own local tray view.

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
    # Egress visibility gate: drop sessions above the caller's access level so
    # PRIVATE/TEAM content cannot leak to a peer (3-Star audit P0-1). Default
    # access_level="private" admits everything for the local view.
    visible_sessions = [s for s in sorted_sessions if _session_visible(s, access_level)]
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
        for session in visible_sessions[:MAX_RECENT_SESSIONS]
    ]
    freshest = _session_date(visible_sessions[0]) if visible_sessions else None

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
        "session_count": len(visible_sessions),
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


def export_local_agents(
    agents_dir: Path, local_name: str, access_level: str = "private"
) -> dict[str, Any]:
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
                    summarize_agent_manifest(
                        loaded, source=local_name, access_level=access_level
                    )
                )
            except Exception as exc:  # noqa: BLE001 -- partial failure must be representable
                agents.append(
                    error_agent_entry(stem, str(exc), source=local_name)
                )

    _join_live_presence(agents, access_level, source=local_name)

    # Live-now outranks stale recency: without the live_count key, an agent
    # that is live RIGHT NOW but has no manifest yet (synthesized row,
    # last_updated None) would sort beneath months-stale entries.
    agents.sort(
        key=lambda a: ((a.get("live_count") or 0) > 0, a.get("last_updated") or ""),
        reverse=True,
    )

    return {
        "schema": AGENTS_SCHEMA,
        "machine": local_name,
        "generated_from": str(agents_dir),
        "agents": agents,
    }


def _join_live_presence(
    agents: list[dict[str, Any]], access_level: str = "private", *, source: str = ""
) -> None:
    """Enrich each agent row in place with live-session presence.

    Adds ``live_sessions`` (a possibly-empty list of {instance, host, project,
    started_at, age_s}) and ``live_count`` to every agent, read from
    ``~/.bourdon/presence`` via :mod:`core.presence`. Presence is ephemeral and
    orthogonal to the durable L5 manifests. Best-effort: a read failure never
    sinks the export — every agent just gets ``live_count=0``.

    Egress gate (parity with the per-session access gate): presence reveals what
    you are actively working on *right now* (project, host). It is emitted for
    the local tray (``private``) and for TRUSTED team peers (``team``) — the
    latter is what makes the federated cross-machine live-session view work — but
    NEVER for a ``public``/untrusted caller, who gets ``live_count=0``.

    An agent with live sessions but NO L5 manifest yet (registered before its
    first export) gets a minimal synthesized row — a running session must never
    be invisible in the tray. Synthesized rows carry only the id + live fields
    (``session_count`` 0, no durable data to show).
    """
    if access_level == "public":
        for agent in agents:
            agent["live_sessions"] = []
            agent["live_count"] = 0
        return

    try:
        from core import presence

        by_agent = presence.live_sessions_by_agent()
    except Exception:  # noqa: BLE001 -- presence is best-effort, never fatal
        by_agent = {}

    def _attach(agent: dict[str, Any], sessions: list[dict[str, Any]]) -> None:
        for session in sessions:
            # Route presence strings through the same audited redaction pipeline
            # every other exported field uses (project = cwd basename, host =
            # hostname) — don't let presence bypass it on the way to a peer.
            if session.get("project"):
                session["project"] = _redact_field(str(session["project"]))
            if session.get("host"):
                session["host"] = _redact_field(str(session["host"]))
        agent["live_sessions"] = sessions
        agent["live_count"] = len(sessions)

    seen: set[str] = set()
    for agent in agents:
        agent_id = str(agent.get("id") or "")
        seen.add(agent_id)
        _attach(agent, by_agent.get(agent_id, []))

    for agent_id, sessions in by_agent.items():
        if agent_id in seen:
            continue
        # The id originates from a presence file (hook-supplied), so it goes
        # through the same redaction pipeline as every other exported string —
        # manifest rows get this in summarize_agent_manifest.
        row = error_agent_entry(_redact_field(agent_id), "", source=source)
        row["parse_error"] = None  # not an error — just no manifest yet
        row["session_count"] = 0
        _attach(row, sessions)
        agents.append(row)
