"""
Bourdon L6 -- MCP server.

Wraps :class:`core.l6_store.L6Store` in a fastmcp server so any MCP-aware
agent (Claude Code, Codex, Cursor, Copilot-next-gen) can query the
federation natively without framework-specific integration.

Requires the ``[server]`` optional extra::

    pip install 'bourdon[server]'

Launch::

    python -m core.l6_server
    # or with a custom library path:
    python -m core.l6_server --library /path/to/agent-library --port 7500

Resources exposed
-----------------
- ``agent-library://agents``
  List of agent IDs known to the store.
- ``agent-library://agents/{id}/memory``
  Full (visibility-filtered) L5 manifest for one agent.
- ``agent-library://entities/{name}``
  Cross-agent view of one entity (who knows about it + each agent's
  summary).

Tools exposed
-------------
- ``query_agent_memory(agent, topic)``
  Cross-agent find for a topic restricted to one agent's manifest.
- ``list_recent_work(since, agent)``
  Sessions across agents (or one) since a given ISO-8601 date.
- ``find_entity(name, access_level, include_private)``
  Cross-agent entity lookup by name. ``access_level`` defaults to
  ``public``. ``include_private`` remains as a compatibility shim.
- ``get_cross_agent_summary(project, access_level, include_private)``
  Roll-up: all agents + sessions + entities relating to one project.
- ``prepare_recognition_context(prompt, access_level, include_private)``
  Immediate recognition and a bounded prompt-context fragment for turn start.
- ``get_deeper_context(prompt, access_level, include_private)``
  Post-recognition L2 context retrieval. Returns empty context when disabled.
- ``commit_to_federation(agent_id, agent_type, entities, sessions, mode, ...)``
  Write-side tool. Cloud-only / webview-wrapper agents (Claude Desktop,
  ChatGPT desktop, etc.) call this to push L5 contributions when they
  have no readable on-disk store for a Bourdon participant to scrape.
- ``export_agents()``
  Source-attributed export of THIS machine's local agents only (LOCAL-ONLY:
  no peer fan-out, to avoid the bidirectional-federation echo). The desktop
  tray's federated read merges this with each peer's ``export_agents``.
"""

from __future__ import annotations

import argparse
import hmac
import ipaddress
import logging
import os
import re
import time as time_module
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from core.codex_turn_compiler import compile_codex_turn as _compile_codex_turn
from core.federation_audit import DECISION_ALLOW, DECISION_DENY, FederationAudit
from core.federation_registry import (
    OPERATOR,
    TIER_QUARANTINED,
    AgentIdentity,
    FederationRegistry,
    get_caller,
    reset_caller,
    set_caller,
)
from core.l2 import query_l2
from core.l6_store import DEFAULT_LIBRARY_PATH, L6Store
from core.recognition_runtime import recognition_first

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from core.l6_remote import RemoteL6Client

_CONTEXT_SENSITIVE_PATTERNS = (
    re.compile(r"\bapi[_-]?key\b", re.IGNORECASE),
    re.compile(r"\bapi[_-]?token\b", re.IGNORECASE),
    re.compile(r"\baccess[_-]?token\b", re.IGNORECASE),
    re.compile(r"\bpassword\b", re.IGNORECASE),
)


def _require_fastmcp():
    """Import fastmcp lazily so importing this module doesn't require it."""
    try:
        from fastmcp import FastMCP  # type: ignore[import-not-found]
    except ImportError as exc:
        raise ImportError(
            "fastmcp is required to run the L6 server. "
            "Install with: pip install 'bourdon[server]'"
        ) from exc
    return FastMCP


# -- Server construction -------------------------------------------------------


def _safe_context_text(value: str, limit: int = 240) -> str:
    text = re.sub(r"\s+", " ", value.strip())
    if any(pattern.search(text) for pattern in _CONTEXT_SENSITIVE_PATTERNS):
        return "[redacted credential-like text]"
    text = re.sub(r"https?://\S+", "[link]", text)
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def _recognition_prompt_context(result: Any) -> str:
    if not result.recognition:
        return ""

    lines = [
        "Bourdon recognition context",
        f"Immediate recognition: {_safe_context_text(result.recognition)}",
    ]
    if result.matched_entities:
        lines.append("Matched entities:")
    for entity in result.matched_entities:
        name = _safe_context_text(str(entity.get("name") or ""))
        entity_type = _safe_context_text(str(entity.get("type") or "topic"))
        summary = str(entity.get("summary") or "").strip()
        source_agents = [
            str(agent)
            for agent in entity.get("source_agents", [])
            if isinstance(agent, str) and agent
        ]
        line = f"- {name} ({entity_type})"
        if source_agents:
            line += f" via {', '.join(source_agents)}"
        if summary:
            line += f": {_safe_context_text(summary)}"
        lines.append(line)
    lines.append("Use this as timing-layer context, not as a final answer.")
    return "\n".join(lines)


