"""Tests for L6Store peer-federated query methods (Phase 1.6).

These tests use a stub RemoteL6Client (no network) injected into L6Store.peers
and verify the merge logic for the federated variants.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

from core.l6_store import L6Store


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def populated_library(tmp_path: Path) -> Path:
    """Local library with one agent (`local-agent`) owning entity `Bourdon`."""
    lib = tmp_path / "agent-library"
    (lib / "agents").mkdir(parents=True)
    manifest = {
        "schema_version": "0.5.0",
        "agent": {"id": "local-agent", "type": "code-assistant"},
        "known_entities": [
            {
                "name": "Bourdon",
                "type": "project",
                "summary": "local-agent's view of Bourdon",
                "visibility": "team",
            },
            {
                "name": "ILTT",
                "type": "project",
                "summary": "local-only entity",
                "visibility": "team",
            },
        ],
        "recent_sessions": [
            {
                "date": "2026-05-16",
                "cwd": "/Users/r/bourdon",
                "project_focus": ["Bourdon"],
                "key_actions": ["did the thing"],
                "visibility": "team",
            }
        ],
    }
    (lib / "agents" / "local-agent.l5.yaml").write_text(yaml.safe_dump(manifest))
    return lib


@dataclass
class StubPeer:
    """In-process stub that mirrors RemoteL6Client's async API."""

    name: str
    list_agents_return: list[str] = field(default_factory=list)
    find_entity_return: dict[str, list[dict]] = field(default_factory=dict)
    list_recent_work_return: dict | None = None
    cross_agent_return: dict | None = None
    raise_on: set[str] = field(default_factory=set)
    calls: list[tuple[str, dict]] = field(default_factory=list)

    async def list_agents(self) -> list[str]:
        self.calls.append(("list_agents", {}))
        if "list_agents" in self.raise_on:
            raise RuntimeError("peer down")
        return list(self.list_agents_return)

    async def find_entity(
        self, name: str, access_level: str = "team", include_private: bool = False
    ) -> list[dict]:
        self.calls.append(
            ("find_entity", {"name": name, "access_level": access_level, "include_private": include_private})
        )
        if "find_entity" in self.raise_on:
            raise RuntimeError("peer down")
        return list(self.find_entity_return.get(name, []))

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
        self.calls.append(("list_recent_work", {"since": since, "limit": limit}))
        if "list_recent_work" in self.raise_on:
            raise RuntimeError("peer down")
        return self.list_recent_work_return or {"sessions": [], "has_more": False, "next_cursor": None}

    async def get_cross_agent_summary(
        self, project: str, access_level: str = "team", include_private: bool = False
    ) -> dict:
        self.calls.append(("get_cross_agent_summary", {"project": project}))
        if "get_cross_agent_summary" in self.raise_on:
            raise RuntimeError("peer down")
        return self.cross_agent_return or {
            "project": project,
            "agents": [],
            "recent_sessions": [],
            "entities": [],
        }


# ---------------------------------------------------------------------------
# list_agents_federated
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_agents_federated_unions_local_and_peer(populated_library: Path) -> None:
    peer = StubPeer(name="pc", list_agents_return=["codex", "claude-code"])
    store = L6Store(populated_library, peers=[peer])
    agents = await store.list_agents_federated()
    assert agents == ["claude-code", "codex", "local-agent"]


@pytest.mark.asyncio
async def test_list_agents_federated_drops_failed_peers(populated_library: Path) -> None:
    bad = StubPeer(name="bad", raise_on={"list_agents"})
    good = StubPeer(name="good", list_agents_return=["remote-agent"])
    store = L6Store(populated_library, peers=[bad, good])
    agents = await store.list_agents_federated()
    assert agents == ["local-agent", "remote-agent"]


@pytest.mark.asyncio
async def test_list_agents_federated_with_no_peers_returns_local(populated_library: Path) -> None:
    store = L6Store(populated_library, peers=None)
    assert await store.list_agents_federated() == ["local-agent"]


# ---------------------------------------------------------------------------
# find_entity_federated
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_find_entity_federated_merges_local_and_peer_match(populated_library: Path) -> None:
    peer = StubPeer(
        name="pc",
        find_entity_return={
            "Bourdon": [
                {
                    "name": "Bourdon",
                    "agents": ["codex"],
                    "types": ["project"],
                    "tags": ["federation"],
                    "summaries": {"codex": "codex's view of Bourdon"},
                }
            ]
        },
    )
    store = L6Store(populated_library, peers=[peer])
    matches = await store.find_entity_federated("Bourdon", access_level="team")
    assert len(matches) == 1
    m = matches[0]
    assert m.name == "Bourdon"
    # Local agent untagged, peer agent prefix-tagged with peer name.
    assert "local-agent" in m.agents
    assert "peer:pc:codex" in m.agents
    # Tags + summaries merged.
    assert "federation" in m.tags
    assert "peer:pc:codex" in m.summaries


