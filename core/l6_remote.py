"""Remote L6 client — speak to a peer Bourdon L6 server over MCP-over-HTTP.

Phase 1.6 deliverable. The peer is a Bourdon `core.l6_server` instance launched
with `--transport http`. This client opens a streamable-HTTP MCP session per
call, invokes the matching ``@mcp.tool()`` by name, and returns the response
dict. Failures (network, auth, peer down) are logged and downgraded to empty
responses so a single dead peer never breaks a federated query.

Auth: Bearer token via ``Authorization: Bearer <token>`` header. Token source
is environment variable (default ``BOURDON_PEER_TOKEN``, overridable per peer
via ``token_env``). When no token is set, requests go without an Authorization
header — peers running with ``--allow-unauthenticated`` will still serve.

Why MCP-over-HTTP instead of a bespoke REST surface: the server already
exposes its query surface as ``@mcp.tool()`` decorated functions. Reusing them
as the wire protocol means peers and direct MCP clients share one code path,
and the request/response shapes are already documented.
"""

from __future__ import annotations

import json
import logging
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, AsyncIterator

logger = logging.getLogger(__name__)


_PEER_TOOL_NAMES = {
    "list_agents",
    "query_agent_memory",
    "list_recent_work",
    "find_entity",
    "get_cross_agent_summary",
    "prepare_recognition_context",
    "get_deeper_context",
}


@dataclass
class RemoteL6Client:
    """Async client that proxies L6Store query methods to a remote peer.

    Parameters
    ----------
    url : str
        Base URL of the peer's MCP HTTP endpoint, e.g. ``http://pc.tailnet:7500/mcp``.
        Trailing ``/mcp`` is appended if missing.
    name : str
        Short identifier for this peer (used in log lines + merge dedupe).
    token_env : str
        Env var to read the bearer token from. Defaults to ``BOURDON_PEER_TOKEN``.
        Set to empty string to skip auth (only useful for ``--allow-unauthenticated``
        peers).
    timeout : float
        Per-call timeout in seconds. Federated queries are NOT in hot paths, but
        a slow peer should not block the whole fan-out — defaults to 5s.
    """

    url: str
    name: str
    token_env: str = "BOURDON_PEER_TOKEN"
    timeout: float = 5.0

    def __post_init__(self) -> None:
        # Normalize URL — MCP streamable-HTTP path is /mcp by convention.
        self.url = self.url.rstrip("/")
        if not self.url.endswith("/mcp"):
            self.url = f"{self.url}/mcp"

    # ------------------------------------------------------------------ utils

    def _headers(self) -> dict[str, str]:
        token = os.environ.get(self.token_env) if self.token_env else None
        if token:
            return {"Authorization": f"Bearer {token}"}
        return {}

    @asynccontextmanager
    async def _session(self) -> AsyncIterator[Any]:
        """Open an MCP streamable-HTTP client session.

        Imports are local because we don't want a hard runtime dep on the
        ``mcp`` package for installs that never use peer federation. Install
        the ``[federation]`` extras to enable.
        """
        try:
            from mcp import ClientSession
            from mcp.client.streamable_http import streamablehttp_client
        except ImportError as exc:  # pragma: no cover -- import guard
            raise RuntimeError(
                "MCP client SDK missing — install bourdon[federation] to enable peer L6."
            ) from exc

        async with streamablehttp_client(
            self.url,
            headers=self._headers(),
            timeout=self.timeout,
        ) as (read, write, _get_session_id):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session

    async def _call_tool(self, tool_name: str, arguments: dict[str, Any]) -> Any:
        """Call one MCP tool on the peer. Returns parsed JSON or None on failure."""
        if tool_name not in _PEER_TOOL_NAMES:  # paranoia — typo guard
            raise ValueError(f"unknown peer tool: {tool_name!r}")
        try:
            async with self._session() as session:
                result = await session.call_tool(tool_name, arguments=arguments)
        except Exception as exc:  # noqa: BLE001 — never raise from a peer call
            logger.warning("peer %s tool %s failed: %s", self.name, tool_name, exc)
            return None
        # The MCP `CallToolResult` carries `.content` (list of TextContent /
        # ImageContent / etc.). For Bourdon tools the payload is JSON-encoded text.
        for item in result.content:
            text = getattr(item, "text", None)
            if not text:
                continue
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                logger.warning(
                    "peer %s tool %s returned non-JSON content (head): %s",
                    self.name,
                    tool_name,
                    text[:200],
                )
                continue
        return None

    # ----------------------------------------------------- mirrored query API

    async def list_agents(self) -> list[str]:
        result = await self._call_tool("list_agents", {})
        if isinstance(result, list):
            return [str(a) for a in result if isinstance(a, str)]
        if isinstance(result, dict) and isinstance(result.get("agents"), list):
            return [str(a) for a in result["agents"] if isinstance(a, str)]
        return []

    async def find_entity(
        self,
        name: str,
        access_level: str = "team",
        include_private: bool = False,
    ) -> list[dict]:
        result = await self._call_tool(
            "find_entity",
            {
                "name": name,
                "access_level": access_level,
                "include_private": include_private,
            },
        )
        if isinstance(result, list):
            return [m for m in result if isinstance(m, dict)]
        if isinstance(result, dict) and isinstance(result.get("matches"), list):
            return [m for m in result["matches"] if isinstance(m, dict)]
        return []

    async def list_recent_work(
        self,
        since: str | None = None,
        agent: str | None = None,
        access_level: str = "team",
        include_private: bool = False,
        limit: int | None = None,
        cursor: str | None = None,
        summary: bool = False,
    ) -> dict:
        args: dict[str, Any] = {
            "access_level": access_level,
            "include_private": include_private,
            "summary": summary,
        }
        if since is not None:
            args["since"] = since
        if agent is not None:
            args["agent"] = agent
        if limit is not None:
            args["limit"] = limit
        if cursor is not None:
            args["cursor"] = cursor
        result = await self._call_tool("list_recent_work", args)
        if isinstance(result, dict):
            return result
        return {"sessions": [], "next_cursor": None, "has_more": False}

    async def get_cross_agent_summary(
        self,
        project: str,
        access_level: str = "team",
        include_private: bool = False,
    ) -> dict:
        result = await self._call_tool(
            "get_cross_agent_summary",
            {
                "project": project,
                "access_level": access_level,
                "include_private": include_private,
            },
        )
        return result if isinstance(result, dict) else {}

    async def prepare_recognition_context(
        self,
        prompt: str,
        access_level: str = "team",
        include_private: bool = False,
    ) -> dict:
        result = await self._call_tool(
            "prepare_recognition_context",
            {
                "prompt": prompt,
                "access_level": access_level,
                "include_private": include_private,
            },
        )
        return result if isinstance(result, dict) else {}

    async def get_deeper_context(
        self,
        prompt: str,
        access_level: str = "team",
        include_private: bool = False,
    ) -> dict:
        result = await self._call_tool(
            "get_deeper_context",
            {
                "prompt": prompt,
                "access_level": access_level,
                "include_private": include_private,
            },
        )
        return result if isinstance(result, dict) else {}
