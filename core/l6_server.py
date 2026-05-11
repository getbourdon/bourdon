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
"""

from __future__ import annotations

import argparse
import logging
import re
import time as time_module
from datetime import datetime
from pathlib import Path
from typing import Any

from core.l2 import query_l2
from core.l6_store import DEFAULT_LIBRARY_PATH, L6Store
from core.recognition_runtime import recognition_first

logger = logging.getLogger(__name__)

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


def create_l6_server(store: L6Store, name: str = "bourdon-l6") -> Any:
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

    # ---- Resources ------------------------------------------------------------

    @mcp.resource("agent-library://agents")
    def list_agents_resource() -> list[str]:
        """List of all agent IDs known to the federation."""
        return store.list_agents()

    @mcp.resource("agent-library://agents/{agent_id}/memory")
    def get_agent_memory_resource(agent_id: str) -> dict:
        """
        Full visibility-filtered L5 manifest for one agent.

        Returns an empty dict with an ``error`` key when the agent is
        unknown (MCP resources can't signal 404 cleanly, so we surface
        it in the payload).
        """
        manifest = store.get_agent_manifest(agent_id, include_private=False)
        if manifest is None:
            return {"error": f"agent not found: {agent_id}"}
        return manifest

    @mcp.resource("agent-library://entities/{name}")
    def get_entity_resource(name: str) -> list[dict]:
        """Cross-agent view of one entity by name."""
        return [
            m.to_dict()
            for m in store.find_entity(name, include_private=False, access_level="public")
        ]

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
    def list_recent_work(
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
        return {
            "since": since,
            "agent": agent,
            "access_level": access_level,
            "include_private": include_private,
            "limit": limit,
            "cursor": cursor,
            "summary": summary,
            "sessions": [s.to_dict(summary=summary) for s in page.sessions],
            "next_cursor": page.next_cursor,
            "has_more": page.has_more,
        }

    @mcp.tool()
    def find_entity(
        name: str,
        access_level: str = "public",
        include_private: bool = False,
    ) -> dict:
        """
        Find an entity by name across all agents.

        ``include_private`` defaults to False. Callers that genuinely need
        unredacted output must pass ``True`` explicitly -- this is a second
        line of defense on top of per-manifest visibility policy.
        """
        matches = store.find_entity(
            name,
            include_private=include_private,
            access_level=access_level,
        )
        return {
            "name": name,
            "access_level": access_level,
            "include_private": include_private,
            "matches": [m.to_dict() for m in matches],
        }

    @mcp.tool()
    def get_cross_agent_summary(
        project: str,
        access_level: str = "public",
        include_private: bool = False,
    ) -> dict:
        """
        Aggregate everything the federation knows about a project.

        Returns agents that touched it, recent sessions whose
        ``project_focus`` references it, and entity matches.
        """
        summary = store.get_cross_agent_summary(
            project,
            include_private=include_private,
            access_level=access_level,
        )
        return summary.to_dict()

    @mcp.tool()
    def prepare_recognition_context(
        prompt: str,
        access_level: str = "team",
        include_private: bool = False,
    ) -> dict:
        """
        Return immediate recognition and a bounded prompt-context fragment.

        This is the MCP-facing timing layer: agents can call it at turn start,
        prepend the returned ``prompt_context`` to their own model prompt, and
        continue with deeper retrieval in parallel.
        """
        return prepare_recognition_context_from_store(
            store,
            prompt,
            access_level=access_level,
            include_private=include_private,
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
        return await get_deeper_context_for_prompt(
            prompt,
            access_level=access_level,
            include_private=include_private,
        )

    return mcp


# -- CLI entry point -----------------------------------------------------------


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
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    logger.info(
        "Bourdon L6 server starting -- library=%s, transport=%s",
        args.library,
        args.transport,
    )
    store = L6Store(args.library)
    logger.info("Loaded %d agent(s): %s", len(store.list_agents()), store.list_agents())
    server = create_l6_server(store)
    if args.transport == "stdio":
        server.run()  # fastmcp default: stdio
    else:
        # HTTP transport -- fastmcp exposes this via run_http or similar.
        # We keep this surface thin because stdio is the MCP default and
        # HTTP setup varies by fastmcp version.
        try:
            server.run(transport="http", port=args.port)
        except TypeError:
            # Older fastmcp signatures: fall back to stdio with a warning.
            logger.warning(
                "This fastmcp version does not accept transport='http'; falling back to stdio."
            )
            server.run()


if __name__ == "__main__":
    main()
