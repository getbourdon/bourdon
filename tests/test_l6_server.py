"""Tests for core.l6_server -- fastmcp wrapper around L6Store."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest
import yaml

from core import l6_server as server_module
from core.l6_store import L6Store

# -- Lazy-import behavior ------------------------------------------------------


def test_create_l6_server_raises_clear_error_when_fastmcp_missing(tmp_path, monkeypatch):
    """Instantiating the server without fastmcp must raise a helpful ImportError."""
    # Pretend fastmcp is not installed
    monkeypatch.setitem(sys.modules, "fastmcp", None)
    store = L6Store(tmp_path / "empty-lib")
    with pytest.raises(ImportError) as excinfo:
        server_module.create_l6_server(store)
    msg = str(excinfo.value).lower()
    assert "fastmcp" in msg
    assert "server" in msg  # should mention the pip extra


def test_importing_module_does_not_require_fastmcp(monkeypatch):
    """Just importing core.l6_server shouldn't try to import fastmcp."""
    # Remove any cached fastmcp import
    monkeypatch.setitem(sys.modules, "fastmcp", None)
    # Re-import the module -- should succeed even though fastmcp is None
    importlib.reload(server_module)
    # No exception raised means the test passes


# -- Server construction when fastmcp IS available ----------------------------


@pytest.fixture
def library(tmp_path):
    lib = tmp_path / "agent-library"
    agents_dir = lib / "agents"
    agents_dir.mkdir(parents=True)

    def write(agent_id: str, manifest: dict) -> Path:
        path = agents_dir / f"{agent_id}.l5.yaml"
        path.write_text(yaml.safe_dump(manifest), encoding="utf-8")
        return path

    return {"path": lib, "write": write}


def _require_fastmcp_or_skip():
    """Skip the test if fastmcp isn't installed in the test env."""
    try:
        import fastmcp  # noqa: F401
    except ImportError:
        pytest.skip("fastmcp not installed; skipping server-construction tests")


def test_create_l6_server_returns_fastmcp_instance(library):
    _require_fastmcp_or_skip()
    library["write"](
        "claude-code",
        {
            "spec_version": "0.1",
            "agent": {"id": "claude-code", "type": "code-assistant"},
            "last_updated": "2026-04-15T12:00:00+00:00",
            "known_entities": [{"name": "ILTT", "type": "project"}],
        },
    )
    store = L6Store(library["path"])
    server = server_module.create_l6_server(store)
    assert server is not None
    # FastMCP instances have a `name` attribute
    assert getattr(server, "name", None) == "bourdon-l6"


def test_server_name_override(library):
    _require_fastmcp_or_skip()
    store = L6Store(library["path"])
    server = server_module.create_l6_server(store, name="my-custom-server")
    assert getattr(server, "name", None) == "my-custom-server"


def test_prepare_recognition_context_from_store_returns_prompt_fragment(library):
    library["write"](
        "codex",
        {
            "spec_version": "0.1",
            "agent": {"id": "codex", "type": "code-assistant"},
            "last_updated": "2026-05-07T12:00:00+00:00",
            "known_entities": [
                {
                    "name": "Bourdon",
                    "type": "topic",
                    "summary": "Runtime recognition project.",
                    "visibility": "team",
                    "tags": ["codex-fallback-concept"],
                }
            ],
        },
    )
    store = L6Store(library["path"])

    report = server_module.prepare_recognition_context_from_store(
        store,
        "Can we keep working on Bourdon?",
        access_level="team",
    )

    assert report["recognition"] == "Oh -- Bourdon, the topic."
    assert report["matched_entities"] == [
        {
            "name": "Bourdon",
            "type": "topic",
            "source_agents": ["codex"],
        }
    ]
    assert "Bourdon recognition context" in report["prompt_context"]
    assert "Runtime recognition project." in report["prompt_context"]


