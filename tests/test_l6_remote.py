"""Tests for core/l6_remote.py (Phase 1.6 peer L6 federation client).

These tests cover the URL normalization, header construction, and JSON
parsing of MCP `CallToolResult` content. End-to-end MCP-over-HTTP integration
is exercised separately via the two-server local integration described in
PR-71 verification notes.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import pytest

from core.l6_remote import RemoteL6Client


# ---------------------------------------------------------------------------
# Helpers — fake MCP client session that RemoteL6Client can talk to via the
# _session() context manager. We monkeypatch _session to yield this fake.
# ---------------------------------------------------------------------------


@dataclass
class FakeTextItem:
    text: str


@dataclass
class FakeCallToolResult:
    content: list[Any]


@dataclass
class FakeSession:
    """Pretend MCP client session. Returns a FakeCallToolResult per call_tool."""

    responses: dict[str, Any] = field(default_factory=dict)
    calls: list[tuple[str, dict]] = field(default_factory=list)
    raise_on: set[str] = field(default_factory=set)

    async def call_tool(self, name: str, arguments: dict) -> FakeCallToolResult:
        self.calls.append((name, arguments))
        if name in self.raise_on:
            raise RuntimeError(f"injected failure on {name}")
        payload = self.responses.get(name)
        return FakeCallToolResult(content=[FakeTextItem(text=json.dumps(payload))])


def _wire_fake_session(client: RemoteL6Client, fake: FakeSession) -> None:
    """Replace RemoteL6Client._session with an async context manager yielding `fake`."""
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _stub():
        yield fake

    client._session = _stub  # type: ignore[method-assign]


# ---------------------------------------------------------------------------
# URL + headers
# ---------------------------------------------------------------------------


def test_url_normalization_appends_mcp_path() -> None:
    c = RemoteL6Client(url="http://example.local:7500", name="x")
    assert c.url == "http://example.local:7500/mcp"


def test_url_normalization_idempotent_when_mcp_present() -> None:
    c = RemoteL6Client(url="http://example.local:7500/mcp", name="x")
    assert c.url == "http://example.local:7500/mcp"


def test_url_normalization_strips_trailing_slash() -> None:
    c = RemoteL6Client(url="http://example.local:7500/", name="x")
    assert c.url == "http://example.local:7500/mcp"


def test_headers_includes_bearer_token_when_env_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOURDON_PEER_TOKEN", "shh-its-a-secret")
    c = RemoteL6Client(url="http://x:7500", name="x")
    assert c._headers() == {"Authorization": "Bearer shh-its-a-secret"}


def test_headers_empty_when_no_token_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BOURDON_PEER_TOKEN", raising=False)
    c = RemoteL6Client(url="http://x:7500", name="x")
    assert c._headers() == {}


def test_headers_uses_custom_token_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BOURDON_PEER_TOKEN", raising=False)
    monkeypatch.setenv("BOURDON_PEER_TOKEN_PC", "pc-token")
    c = RemoteL6Client(url="http://x:7500", name="pc", token_env="BOURDON_PEER_TOKEN_PC")
    assert c._headers() == {"Authorization": "Bearer pc-token"}


def test_empty_token_env_skips_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BOURDON_PEER_TOKEN", raising=False)
    c = RemoteL6Client(url="http://x:7500", name="x", token_env="")
    assert c._headers() == {}


# ---------------------------------------------------------------------------
# Tool calls — happy paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_agents_extracts_agents_field() -> None:
    c = RemoteL6Client(url="http://x:7500", name="x", token_env="")
    fake = FakeSession(responses={"list_agents": {"agents": ["claude-code", "codex"]}})
    _wire_fake_session(c, fake)
    agents = await c.list_agents()
    assert agents == ["claude-code", "codex"]
    assert fake.calls == [("list_agents", {"federation_hop": 1})]


@pytest.mark.asyncio
async def test_list_agents_accepts_bare_list_response() -> None:
    c = RemoteL6Client(url="http://x:7500", name="x", token_env="")
    fake = FakeSession(responses={"list_agents": ["a", "b"]})
    _wire_fake_session(c, fake)
    assert await c.list_agents() == ["a", "b"]


@pytest.mark.asyncio
async def test_find_entity_forwards_args_and_returns_matches() -> None:
    c = RemoteL6Client(url="http://x:7500", name="x", token_env="")
    fake = FakeSession(
        responses={
            "find_entity": {
                "name": "Bourdon",
                "matches": [{"name": "Bourdon", "agents": ["codex"], "types": ["project"]}],
            }
        }
    )
    _wire_fake_session(c, fake)
    matches = await c.find_entity("Bourdon", access_level="team", include_private=False)
    assert len(matches) == 1
    assert matches[0]["name"] == "Bourdon"
    assert fake.calls == [
        (
            "find_entity",
            {
                "name": "Bourdon",
                "access_level": "team",
                "include_private": False,
                "federation_hop": 1,
            },
        )
    ]


@pytest.mark.asyncio
async def test_list_recent_work_passes_optional_args() -> None:
    c = RemoteL6Client(url="http://x:7500", name="x", token_env="")
    fake = FakeSession(responses={"list_recent_work": {"sessions": [], "has_more": False, "next_cursor": None}})
    _wire_fake_session(c, fake)
    result = await c.list_recent_work(since="2026-05-01", agent="codex", limit=5)
    assert result["sessions"] == []
    _, args = fake.calls[0]
    assert args["since"] == "2026-05-01"
    assert args["agent"] == "codex"
    assert args["limit"] == 5
    # access_level/include_private/summary defaults always present
    assert "access_level" in args
    assert "include_private" in args
    assert "summary" in args


@pytest.mark.asyncio
async def test_get_cross_agent_summary_returns_dict() -> None:
    c = RemoteL6Client(url="http://x:7500", name="x", token_env="")
    fake = FakeSession(
        responses={
            "get_cross_agent_summary": {
                "project": "Bourdon",
                "agents": ["codex"],
                "recent_sessions": [],
                "entities": [],
            }
        }
    )
    _wire_fake_session(c, fake)
    result = await c.get_cross_agent_summary("Bourdon")
    assert result["project"] == "Bourdon"


@pytest.mark.asyncio
async def test_prepare_recognition_context_returns_dict() -> None:
    c = RemoteL6Client(url="http://x:7500", name="x", token_env="")
    fake = FakeSession(
        responses={
            "prepare_recognition_context": {
                "prompt": "hi",
                "recognition": "I know Bourdon.",
                "matched_entities": [],
                "recognition_latency_us": 1.2,
            }
        }
    )
    _wire_fake_session(c, fake)
    result = await c.prepare_recognition_context("hi")
    assert result["recognition"].startswith("I know")


# ---------------------------------------------------------------------------
# Tool calls — failure paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_failed_call_returns_empty_collection() -> None:
    c = RemoteL6Client(url="http://x:7500", name="x", token_env="")
    fake = FakeSession(raise_on={"list_agents"})
    _wire_fake_session(c, fake)
    # Must not raise — federation degrades gracefully when a peer is dead.
    assert await c.list_agents() == []


@pytest.mark.asyncio
async def test_failed_find_entity_returns_empty_list() -> None:
    c = RemoteL6Client(url="http://x:7500", name="x", token_env="")
    fake = FakeSession(raise_on={"find_entity"})
    _wire_fake_session(c, fake)
    assert await c.find_entity("Bourdon") == []


@pytest.mark.asyncio
async def test_failed_list_recent_work_returns_empty_envelope() -> None:
    c = RemoteL6Client(url="http://x:7500", name="x", token_env="")
    fake = FakeSession(raise_on={"list_recent_work"})
    _wire_fake_session(c, fake)
    result = await c.list_recent_work()
    assert result == {"sessions": [], "next_cursor": None, "has_more": False}


@pytest.mark.asyncio
async def test_unknown_tool_name_raises() -> None:
    c = RemoteL6Client(url="http://x:7500", name="x", token_env="")
    with pytest.raises(ValueError, match="unknown peer tool"):
        await c._call_tool("not_a_real_tool", {})


# ---------------------------------------------------------------------------
# Depth-1 federation contract (#139)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_all_fanout_queries_send_federation_hop() -> None:
    """Every fan-out-capable query must declare itself as hop 1.

    Without this, bidirectional peering (A lists B, B lists A) recurses until
    fd exhaustion — issue #139. ``export_agents`` is exempt: its server tool
    is local-only by construction.
    """
    c = RemoteL6Client(url="http://x:7500", name="x", token_env="")
    fake = FakeSession(
        responses={
            "list_agents": {"agents": []},
            "find_entity": {"matches": []},
            "list_recent_work": {"sessions": [], "has_more": False, "next_cursor": None},
            "get_cross_agent_summary": {},
            "prepare_recognition_context": {},
        }
    )
    _wire_fake_session(c, fake)

    await c.list_agents()
    await c.find_entity("Bourdon")
    await c.list_recent_work()
    await c.get_cross_agent_summary("Bourdon")
    await c.prepare_recognition_context("hi")

    assert len(fake.calls) == 5
    for name, args in fake.calls:
        assert args.get("federation_hop") == 1, f"{name} did not send federation_hop=1"