def prepare_recognition_context_from_store(
    store: L6Store,
    prompt: str,
    access_level: str = "team",
    include_private: bool = False,
) -> dict[str, Any]:
    manifest = store.build_recognition_manifest(
        include_private=include_private,
        access_level=access_level,
    )
    t0 = time_module.perf_counter()
    result = recognition_first(
        prompt,
        manifest,
        access_level=access_level,
    )
    latency_us = (time_module.perf_counter() - t0) * 1_000_000
    hydration = result.hydration
    hydration_scheduled = hydration is not None
    if hydration is not None:
        hydration.close()

    return {
        "prompt": prompt,
        "access_level": access_level,
        "include_private": include_private,
        "recognition": result.recognition,
        "matched_entities": [
            {
                "name": str(entity.get("name") or ""),
                "type": str(entity.get("type") or "topic"),
                "source_agents": list(entity.get("source_agents") or []),
            }
            for entity in result.matched_entities
        ],
        "recognition_latency_us": round(latency_us, 1),
        "hydration_scheduled": hydration_scheduled,
        "prompt_context": _recognition_prompt_context(result),
    }


async def prepare_recognition_context_federated(
    store: L6Store,
    prompt: str,
    access_level: str = "team",
    include_private: bool = False,
    timeout_per_peer: float | None = None,
) -> dict[str, Any]:
    """Phase 1.7 — federated recognition with bounded per-peer latency.

    Behavior:
    1. Run local recognition first (sync, ~1.2 ms). This always produces a
       valid response — peers can only *augment*, never block.
    2. Fan out to every peer in parallel via ``asyncio.wait_for``, each
       capped by ``timeout_per_peer`` (default: per-peer ``recognition_timeout``,
       typically 200 ms).
    3. Merge peer-returned ``matched_entities`` into the local list, tagging
       peer-sourced agents as ``peer:<peer-name>:<agent>``. Dedupe by
       ``name.lower()``; on dedupe, peer source_agents are appended to the
       local entity.
    4. Append a one-line summary of each responding peer to the
       ``prompt_context`` so the caller can show provenance.
    5. Return the extended payload with per-peer latency breakdown.

    Slow peers (over timeout) and failed peers are logged + their latency
    reported as ``None``. They never propagate exceptions.
    """
    import asyncio

    # 1. Local first — guaranteed answer.
    local = prepare_recognition_context_from_store(
        store, prompt, access_level=access_level, include_private=include_private
    )
    if not store.peers:
        # Backward-compatible: same shape, with empty peer metadata.
        local["peer_latencies_us"] = {}
        local["peers_queried"] = 0
        local["peers_responded"] = 0
        local["peers_timed_out"] = 0
        return local

    async def _one_peer(peer) -> tuple[str, dict | None, float | None, str | None]:
        budget = timeout_per_peer if timeout_per_peer is not None else peer.recognition_timeout
        p_start = time_module.perf_counter()
        try:
            payload = await asyncio.wait_for(
                peer.prepare_recognition_context(
                    prompt,
                    access_level=access_level,
                    include_private=include_private,
                ),
                timeout=budget,
            )
        except asyncio.TimeoutError:
            return peer.name, None, None, "timeout"
        except Exception as exc:  # noqa: BLE001 — never raise from a peer call
            logger.warning("peer %s prepare_recognition_context raised: %s", peer.name, exc)
            return peer.name, None, None, f"error:{exc}"
        latency_us = (time_module.perf_counter() - p_start) * 1_000_000
        return peer.name, payload, round(latency_us, 1), None

    results = await asyncio.gather(*(_one_peer(p) for p in store.peers))

    peer_latencies: dict[str, float | None] = {}
    matched_by_key: dict[str, dict] = {
        e["name"].lower(): e for e in local["matched_entities"] if e.get("name")
    }
    extra_context_lines: list[str] = []
    peers_responded = 0
    peers_timed_out = 0

    for peer_name, payload, latency_us, err in results:
        peer_latencies[peer_name] = latency_us
        if err == "timeout":
            peers_timed_out += 1
            continue
        if payload is None:
            continue
        peers_responded += 1
        # Merge peer-matched entities, tagging the agents with peer provenance.
        for ent in payload.get("matched_entities") or []:
            if not isinstance(ent, dict):
                continue
            ent_name = (ent.get("name") or "").strip()
            if not ent_name:
                continue
            tagged_agents = [
                f"peer:{peer_name}:{a}"
                for a in ent.get("source_agents") or []
                if isinstance(a, str)
            ]
            key = ent_name.lower()
            existing = matched_by_key.get(key)
            if existing is None:
                matched_by_key[key] = {
                    "name": ent_name,
                    "type": str(ent.get("type") or "topic"),
                    "source_agents": tagged_agents,
                }
            else:
                for a in tagged_agents:
                    if a not in existing["source_agents"]:
                        existing["source_agents"].append(a)
        # Append a short peer-recognition line to prompt_context.
        peer_recognition = (payload.get("recognition") or "").strip()
        if peer_recognition:
            extra_context_lines.append(f"[peer:{peer_name}] {peer_recognition}")

    # Rebuild the response. Replace matched_entities with the merged set, and
    # extend prompt_context with the peer-tagged lines.
    local["matched_entities"] = list(matched_by_key.values())
    if extra_context_lines:
        existing_ctx = local.get("prompt_context") or ""
        local["prompt_context"] = existing_ctx.rstrip() + "\n" + "\n".join(extra_context_lines)
    local["peer_latencies_us"] = peer_latencies
    local["peers_queried"] = len(store.peers)
    local["peers_responded"] = peers_responded
    local["peers_timed_out"] = peers_timed_out
    return local


