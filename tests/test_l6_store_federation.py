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
    # Source-attributed agent export (desktop tray, multi-machine).
    export_agents_return: dict | None = None
    # Phase 1.7 surfaces.
    recognition_return: dict | None = None
    recognition_delay: float = 0.0  # seconds — sleep before returning
    recognition_timeout: float = 0.2  # matches RemoteL6Client default
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

    async def export_agents(self) -> dict | None:
        self.calls.append(("export_agents", {}))
        if "export_agents" in self.raise_on:
            raise RuntimeError("peer down")
        return self.export_agents_return

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

    async def prepare_recognition_context(
        self,
        prompt: str,
        access_level: str = "team",
        include_private: bool = False,
    ) -> dict:
        self.calls.append(("prepare_recognition_context", {"prompt": prompt}))
        if self.recognition_delay:
            import asyncio as _asyncio
            await _asyncio.sleep(self.recognition_delay)
        if "prepare_recognition_context" in self.raise_on:
            raise RuntimeError("peer down")
        return self.recognition_return or {
            "prompt": prompt,
            "recognition": "I have no idea what that is.",
            "matched_entities": [],
            "recognition_latency_us": 0.0,
            "prompt_context": "",
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


# ---------------------------------------------------------------------------
# prepare_recognition_context_federated (Phase 1.7)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_prepare_recognition_federated_merges_peer_entities(populated_library: Path) -> None:
    from core.l6_server import prepare_recognition_context_federated

    peer = StubPeer(
        name="pc",
        recognition_return={
            "prompt": "Do you remember what Bourdon is?",
            "recognition": "Yes — Bourdon is your federation runtime.",
            "matched_entities": [
                {"name": "Bourdon", "type": "project", "source_agents": ["codex"]},
                {"name": "RemoteOnly", "type": "project", "source_agents": ["codex"]},
            ],
            "recognition_latency_us": 1.5,
            "prompt_context": "Bourdon recognized via codex.",
        },
    )
    store = L6Store(populated_library, peers=[peer])
    payload = await prepare_recognition_context_federated(
        store, "Do you remember what Bourdon is?", access_level="team"
    )

    by_name = {e["name"]: e for e in payload["matched_entities"]}
    # The local Bourdon entity exists; peer "codex" appended with peer tag.
    assert "Bourdon" in by_name
    assert "peer:pc:codex" in by_name["Bourdon"]["source_agents"]
    # Peer-only entity surfaces with peer tag.
    assert "RemoteOnly" in by_name
    assert by_name["RemoteOnly"]["source_agents"] == ["peer:pc:codex"]
    # Peer recognition line appears in prompt_context.
    assert "[peer:pc]" in payload["prompt_context"]
    assert payload["peers_queried"] == 1
    assert payload["peers_responded"] == 1
    assert payload["peers_timed_out"] == 0
    assert "pc" in payload["peer_latencies_us"]


@pytest.mark.asyncio
async def test_prepare_recognition_federated_peer_timeout_drops_silently(populated_library: Path) -> None:
    from core.l6_server import prepare_recognition_context_federated

    slow = StubPeer(
        name="slow",
        recognition_delay=0.5,  # well past the 50 ms budget below
        recognition_return={
            "prompt": "Q",
            "recognition": "Should never appear",
            "matched_entities": [{"name": "ShouldNeverAppear", "type": "project", "source_agents": ["codex"]}],
        },
    )
    store = L6Store(populated_library, peers=[slow])
    payload = await prepare_recognition_context_federated(
        store,
        "Do you remember what Bourdon is?",
        access_level="team",
        timeout_per_peer=0.05,  # 50 ms — slow peer can't make it
    )

    # Local entities still present.
    names = {e["name"] for e in payload["matched_entities"]}
    assert "Bourdon" in names
    # Slow peer's entity does NOT leak in.
    assert "ShouldNeverAppear" not in names
    assert payload["peers_queried"] == 1
    assert payload["peers_responded"] == 0
    assert payload["peers_timed_out"] == 1
    # Latency for a timed-out peer is reported as None.
    assert payload["peer_latencies_us"]["slow"] is None


@pytest.mark.asyncio
async def test_prepare_recognition_federated_peer_failure_isolated(populated_library: Path) -> None:
    from core.l6_server import prepare_recognition_context_federated

    bad = StubPeer(name="bad", raise_on={"prepare_recognition_context"})
    store = L6Store(populated_library, peers=[bad])
    payload = await prepare_recognition_context_federated(
        store, "Do you remember what Bourdon is?", access_level="team"
    )
    # Local content still there; peer reported as not responding.
    names = {e["name"] for e in payload["matched_entities"]}
    assert "Bourdon" in names
    assert payload["peers_queried"] == 1
    assert payload["peers_responded"] == 0
    assert payload["peer_latencies_us"]["bad"] is None


@pytest.mark.asyncio
async def test_prepare_recognition_federated_no_peers_matches_sync_shape(populated_library: Path) -> None:
    from core.l6_server import prepare_recognition_context_federated, prepare_recognition_context_from_store

    store = L6Store(populated_library, peers=None)
    sync_payload = prepare_recognition_context_from_store(
        store, "Do you remember what Bourdon is?", access_level="team"
    )
    fed_payload = await prepare_recognition_context_federated(
        store, "Do you remember what Bourdon is?", access_level="team"
    )

    # The federated path with no peers returns the same payload modulo the
    # new peer-metadata keys.
    for k, v in sync_payload.items():
        if k == "recognition_latency_us":
            continue  # measured independently, will differ
        assert fed_payload[k] == v
    assert fed_payload["peers_queried"] == 0
    assert fed_payload["peers_responded"] == 0
    assert fed_payload["peers_timed_out"] == 0
    assert fed_payload["peer_latencies_us"] == {}


# ---------------------------------------------------------------------------
# export_agents_federated (desktop tray, multi-machine)
# ---------------------------------------------------------------------------


def _local_claude_code(lib: Path) -> None:
    """Add a local `claude-code` manifest so same-named dedupe can be tested."""
    manifest = {
        "spec_version": "0.1",
        "agent": {"id": "claude-code", "type": "code-assistant"},
        "last_updated": "2026-06-01T00:00:00+00:00",
        "capabilities": ["mcp"],
        "recent_sessions": [
            {"date": "2026-06-01", "project_focus": ["Bourdon"], "visibility": "team"}
        ],
    }
    (lib / "agents" / "claude-code.l5.yaml").write_text(yaml.safe_dump(manifest))


@pytest.fixture(autouse=True)
def _pin_local_name(monkeypatch):
    monkeypatch.setenv("BOURDON_LOCAL_NAME", "pc")


@pytest.mark.asyncio
async def test_export_agents_federated_no_peers_is_local_only(
    populated_library: Path,
) -> None:
    store = L6Store(populated_library, peers=None)
    report = await store.export_agents_federated()
    assert report["schema"] == "bourdon.agents/v1"
    assert [a["id"] for a in report["agents"]] == ["local-agent"]
    assert report["agents"][0]["source"] == "pc"
    assert report["agents"][0]["source_kind"] == "local"
    assert report["sources"] == [
        {"name": "pc", "kind": "local", "reachable": True, "agent_count": 1}
    ]


@pytest.mark.asyncio
async def test_export_agents_federated_merges_peer_and_dedupes_by_source_id(
    populated_library: Path,
) -> None:
    _local_claude_code(populated_library)
    # Peer self-reports as "mac" AND tries to spoof a "pc" source — we must
    # re-tag with the peer's configured name and never trust its self-report.
    peer = StubPeer(
        name="mac",
        export_agents_return={
            "schema": "bourdon.agents/v1",
            "machine": "evil-spoof",
            "agents": [
                {
                    "id": "claude-code",
                    "source": "pc",  # spoofed — must be overwritten to "mac"
                    "source_kind": "local",
                    "last_updated": "2026-05-30T00:00:00+00:00",
                },
            ],
        },
    )
    store = L6Store(populated_library, peers=[peer])
    report = await store.export_agents_federated()

    rows = {(a["id"], a["source"], a["source_kind"]) for a in report["agents"]}
    # Both claude-code rows survive — one local (pc), one peer (mac).
    assert ("claude-code", "pc", "local") in rows
    assert ("claude-code", "mac", "peer") in rows
    # The spoofed "source": "pc" on the peer row was overwritten to "mac".
    assert ("claude-code", "pc", "peer") not in rows

    by_name = {s["name"]: s for s in report["sources"]}
    assert by_name["pc"] == {
        "name": "pc",
        "kind": "local",
        "reachable": True,
        "agent_count": 2,
    }
    assert by_name["mac"] == {
        "name": "mac",
        "kind": "peer",
        "reachable": True,
        "agent_count": 1,
    }


@pytest.mark.asyncio
async def test_export_agents_federated_peer_raises_is_unreachable(
    populated_library: Path,
) -> None:
    peer = StubPeer(name="mac", raise_on={"export_agents"})
    store = L6Store(populated_library, peers=[peer])
    report = await store.export_agents_federated()

    # Local agents still returned.
    assert [a["id"] for a in report["agents"]] == ["local-agent"]
    by_name = {s["name"]: s for s in report["sources"]}
    assert by_name["mac"] == {
        "name": "mac",
        "kind": "peer",
        "reachable": False,
        "agent_count": 0,
    }


@pytest.mark.asyncio
async def test_export_agents_federated_peer_returns_none_is_unreachable(
    populated_library: Path,
) -> None:
    # None mirrors an un-upgraded peer with no export_agents tool.
    peer = StubPeer(name="mac", export_agents_return=None)
    store = L6Store(populated_library, peers=[peer])
    report = await store.export_agents_federated()

    assert [a["id"] for a in report["agents"]] == ["local-agent"]
    by_name = {s["name"]: s for s in report["sources"]}
    assert by_name["mac"]["reachable"] is False
    assert by_name["mac"]["agent_count"] == 0


@pytest.mark.asyncio
async def test_export_agents_federated_peer_empty_agents_is_unreachable(
    populated_library: Path,
) -> None:
    peer = StubPeer(
        name="mac",
        export_agents_return={"schema": "bourdon.agents/v1", "agents": []},
    )
    store = L6Store(populated_library, peers=[peer])
    report = await store.export_agents_federated()
    by_name = {s["name"]: s for s in report["sources"]}
    assert by_name["mac"]["reachable"] is False
    assert by_name["mac"]["agent_count"] == 0


@pytest.mark.asyncio
async def test_export_agents_federated_local_name_override(
    populated_library: Path,
) -> None:
    store = L6Store(populated_library, peers=None)
    report = await store.export_agents_federated(local_name="my-laptop")
    assert report["agents"][0]["source"] == "my-laptop"
    assert report["sources"][0]["name"] == "my-laptop"
