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


# -- Depth-1 federation: peer-originated calls must not fan out (#139) ---------


class _RecordingPeer:
    """Fake peer that records every query and returns benign canned results."""

    name = "mac"
    recognition_timeout = 0.2

    def __init__(self):
        self.calls: list[str] = []

    async def list_agents(self):
        self.calls.append("list_agents")
        return ["peer-agent"]

    async def find_entity(self, name, access_level="team", include_private=False):
        self.calls.append("find_entity")
        return []

    async def list_recent_work(self, **kwargs):
        self.calls.append("list_recent_work")
        return {"sessions": [], "next_cursor": None, "has_more": False}

    async def get_cross_agent_summary(
        self, project, access_level="team", include_private=False
    ):
        self.calls.append("get_cross_agent_summary")
        return {"project": project, "agents": [], "recent_sessions": [], "entities": []}

    async def prepare_recognition_context(
        self, prompt, access_level="team", include_private=False
    ):
        self.calls.append("prepare_recognition_context")
        return {"matched_entities": []}


def _server_with_recording_peer(library):
    library["write"](
        "claude-code",
        {
            "spec_version": "0.1",
            "agent": {"id": "claude-code", "type": "code-assistant"},
            "last_updated": "2026-06-01T12:00:00+00:00",
            "known_entities": [{"name": "Bourdon", "type": "project"}],
            "recent_sessions": [],
        },
    )
    peer = _RecordingPeer()
    store = L6Store(library["path"], peers=[peer])
    return server_module.create_l6_server(store), peer


@pytest.mark.parametrize(
    ("tool_name", "args"),
    [
        ("list_agents", {}),
        ("find_entity", {"name": "Bourdon"}),
        ("list_recent_work", {}),
        ("get_cross_agent_summary", {"project": "Bourdon"}),
        ("prepare_recognition_context", {"prompt": "What is Bourdon?"}),
    ],
)
def test_federation_hop_1_answers_local_only(library, tool_name, args):
    """A peer-originated call (federation_hop=1) must never re-fan out.

    Without this guard, bidirectional peering (A lists B, B lists A) recurses
    until fd exhaustion — issue #139.
    """
    _require_fastmcp_or_skip()
    import asyncio

    server, peer = _server_with_recording_peer(library)

    async def _call():
        tool = await server.get_tool(tool_name)
        return await tool.fn(federation_hop=1, **args)

    result = asyncio.run(_call())
    assert peer.calls == [], f"{tool_name} fanned out to peers despite federation_hop=1"
    assert "error" not in result


def test_federation_hop_0_still_fans_out(library):
    """Default client calls (federation_hop=0) keep the federated merge."""
    _require_fastmcp_or_skip()
    import asyncio

    server, peer = _server_with_recording_peer(library)

    async def _call():
        tool = await server.get_tool("list_agents")
        return await tool.fn()

    result = asyncio.run(_call())
    assert "list_agents" in peer.calls
    assert "peer-agent" in result["agents"]
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

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.http_app_calls: list = []

    def run(self, *args, **kwargs):  # noqa: ANN002, ANN003
        self.calls.append(dict(kwargs))

    def http_app(self, middleware=None, stateless_http=None):  # noqa: ANN001
        self.http_app_calls.append(middleware)
        return ("ASGI_APP", middleware)


def test_run_l6_server_stdio_uses_default_run():
    server = _RecordingServer()
    server_module.run_l6_server(server, transport="stdio")
    assert server.calls == [{}]


def test_run_l6_server_http_default_binds_loopback_only(monkeypatch):
    """v0.9.0 contract (spec D8): the default HTTP bind is 127.0.0.1.
    Cross-host / Tailnet serving requires an explicit --host 0.0.0.0 AND
    auth configured."""
    pytest.importorskip("uvicorn")  # optional [server] extra; skip if absent in CI
    captured: dict = {}
    monkeypatch.setattr(
        "uvicorn.run",
        lambda app, host=None, port=None, log_level=None: captured.update(
            app=app, host=host, port=port
        ),
    )
    server = _RecordingServer()
    server_module.run_l6_server(
        server, transport="http", port=7501, allow_unauthenticated=True
    )
    assert captured["host"] == "127.0.0.1"
    assert captured["port"] == 7501
    # Unauth path wraps the operator-identity middleware (never bare).
    assert server.http_app_calls and server.http_app_calls[0] is not None
    assert server.calls == []  # never falls back to server.run(transport=http)


def test_run_l6_server_http_nonloopback_without_auth_refuses_to_start(monkeypatch):
    """v0.9.0 negative test: 0.0.0.0 with no auth configured must exit
    non-zero at startup — not serve and 503 per request."""
    pytest.importorskip("uvicorn")
    monkeypatch.delenv("BOURDON_PEER_TOKEN_SERVER", raising=False)
    called: dict = {}
    monkeypatch.setattr(
        "uvicorn.run", lambda *a, **k: called.update(ran=True)
    )
    server = _RecordingServer()
    with pytest.raises(SystemExit):
        server_module.run_l6_server(
            server, transport="http", host="0.0.0.0", allow_unauthenticated=False
        )
    assert "ran" not in called


def test_run_l6_server_http_nonloopback_unauthenticated_refuses_to_start(monkeypatch):
    """--allow-unauthenticated is loopback-only in v0.9.0; combined with a
    non-loopback bind the server refuses to start even if auth env is set."""
    pytest.importorskip("uvicorn")
    monkeypatch.setenv("BOURDON_PEER_TOKEN_SERVER", "shh")
    called: dict = {}
    monkeypatch.setattr(
        "uvicorn.run", lambda *a, **k: called.update(ran=True)
    )
    server = _RecordingServer()
    with pytest.raises(SystemExit):
        server_module.run_l6_server(
            server, transport="http", host="0.0.0.0", allow_unauthenticated=True
        )
    assert "ran" not in called


def test_run_l6_server_http_nonloopback_with_auth_binds_all_interfaces(monkeypatch):
    """Tailnet serving still works: explicit 0.0.0.0 + auth configured."""
    pytest.importorskip("uvicorn")
    monkeypatch.setenv("BOURDON_PEER_TOKEN_SERVER", "shh-its-a-secret")
    captured: dict = {}
    monkeypatch.setattr(
        "uvicorn.run",
        lambda app, host=None, port=None, log_level=None: captured.update(
            host=host, port=port
        ),
    )
    server = _RecordingServer()
    server_module.run_l6_server(
        server, transport="http", port=7502, host="0.0.0.0",
        allow_unauthenticated=False,
    )
    assert captured["host"] == "0.0.0.0"
    assert captured["port"] == 7502
    # Authed path passes a non-empty middleware list to http_app().
    assert server.http_app_calls and server.http_app_calls[0] is not None


def test_run_l6_server_http_respects_explicit_host(monkeypatch):
    pytest.importorskip("uvicorn")  # optional [server] extra; skip if absent in CI
    captured: dict = {}
    monkeypatch.setattr(
        "uvicorn.run",
        lambda app, host=None, port=None, log_level=None: captured.update(host=host),
    )
    server = _RecordingServer()
    server_module.run_l6_server(
        server,
        transport="http",
        port=7503,
        host="127.0.0.1",
        allow_unauthenticated=True,
    )
    assert captured["host"] == "127.0.0.1"