async def get_deeper_context_for_prompt(
    prompt: str,
    access_level: str = "team",
    include_private: bool = False,
) -> dict[str, Any]:
    try:
        context = await query_l2(prompt)
    except Exception as exc:  # noqa: BLE001 -- deeper context must not crash a turn
        logger.warning("L2 deeper context failed: %s", exc)
        context = ""
    return {
        "prompt": prompt,
        "access_level": access_level,
        "include_private": include_private,
        "context": context,
        "context_chars": len(context),
    }


def compile_codex_turn_from_store(
    store: L6Store,
    prompt: str,
    cwd: str | None = None,
    access_level: str = "team",
    max_items: int = 6,
    max_chars: int = 1800,
) -> dict[str, Any]:
    """Return a Codex turn-scoped recognition brief using this server's store."""
    brief = _compile_codex_turn(
        prompt,
        cwd=cwd,
        library_path=store.library_path,
        access_level=access_level,
        max_items=max_items,
        max_chars=max_chars,
        delivery="all",
    )
    return brief.to_dict()


def create_l6_server(
    store: L6Store,
    name: str = "bourdon-l6",
    registry: FederationRegistry | None = None,
    audit: FederationAudit | None = None,
) -> Any:
    """
    Build a FastMCP server exposing L6 resources + tools over the given store.

    Parameters
    ----------
    store : L6Store
        The federation store to serve from.
    name : str
        Server name (used in MCP handshakes).

    Returns
    -------
    FastMCP
        A configured FastMCP instance. Caller may start it via
        ``mcp.run()`` (stdio), ``await mcp.run_async()``, or by passing
        it to an ASGI server for HTTP transport.
    """
    fastmcp_cls = _require_fastmcp()
    mcp = fastmcp_cls(name)

    if registry is None:
        registry = FederationRegistry()
    if audit is None:
        audit = FederationAudit()

    # ---- Trust-tier enforcement (v0.9.0) ---------------------------------------
    #
    # Caller identity arrives via the contextvar set by the HTTP auth
    # middleware (stateless HTTP runs tool handlers inside the request task,
    # so propagation is deterministic), with a request.state fallback for any
    # transport mode where the contextvar didn't carry. stdio has neither and
    # resolves to OPERATOR (trusted) — exactly v0.8.0 behavior.
    #
    # Quarantined callers get an allowlisted read surface filtered to their
    # granted namespaces (spec/SPEC_v0.9.0.md D4) and staged writes (D5).
    # Every call is audited, allow and deny alike (D7).

    def _resolve_caller() -> AgentIdentity:
        ident = get_caller()
        if ident is not OPERATOR:
            return ident
        try:
            from fastmcp.server.dependencies import get_http_request

            request = get_http_request()
        except Exception:  # noqa: BLE001 — no HTTP context => stdio => operator
            return ident
        if request is None:
            return ident
        state_ident = getattr(getattr(request, "state", None), "bourdon_identity", None)
        if isinstance(state_ident, AgentIdentity):
            return state_ident
        # An HTTP request that never passed our identity middleware: fail
        # CLOSED — treat as an unknown quarantined caller with zero grants.
        return AgentIdentity(agent_id="unknown", tier=TIER_QUARANTINED)

    def _audit(caller: AgentIdentity, op: str, namespace: str = "*",
               decision: str = DECISION_ALLOW, detail: str | None = None) -> None:
        audit.record(caller.agent_id, op, namespace, decision, detail)

    def _denied(op: str, caller: AgentIdentity, namespace: str = "*",
                detail: str = "tier 'quarantined' may not call this tool") -> dict:
        _audit(caller, op, namespace, DECISION_DENY, detail)
        return {
            "error": "access denied",
            "op": op,
            "agent": caller.agent_id,
            "tier": caller.tier,
            "detail": detail,
        }

    def _filter_entity_matches(matches: list, caller: AgentIdentity) -> list:
        """Drop non-granted agents from EntityMatch rows; drop empty rows."""
        if caller.is_trusted:
            return matches
        kept = []
        for m in matches:
            agents = [a for a in m.agents if caller.may_read(a)]
            if not agents:
                continue
            m.agents = agents
            m.summaries = {a: s for a, s in m.summaries.items() if caller.may_read(a)}
            kept.append(m)
        return kept

    # ---- Resources ------------------------------------------------------------

    @mcp.resource("agent-library://agents")
    def list_agents_resource() -> list[str]:
        """List of all agent IDs known to the federation."""
        caller = _resolve_caller()
        agents = store.list_agents()
        if not caller.is_trusted:
            agents = [a for a in agents if caller.may_read(a)]
        _audit(caller, "resource:agents")
        return agents

    @mcp.resource("agent-library://agents/{agent_id}/memory")
    def get_agent_memory_resource(agent_id: str) -> dict:
        """
        Full visibility-filtered L5 manifest for one agent.

        Returns an empty dict with an ``error`` key when the agent is
        unknown (MCP resources can't signal 404 cleanly, so we surface
        it in the payload).
        """
        caller = _resolve_caller()
        if not caller.may_read(agent_id):
            return _denied("resource:agent-memory", caller, namespace=agent_id,
                           detail=f"namespace {agent_id!r} not granted")
        _audit(caller, "resource:agent-memory", agent_id)
        manifest = store.get_agent_manifest(agent_id, include_private=False)
        if manifest is None:
            return {"error": f"agent not found: {agent_id}"}
        return manifest

    @mcp.resource("agent-library://entities/{name}")
    def get_entity_resource(name: str) -> list[dict]:
        """Cross-agent view of one entity by name."""
        caller = _resolve_caller()
        matches = store.find_entity(name, include_private=False, access_level="public")
        matches = _filter_entity_matches(matches, caller)
        _audit(caller, "resource:entity")
        return [m.to_dict() for m in matches]

    # ---- Tools ---------------------------------------------------------------

    @mcp.tool()
    def query_agent_memory(
        agent: str,
        topic: str,
        access_level: str = "public",
        include_private: bool = False,
    ) -> dict:
        """
        Find entries in one agent's L5 that match a topic.

        Parameters
        ----------
        agent : str
            Agent ID (e.g. "claude-code", "codex", "clyde").
        topic : str
            The entity name or topic to look for. Case-insensitive.

        Returns
        -------
        dict
            ``{"agent": str, "matches": list[EntityMatch-as-dict]}``
        """
        caller = _resolve_caller()
        if not caller.may_read(agent):
            return _denied("query_agent_memory", caller, namespace=agent,
                           detail=f"namespace {agent!r} not granted")
        _audit(caller, "query_agent_memory", agent)
        matches = [
            m
            for m in store.find_entity(
                topic,
                include_private=include_private,
                access_level=access_level,
            )
            if agent in m.agents
        ]
        return {
            "agent": agent,
            "topic": topic,
            "access_level": access_level,
            "include_private": include_private,
            "matches": [m.to_dict() for m in matches],
        }

    @mcp.tool()
    async def list_recent_work(
        since: str | None = None,
        agent: str | None = None,
        access_level: str = "public",
        include_private: bool = False,
        limit: int | None = None,
        cursor: str | None = None,
        summary: bool = False,
    ) -> dict:
        """
        Return a page of sessions across agents (or a single agent).

        Parameters
        ----------
        since : str, optional
            ISO 8601 date (``YYYY-MM-DD``) or datetime. When omitted AND
            ``cursor`` is omitted, the store applies a 14-day default
            window so the first call from a naive caller doesn't pull
            the entire history.
        agent : str, optional
            Filter to one agent's sessions.
        limit : int, optional
            Page size. Defaults to 20, capped at 100.
        cursor : str, optional
            Opaque token from a previous response's ``next_cursor``.
            Pagination loop: call once, then keep passing the most recent
            ``next_cursor`` until ``has_more`` is false. Re-pass any
            ``since`` / ``agent`` filters on each page.
        summary : bool, optional
            When true, omit ``key_actions`` and ``files_touched`` from
            each session row. Useful for timeline/dashboard callers that
            only need date + agent + project focus.
        """
        caller = _resolve_caller()
        if not caller.is_trusted and agent is not None and not caller.may_read(agent):
            denial = _denied("list_recent_work", caller, namespace=agent,
                             detail=f"namespace {agent!r} not granted")
            denial.update({"sessions": [], "next_cursor": None, "has_more": False})
            return denial
        _audit(caller, "list_recent_work", agent or "*")
        cutoff: datetime | None = None
        if since:
            try:
                # Accept both date and datetime ISO strings
                cutoff = datetime.fromisoformat(since)
            except ValueError:
                # Fall back to date-only parse
                try:
                    from datetime import date as _date
                    from datetime import time as _time

                    parsed = _date.fromisoformat(since)
                    cutoff = datetime.combine(parsed, _time.min)
                except ValueError:
                    logger.warning("Invalid 'since' value: %s", since)
        try:
            if store.peers and not cursor:
                # Federated path: merge local + peer sessions. Cursoring across
                # peers is not supported in v0; a non-None cursor falls back to
                # local-only paging (where the cursor encoding is valid).
                page = await store.list_recent_work_federated(
                    since=cutoff,
                    agent=agent,
                    include_private=include_private,
                    access_level=access_level,
                    limit=limit,
                    cursor=cursor,
                )
            else:
                page = store.list_recent_work(
                    since=cutoff,
                    agent=agent,
                    include_private=include_private,
                    access_level=access_level,
                    limit=limit,
                    cursor=cursor,
                )
        except ValueError as exc:
            # Bad cursor token -- surface to the caller rather than silently
            # treating it as a fresh first page.
            return {
                "error": str(exc),
                "since": since,
                "agent": agent,
                "access_level": access_level,
                "include_private": include_private,
                "limit": limit,
                "cursor": cursor,
                "summary": summary,
                "sessions": [],
                "next_cursor": None,
                "has_more": False,
            }
        rows = page.sessions
        if not caller.is_trusted:
            rows = [s for s in rows if caller.may_read(s.agent)]
        return {
            "since": since,
            "agent": agent,
            "access_level": access_level,
            "include_private": include_private,
            "limit": limit,
            "cursor": cursor,
            "summary": summary,
            "sessions": [s.to_dict(summary=summary) for s in rows],
            "next_cursor": page.next_cursor,
            "has_more": page.has_more,
        }

    @mcp.tool()
    async def find_entity(
        name: str,
        access_level: str = "public",
        include_private: bool = False,
    ) -> dict:
        """
        Find an entity by name across all agents.

        ``include_private`` defaults to False. Callers that genuinely need
        unredacted output must pass ``True`` explicitly -- this is a second
        line of defense on top of per-manifest visibility policy.

        When the server has peer L6 servers configured (``--peer`` flag),
        the result also merges matches from each peer's library, tagging
        peer-sourced agents as ``peer:<peer-name>:<agent>``.
        """
        caller = _resolve_caller()
        _audit(caller, "find_entity")
        matches = await store.find_entity_federated(
            name,
            include_private=include_private,
            access_level=access_level,
        )
        matches = _filter_entity_matches(matches, caller)
        return {
            "name": name,
            "access_level": access_level,
            "include_private": include_private,
            "matches": [m.to_dict() for m in matches],
        }

    @mcp.tool()
    async def list_agents() -> dict:
        """
        List agent IDs known to this L6 server, plus any peers' agents.

        Peer-sourced agents are NOT prefix-tagged here — call sites that need
        provenance use the more detailed ``find_entity`` / ``get_cross_agent_summary``
        tools where each agent is tagged ``peer:<peer-name>:<agent>``.
        """
        caller = _resolve_caller()
        agents = await store.list_agents_federated()
        if not caller.is_trusted:
            agents = [a for a in agents if caller.may_read(a)]
        _audit(caller, "list_agents")
        return {"agents": agents}

    @mcp.tool()
    def export_agents() -> dict:
        """Export THIS server's LOCAL agents only, source-attributed for the tray.

        Returns the ``bourdon.agents/v1`` envelope for this machine's own
        ``*.l5.yaml`` manifests -- each agent redacted and tagged
        ``source=<this machine>`` / ``source_kind="local"``.

        Critically, this tool does NOT fan out to peers. The federated merge
        (local + every peer's ``export_agents``) happens caller-side in
        :meth:`core.l6_store.L6Store.export_agents_federated`, which re-tags
        each peer's agents with that peer's name. Keeping this tool local-only
        is what prevents the bidirectional-federation echo: when peer A calls
        peer B's ``export_agents``, B returns only B's agents, never A's agents
        bounced back.
        """
        from core.agents_export import export_local_agents, resolve_local_name

        caller = _resolve_caller()
        envelope = export_local_agents(
            store.library_path / "agents", resolve_local_name()
        )
        if not caller.is_trusted:
            envelope["agents"] = [
                a
                for a in envelope.get("agents", [])
                if caller.may_read(str(a.get("id") or ""))
            ]
        _audit(caller, "export_agents")
        return envelope

    @mcp.tool()
    def commit_to_federation(
        agent_id: str,
        agent_type: str | None = None,
        instance: str | None = None,
        role_narrative: str | None = None,
        entities: list[dict] | None = None,
        sessions: list[dict] | None = None,
        mode: str = "merge",
    ) -> dict:
        """
        Write a contribution to the federation under ``agent_id``.

        The write-side companion to the read tools. Lets MCP-aware cloud
        agents (Claude Desktop, ChatGPT desktop, other webview/cloud-only
        agents that have no readable on-disk store for Bourdon to scrape)
        push their own L5 contributions into the federation by calling
        this tool when they decide a piece of context is worth sharing.

        Parameters
        ----------
        agent_id : str
            Agent slug, e.g. ``claude-desktop``. Must match
            ``^[a-z0-9][a-z0-9_-]*$``.
        agent_type : str, optional
            Required when creating a NEW manifest for this agent_id; one
            of the L5 schema enum values (``code-assistant``,
            ``note-capture``, ``other``, etc.). Ignored when merging
            into an existing manifest that already has agent.type set.
        instance : str, optional
            Optional machine/deployment identifier.
        role_narrative : str, optional
            Free-text description of the agent's role within a fleet.
        entities : list of dict, optional
            Each entity dict needs at minimum a non-empty ``name`` (other
            L5 entity fields -- type, summary, tags, visibility, aliases,
            valid_from, valid_to -- pass through as-is).
        sessions : list of dict, optional
            Each session dict needs at minimum a non-empty ``date`` (ISO
            8601 string). Other L5 session fields -- cwd, project_focus,
            key_actions, files_touched, visibility -- pass through.
        mode : "merge" or "replace"
            ``merge`` (default) unions new rows with the existing manifest.
            Entities dedupe by ``name.lower()``; sessions dedupe by
            ``(date, cwd)``. List fields (tags, aliases, key_actions,
            files_touched, project_focus) are unioned on dupe; non-list
            fields are overwritten. ``replace`` wipes the manifest and
            writes only the provided content.

        Returns
        -------
        dict with the write summary (counts added/updated/total, path,
        agent identity, last_updated). On invalid input, returns a
        structured error response with an ``error`` key.

        Quarantined callers (v0.9.0): the write is STAGED under
        ``<library>/staging/<caller>/`` instead of touching the live store,
        and only the caller's own ``agent_id`` namespace is writable.
        Staged content is invisible to every read until the operator runs
        ``bourdon staging promote <agent>``.
        """
        caller = _resolve_caller()
        if not caller.is_trusted:
            if agent_id != caller.agent_id:
                return _denied(
                    "commit_to_federation",
                    caller,
                    namespace=agent_id,
                    detail=(
                        "quarantined members may only write their own "
                        f"namespace ({caller.agent_id!r})"
                    ),
                )
            try:
                for row in entities or []:
                    if not isinstance(row, dict) or not str(row.get("name") or "").strip():
                        raise ValueError("each entity needs a non-empty 'name'")
                for row in sessions or []:
                    if not isinstance(row, dict) or not str(row.get("date") or "").strip():
                        raise ValueError("each session needs a non-empty ISO-8601 'date'")
                from core.federation_staging import merge_into_staged

                path = merge_into_staged(
                    store.library_path,
                    caller.agent_id,
                    agent_id,
                    entities,
                    sessions,
                    agent_type=agent_type,
                    instance=instance,
                    role_narrative=role_narrative,
                )
            except ValueError as exc:
                return {"error": str(exc), "agent_id": agent_id, "mode": mode}
            _audit(caller, "commit_to_federation", agent_id, detail="staged")
            return {
                "staged": True,
                "agent_id": agent_id,
                "path": str(path),
                "note": (
                    "quarantined write staged for review; an operator must run "
                    "`bourdon staging promote " + agent_id + "` before it "
                    "propagates to the federation"
                ),
            }
        _audit(caller, "commit_to_federation", agent_id)
        try:
            return store.commit_l5(
                agent_id=agent_id,
                agent_type=agent_type,
                instance=instance,
                role_narrative=role_narrative,
                entities=entities,
                sessions=sessions,
                mode=mode,
            )
        except ValueError as exc:
            return {
                "error": str(exc),
                "agent_id": agent_id,
                "mode": mode,
            }

    @mcp.tool()
    async def get_cross_agent_summary(
        project: str,
        access_level: str = "public",
        include_private: bool = False,
    ) -> dict:
        """
        Aggregate everything the federation knows about a project.

        Returns agents that touched it, recent sessions whose
        ``project_focus`` references it, and entity matches. When peers are
        configured (``--peer`` flag), peer libraries are merged in with
        agents tagged as ``peer:<peer-name>:<agent>``.
        """
        caller = _resolve_caller()
        if not caller.is_trusted:
            return _denied("get_cross_agent_summary", caller)
        _audit(caller, "get_cross_agent_summary")
        summary = await store.get_cross_agent_summary_federated(
            project,
            include_private=include_private,
            access_level=access_level,
        )
        return summary.to_dict()

    @mcp.tool()
    async def prepare_recognition_context(
        prompt: str,
        access_level: str = "team",
        include_private: bool = False,
    ) -> dict:
        """
        Return immediate recognition and a bounded prompt-context fragment.

        This is the MCP-facing timing layer: agents can call it at turn start,
        prepend the returned ``prompt_context`` to their own model prompt, and
        continue with deeper retrieval in parallel.

        When peers are configured (``--peer`` flag, Phase 1.6+), the local
        recognition fires first (~1.2 ms substrate) then peers are queried
        in parallel under a tight per-peer timeout. Peer-matched entities
        are merged into ``matched_entities`` with agents tagged
        ``peer:<peer-name>:<agent>``. Slow / dead peers are dropped and
        reported in ``peer_latencies_us`` so the response is bounded.
        """
        caller = _resolve_caller()
        if not caller.is_trusted:
            return _denied("prepare_recognition_context", caller)
        _audit(caller, "prepare_recognition_context")
        if store.peers:
            return await prepare_recognition_context_federated(
                store,
                prompt,
                access_level=access_level,
                include_private=include_private,
            )
        return prepare_recognition_context_from_store(
            store,
            prompt,
            access_level=access_level,
            include_private=include_private,
        )

    @mcp.tool()
    def compile_codex_turn(
        prompt: str,
        cwd: str | None = None,
        access_level: str = "team",
        max_items: int = 6,
        max_chars: int = 1800,
    ) -> dict:
        """
        Compile a turn-scoped Codex recognition brief.

        This is the active recognition-orchestration surface for Codex: it
        ranks prompt, cwd/repo identity, local Codex thread metadata, and L6
        federation context into a compact prompt fragment without depending on
        native Stage 1 summarization.
        """
        caller = _resolve_caller()
        if not caller.is_trusted:
            return _denied("compile_codex_turn", caller)
        _audit(caller, "compile_codex_turn")
        return compile_codex_turn_from_store(
            store,
            prompt,
            cwd=cwd,
            access_level=access_level,
            max_items=max_items,
            max_chars=max_chars,
        )

    @mcp.tool()
    async def get_deeper_context(
        prompt: str,
        access_level: str = "team",
        include_private: bool = False,
    ) -> dict:
        """
        Return post-recognition L2 context for the prompt.

        This companion tool is intentionally separate from
        ``prepare_recognition_context`` so immediate recognition never waits on
        retrieval. If L2 is disabled or unavailable, the returned context is
        empty.
        """
        caller = _resolve_caller()
        if not caller.is_trusted:
            return _denied("get_deeper_context", caller)
        _audit(caller, "get_deeper_context")
        return await get_deeper_context_for_prompt(
            prompt,
            access_level=access_level,
            include_private=include_private,
        )

    return mcp