@pytest.mark.asyncio
async def test_find_entity_federated_creates_match_when_only_peer_has_it(populated_library: Path) -> None:
    peer = StubPeer(
        name="pc",
        find_entity_return={
            "RemoteOnly": [
                {
                    "name": "RemoteOnly",
                    "agents": ["codex"],
                    "types": ["project"],
                }
            ]
        },
    )
    store = L6Store(populated_library, peers=[peer])
    matches = await store.find_entity_federated("RemoteOnly", access_level="team")
    assert len(matches) == 1
    assert matches[0].name == "RemoteOnly"
    assert "peer:pc:codex" in matches[0].agents


@pytest.mark.asyncio
async def test_find_entity_federated_failed_peer_returns_local_only(populated_library: Path) -> None:
    peer = StubPeer(name="pc", raise_on={"find_entity"})
    store = L6Store(populated_library, peers=[peer])
    matches = await store.find_entity_federated("Bourdon", access_level="team")
    assert len(matches) == 1
    assert matches[0].agents == ["local-agent"]


# ---------------------------------------------------------------------------
# list_recent_work_federated
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_recent_work_federated_merges_and_dedupes(populated_library: Path) -> None:
    peer = StubPeer(
        name="pc",
        list_recent_work_return={
            "sessions": [
                {
                    "agent": "codex",
                    "date": "2026-05-15",
                    "cwd": "/c/repos/bourdon",
                    "project_focus": ["Bourdon"],
                    "key_actions": ["from peer"],
                },
                # Same-day duplicate from a different cwd → distinct row
                {
                    "agent": "codex",
                    "date": "2026-05-15",
                    "cwd": "/other",
                    "project_focus": ["Bourdon"],
                    "key_actions": ["second"],
                },
            ],
            "has_more": False,
            "next_cursor": None,
        },
    )
    store = L6Store(populated_library, peers=[peer])
    # Use a wide `since` so the local 2026-05-16 entry isn't filtered out.
    # Local fixture sessions are visibility=team, so explicit access_level=team.
    since = datetime(2025, 1, 1, tzinfo=timezone.utc)
    page = await store.list_recent_work_federated(since=since, access_level="team")
    dates = sorted({s.date for s in page.sessions})
    assert "2026-05-16" in dates  # local
    assert "2026-05-15" in dates  # peer
    peer_rows = [s for s in page.sessions if s.agent.startswith("peer:pc:")]
    assert len(peer_rows) == 2


@pytest.mark.asyncio
async def test_list_recent_work_federated_no_peers_returns_local(populated_library: Path) -> None:
    store = L6Store(populated_library)
    since = datetime(2025, 1, 1, tzinfo=timezone.utc)
    page = await store.list_recent_work_federated(since=since, access_level="team")
    assert all(not s.agent.startswith("peer:") for s in page.sessions)


# ---------------------------------------------------------------------------
# get_cross_agent_summary_federated
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_cross_agent_summary_federated_unions_agents_and_entities(
    populated_library: Path,
) -> None:
    peer = StubPeer(
        name="pc",
        cross_agent_return={
            "project": "Bourdon",
            "agents": ["codex"],
            "recent_sessions": [
                {
                    "agent": "codex",
                    "date": "2026-05-15",
                    "cwd": "/c/repos/bourdon",
                    "project_focus": ["Bourdon"],
                }
            ],
            "entities": [
                {
                    "name": "Bourdon",
                    "agents": ["codex"],
                    "types": ["project"],
                    "summaries": {"codex": "codex view"},
                }
            ],
        },
    )
    store = L6Store(populated_library, peers=[peer])
    summary = await store.get_cross_agent_summary_federated("Bourdon", access_level="team")
    assert "local-agent" in summary.agents
    assert "peer:pc:codex" in summary.agents
    # The Bourdon entity appears with both local + peer-tagged agents.
    bourdon_entities = [e for e in summary.entities if e.name.lower() == "bourdon"]
    assert bourdon_entities, "expected at least one Bourdon entity match"
    e = bourdon_entities[0]
    assert "local-agent" in e.agents
    assert "peer:pc:codex" in e.agents


@pytest.mark.asyncio
async def test_get_cross_agent_summary_federated_failed_peer_isolated(populated_library: Path) -> None:
    bad = StubPeer(name="bad", raise_on={"get_cross_agent_summary"})
    store = L6Store(populated_library, peers=[bad])
    summary = await store.get_cross_agent_summary_federated("Bourdon", access_level="team")
    # Local content still there; no peer rows added.
    assert summary.agents == ["local-agent"]
    assert all(not a.startswith("peer:") for a in summary.agents)