def test_prepare_recognition_context_from_store_respects_public_visibility(library):
    library["write"](
        "codex",
        {
            "spec_version": "0.1",
            "agent": {"id": "codex", "type": "code-assistant"},
            "last_updated": "2026-05-07T12:00:00+00:00",
            "known_entities": [
                {
                    "name": "Private Anchor",
                    "type": "topic",
                    "summary": "Should stay hidden.",
                    "visibility": "team",
                }
            ],
        },
    )
    store = L6Store(library["path"])

    report = server_module.prepare_recognition_context_from_store(
        store,
        "Private Anchor please",
        access_level="public",
    )

    assert report["recognition"] == ""
    assert report["matched_entities"] == []
    assert report["prompt_context"] == ""


def test_mcp_compile_codex_turn_returns_same_schema(library):
    library["write"](
        "codex",
        {
            "spec_version": "0.1",
            "agent": {"id": "codex", "type": "code-assistant"},
            "last_updated": "2026-05-27T12:00:00+00:00",
            "known_entities": [
                {
                    "name": "Bourdon",
                    "type": "project",
                    "summary": "Turn compiler context.",
                    "visibility": "team",
                }
            ],
        },
    )
    store = L6Store(library["path"])

    report = server_module.compile_codex_turn_from_store(
        store,
        "Bourdon recognition",
        access_level="team",
    )

    assert report["schema_version"] == "codex-turn-brief/v1"
    assert report["health"]["strategy"] == "turn_compiled"
    assert report["items"][0]["name"] == "Bourdon"
    assert "Bourdon turn recognition brief" in report["delivery"]["explicit_text"]


async def test_get_deeper_context_for_prompt_never_raises(monkeypatch):
    async def broken_query_l2(prompt):
        raise RuntimeError("retriever unavailable")

    monkeypatch.setattr(server_module, "query_l2", broken_query_l2)

    report = await server_module.get_deeper_context_for_prompt("Bourdon")

    assert report["context"] == ""
    assert report["context_chars"] == 0


async def test_get_deeper_context_for_prompt_returns_l2_text(monkeypatch):
    async def fake_query_l2(prompt):
        return f"Deeper context for {prompt}."

    monkeypatch.setattr(server_module, "query_l2", fake_query_l2)

    report = await server_module.get_deeper_context_for_prompt("Bourdon")

    assert report["context"] == "Deeper context for Bourdon."
    assert report["context_chars"] == len("Deeper context for Bourdon.")


# -- export_agents tool (LOCAL-ONLY, no peer fan-out) --------------------------


class _ExplodingPeer:
    """Peer whose every async method raises -- proves export_agents never calls it."""

    name = "mac"

    async def export_agents(self):  # pragma: no cover - must never be awaited
        raise AssertionError("export_agents tool must NOT fan out to peers")


def test_export_agents_tool_is_local_only(library, monkeypatch):
    _require_fastmcp_or_skip()
    import asyncio

    monkeypatch.setenv("BOURDON_LOCAL_NAME", "pc")
    library["write"](
        "claude-code",
        {
            "spec_version": "0.1",
            "agent": {"id": "claude-code", "type": "code-assistant"},
            "last_updated": "2026-06-01T12:00:00+00:00",
            "capabilities": ["mcp"],
            "recent_sessions": [],
        },
    )
    store = L6Store(library["path"], peers=[_ExplodingPeer()])
    server = server_module.create_l6_server(store)

    async def _call():
        tool = await server.get_tool("export_agents")
        return tool.fn()

    report = asyncio.run(_call())
    assert report["schema"] == "bourdon.agents/v1"
    assert report["machine"] == "pc"
    # Only THIS machine's agents -- no peer fan-out, no "sources" key.
    assert [a["id"] for a in report["agents"]] == ["claude-code"]
    assert report["agents"][0]["source"] == "pc"
    assert report["agents"][0]["source_kind"] == "local"
    assert "sources" not in report
# -- Peer loading (load_peers) -------------------------------------------------