# -- CLI entry point -----------------------------------------------------------


DEFAULT_PEERS_CONFIG = Path.home() / ".bourdon" / "peers.yaml"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="bourdon-l6-server",
        description="Launch the Bourdon L6 federation MCP server.",
    )
    parser.add_argument(
        "--library",
        type=Path,
        default=DEFAULT_LIBRARY_PATH,
        help=f"Path to the agent-library directory (default: {DEFAULT_LIBRARY_PATH})",
    )
    parser.add_argument(
        "--transport",
        choices=("stdio", "http"),
        default="stdio",
        help="MCP transport (default: stdio)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=7500,
        help="Port for HTTP transport (ignored for stdio, default: 7500)",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help=(
            "Bind host for HTTP transport (default: 127.0.0.1 — loopback only). "
            "Use 0.0.0.0 for cross-host / Tailnet federation; non-loopback "
            "binds require auth configured (bourdon agent add / "
            "BOURDON_PEER_TOKEN_SERVER) or the server refuses to start."
        ),
    )
    parser.add_argument(
        "--peer",
        action="append",
        default=[],
        help=(
            "Peer L6 server URL (e.g. http://pc.tailnet:7500). Repeatable. "
            "Combined with peers loaded from --peers-config. See "
            "config/peers.example.yaml for the declarative format."
        ),
    )
    parser.add_argument(
        "--peers-config",
        type=Path,
        default=DEFAULT_PEERS_CONFIG,
        help=(
            "Path to a YAML file listing peer L6 servers. Loaded if it "
            "exists. Per-peer entries: name, url, token_env. Skipped "
            "silently if the file is absent."
        ),
    )
    parser.add_argument(
        "--allow-unauthenticated",
        action="store_true",
        help=(
            "Serve HTTP transport without Bearer-token auth. Off by default "
            "(server requires Authorization: Bearer <env BOURDON_PEER_TOKEN_SERVER> "
            "on /mcp). Only safe on a closed network (Tailnet, localhost)."
        ),
    )
    return parser.parse_args()


def load_peers(
    config_path: Path,
    inline_urls: list[str],
) -> list[RemoteL6Client]:
    """Build the peers list from CLI flags + optional config file.

    Returns an empty list if no peers are configured. Import of
    :class:`RemoteL6Client` is local so importing this module without the
    ``[federation]`` extras stays cheap.

    Shared by both serve entry points: ``python -m core.l6_server`` (``main``)
    and ``bourdon serve`` (``cli.main._handle_serve``). Public so the CLI can
    reuse the exact same flag + config-file resolution.
    """
    from core.l6_remote import RemoteL6Client

    peers: list[RemoteL6Client] = []
    seen_urls: set[str] = set()
    if config_path.exists():
        try:
            import yaml as _yaml

            data = _yaml.safe_load(config_path.read_text()) or {}
            for entry in data.get("peers") or []:
                if not isinstance(entry, dict):
                    continue
                url = entry.get("url")
                if not isinstance(url, str) or not url:
                    continue
                name = entry.get("name") or url
                token_env = entry.get("token_env") or "BOURDON_PEER_TOKEN"
                if url in seen_urls:
                    continue
                seen_urls.add(url)
                peers.append(RemoteL6Client(url=url, name=name, token_env=token_env))
        except Exception as exc:  # noqa: BLE001 -- config errors degrade to "no peers"
            logger.warning("Failed to load peers config %s: %s", config_path, exc)
    for url in inline_urls:
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)
        peers.append(RemoteL6Client(url=url, name=url))
    return peers