def test_load_peers_empty_when_no_config_and_no_inline(tmp_path):
    """No config file + no inline URLs -> empty peer list (federation off)."""
    missing = tmp_path / "nope.yaml"
    assert server_module.load_peers(missing, []) == []


def test_load_peers_from_config_file(tmp_path):
    """A peers.yaml is parsed into RemoteL6Client objects with token_env honored."""
    cfg = tmp_path / "peers.yaml"
    cfg.write_text(
        yaml.safe_dump(
            {
                "peers": [
                    {"name": "pc", "url": "http://pc.tailnet:7500", "token_env": "TOK_PC"},
                    {"name": "mac", "url": "http://mac.tailnet:7500"},
                ]
            }
        ),
        encoding="utf-8",
    )
    peers = server_module.load_peers(cfg, [])
    assert [p.name for p in peers] == ["pc", "mac"]
    # RemoteL6Client.__post_init__ normalizes the URL with a /mcp suffix.
    assert peers[0].url == "http://pc.tailnet:7500/mcp"
    assert peers[0].token_env == "TOK_PC"
    # Missing token_env defaults to the shared env var.
    assert peers[1].token_env == "BOURDON_PEER_TOKEN"


def test_load_peers_inline_urls(tmp_path):
    """Inline --peer URLs become peers named after the URL."""
    missing = tmp_path / "absent.yaml"
    peers = server_module.load_peers(missing, ["http://localhost:7501"])
    assert len(peers) == 1
    assert peers[0].name == "http://localhost:7501"
    assert peers[0].url == "http://localhost:7501/mcp"


def test_load_peers_dedupes_config_and_inline(tmp_path):
    """The same URL in both the config file and an inline flag yields one peer."""
    cfg = tmp_path / "peers.yaml"
    cfg.write_text(
        yaml.safe_dump({"peers": [{"name": "pc", "url": "http://dup:7500"}]}),
        encoding="utf-8",
    )
    peers = server_module.load_peers(cfg, ["http://dup:7500"])
    assert len(peers) == 1
    assert peers[0].name == "pc"  # config entry wins; inline dup is skipped


def test_load_peers_malformed_config_degrades_to_empty(tmp_path):
    """A malformed config never raises -- it degrades to no peers."""
    cfg = tmp_path / "peers.yaml"
    cfg.write_text("peers:\n  - not-a-mapping\n  - url: ''\n", encoding="utf-8")
    assert server_module.load_peers(cfg, []) == []


# -- Shared run path (run_l6_server) -------------------------------------------


class _RecordingServer:
    """Stand-in fastmcp server that records how .run() was invoked."""

    def __init__(self, fail_http_typeerror: bool = False) -> None:
        self.calls: list[dict] = []
        self._fail_http_typeerror = fail_http_typeerror

    def run(self, *args, **kwargs):  # noqa: ANN002, ANN003
        if kwargs.get("transport") == "http" and self._fail_http_typeerror:
            raise TypeError("this fastmcp version does not accept transport=")
        self.calls.append(dict(kwargs))


def test_run_l6_server_stdio_uses_default_run():
    server = _RecordingServer()
    server_module.run_l6_server(server, transport="stdio")
    assert server.calls == [{}]


def test_run_l6_server_http_unauthenticated_passes_transport():
    server = _RecordingServer()
    server_module.run_l6_server(
        server, transport="http", port=7501, allow_unauthenticated=True
    )
    assert server.calls == [{"transport": "http", "port": 7501}]


def test_run_l6_server_http_unauth_falls_back_when_transport_kwarg_unsupported():
    """Old fastmcp without transport= kwarg -> fall back to stdio run()."""
    server = _RecordingServer(fail_http_typeerror=True)
    server_module.run_l6_server(
        server, transport="http", port=7502, allow_unauthenticated=True
    )
    # The http attempt raised TypeError; the fallback bare run() is recorded.
    assert server.calls == [{}]