def _is_loopback_host(host: str) -> bool:
    """Whether a bind host is loopback-only."""
    if host in ("localhost",):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _build_auth_middleware(registry: FederationRegistry):
    """Starlette middleware enforcing Authorization: Bearer <token> (v0.9.0).

    Two token classes authenticate, both compared constant-time:

    - **Per-agent tokens** from the federation registry
      (``bourdon agent add`` / ``~/.bourdon/federation.yaml``). Resolve to
      that agent's :class:`AgentIdentity` (tier + grants). Revoked members
      get 401 — the registry re-reads on mtime change, so ``bourdon revoke``
      takes effect on a running server without a restart.
    - **The legacy shared token** (env ``BOURDON_PEER_TOKEN_SERVER``),
      mapped to the trusted ``operator`` identity. This is the v0.8.0
      migration path: existing PC<->Mac peering keeps working unchanged.

    If neither auth source is configured and the server was launched without
    ``--allow-unauthenticated``, every request gets 503 — fail closed.
    Token material never appears in any log or response body.
    """
    legacy = os.environ.get("BOURDON_PEER_TOKEN_SERVER")

    try:
        from starlette.middleware.base import BaseHTTPMiddleware
        from starlette.responses import JSONResponse
    except ImportError as exc:  # pragma: no cover -- starlette ships with fastmcp
        raise RuntimeError("starlette is required for HTTP transport") from exc

    class _BearerAuth(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            if legacy is None and not registry.has_active_agents():
                return JSONResponse(
                    {
                        "error": (
                            "Server has no auth configured (no registered agents "
                            "via `bourdon agent add` and no BOURDON_PEER_TOKEN_SERVER) "
                            "and was launched without --allow-unauthenticated."
                        )
                    },
                    status_code=503,
                )
            header = request.headers.get("authorization") or ""
            if not header.lower().startswith("bearer "):
                return JSONResponse({"error": "missing Bearer token"}, status_code=401)
            token = header.split(" ", 1)[1].strip()
            identity: AgentIdentity | None = None
            if legacy is not None and hmac.compare_digest(
                token.encode("utf-8"), legacy.encode("utf-8")
            ):
                identity = OPERATOR
            if identity is None:
                identity = registry.authenticate(token)
            if identity is None:
                # Deliberately does not distinguish invalid vs revoked, and
                # never echoes the presented token.
                return JSONResponse(
                    {"error": "invalid or revoked Bearer token"}, status_code=401
                )
            request.state.bourdon_identity = identity
            ctx_token = set_caller(identity)
            try:
                return await call_next(request)
            finally:
                reset_caller(ctx_token)

    return _BearerAuth


def _build_operator_identity_middleware():
    """Middleware for ``--allow-unauthenticated`` (loopback-only) serving.

    Binds the trusted operator identity to every request so the tier
    enforcement layer treats local unauthenticated callers exactly like the
    stdio transport. Without this, the fail-closed resolver would quarantine
    them.
    """
    try:
        from starlette.middleware.base import BaseHTTPMiddleware
    except ImportError as exc:  # pragma: no cover -- starlette ships with fastmcp
        raise RuntimeError("starlette is required for HTTP transport") from exc

    class _OperatorIdentity(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            request.state.bourdon_identity = OPERATOR
            ctx_token = set_caller(OPERATOR)
            try:
                return await call_next(request)
            finally:
                reset_caller(ctx_token)

    return _OperatorIdentity


def run_l6_server(
    server: Any,
    *,
    transport: str = "stdio",
    port: int = 7500,
    host: str = "127.0.0.1",
    allow_unauthenticated: bool = False,
    registry: FederationRegistry | None = None,
) -> None:
    """Run an already-created L6 MCP server under the requested transport.

    Shared by both serve entry points (``python -m core.l6_server`` and
    ``bourdon serve``) so they get identical transport, bind host, and
    Bearer-auth behavior.

    - ``stdio`` (default): blocks until the connecting MCP client disconnects.
    - ``http``: always run under uvicorn so the bind ``host`` is ours to set.

    v0.9.0 bind/auth contract (spec/SPEC_v0.9.0.md D8):

    - Default bind is **127.0.0.1** (loopback). This is a breaking change
      from v0.8.0's ``0.0.0.0`` default — Tailnet / cross-host peers must
      pass ``--host 0.0.0.0`` explicitly.
    - A non-loopback bind REQUIRES auth configured (>=1 active registered
      agent, or the legacy ``BOURDON_PEER_TOKEN_SERVER``). Otherwise the
      server **exits non-zero at startup** instead of serving.
    - ``--allow-unauthenticated`` is honored on loopback binds only; combined
      with a non-loopback host the server refuses to start. There is no
      anonymous-access code path on a network-reachable bind.
    - HTTP serves **stateless** streamable-HTTP: every request is handled in
      its own task, so the auth middleware's caller identity deterministically
      reaches the tool handlers. Peer clients already open per-call sessions.
    """
    if transport == "stdio":
        server.run()  # fastmcp default: stdio
        return

    # HTTP transport: always via uvicorn so we control the bind host + the
    # middleware stack. (Both authed and unauth paths bind `host`.)
    try:
        import uvicorn
        from starlette.middleware import Middleware
    except ImportError as exc:
        raise RuntimeError(
            "uvicorn + starlette are required for HTTP transport. "
            "Install via: pip install 'bourdon[server,federation]'"
        ) from exc

    if registry is None:
        registry = FederationRegistry()
    auth_configured = bool(os.environ.get("BOURDON_PEER_TOKEN_SERVER")) or (
        registry.has_active_agents()
    )
    if not _is_loopback_host(host):
        if allow_unauthenticated:
            raise SystemExit(
                f"refusing to start: --allow-unauthenticated with non-loopback "
                f"bind {host!r}. Anonymous access is loopback-only; register an "
                "agent token (`bourdon agent add <id>`) or set "
                "BOURDON_PEER_TOKEN_SERVER to serve on this interface."
            )
        if not auth_configured:
            raise SystemExit(
                f"refusing to start: bind {host!r} is network-reachable but no "
                "auth is configured. Register an agent token "
                "(`bourdon agent add <id>`) or set BOURDON_PEER_TOKEN_SERVER, "
                "or bind 127.0.0.1."
            )

    if allow_unauthenticated:
        logger.warning(
            "Serving HTTP transport WITHOUT auth on %s:%d (--allow-unauthenticated, "
            "loopback-only). Local callers get trusted operator access.",
            host,
            port,
        )
        ident_cls = _build_operator_identity_middleware()
        app = server.http_app(middleware=[Middleware(ident_cls)], stateless_http=True)
    else:
        auth_cls = _build_auth_middleware(registry)
        app = server.http_app(middleware=[Middleware(auth_cls)], stateless_http=True)
    uvicorn.run(app, host=host, port=port, log_level="info")


def main() -> None:
    args = _parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    peers = load_peers(args.peers_config, args.peer)
    logger.info(
        "Bourdon L6 server starting -- library=%s, transport=%s, peers=%d",
        args.library,
        args.transport,
        len(peers),
    )
    for p in peers:
        logger.info("  peer: %s -> %s", p.name, p.url)
    store = L6Store(args.library, peers=peers)
    logger.info("Loaded %d agent(s): %s", len(store.list_agents()), store.list_agents())
    registry = FederationRegistry()
    server = create_l6_server(store, registry=registry)
    run_l6_server(
        server,
        transport=args.transport,
        port=args.port,
        host=args.host,
        allow_unauthenticated=args.allow_unauthenticated,
        registry=registry,
    )


if __name__ == "__main__":
    main()
