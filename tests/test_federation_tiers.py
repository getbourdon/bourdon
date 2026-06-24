"""Trust-tier enforcement tests (v0.9.0, spec/SPEC_v0.9.0.md R3).

Server-side enforcement: quarantined callers get an allowlisted read surface
filtered to granted namespaces, staged writes, and structured denials —
verified at the tool layer (contextvar identity) and end-to-end over the
HTTP transport (Bearer token -> middleware -> handler).
"""

from __future__ import annotations

import asyncio
import json

import pytest
import yaml

from core.federation_audit import FederationAudit
from core.federation_registry import (
    AgentIdentity,
    FederationRegistry,
    reset_caller,
    set_caller,
)
from core.l6_store import L6Store

pytest.importorskip("fastmcp")

from core import l6_server as server_module  # noqa: E402


@pytest.fixture
def fed(tmp_path):
    """Library with two agents, a quarantined member granted ONE namespace."""
    agents = tmp_path / "lib" / "agents"
    agents.mkdir(parents=True)
    (agents / "claude-code.l5.yaml").write_text(
        yaml.safe_dump(
            {
                "spec_version": "0.1",
                "agent": {"id": "claude-code", "type": "code-assistant"},
                "last_updated": "2026-06-01T12:00:00+00:00",
                "known_entities": [
                    {"name": "Bourdon", "type": "project", "summary": "granted side"}
                ],
                "recent_sessions": [
                    {"date": "2026-06-08", "cwd": "/x", "project_focus": ["Bourdon"]}
                ],
            }
        ),
        encoding="utf-8",
    )
    (agents / "codex.l5.yaml").write_text(
        yaml.safe_dump(
            {
                "spec_version": "0.1",
                "agent": {"id": "codex", "type": "code-assistant"},
                "last_updated": "2026-06-01T12:00:00+00:00",
                "known_entities": [
                    {"name": "SecretProj", "type": "project", "summary": "ungranted"},
                    {"name": "Bourdon", "type": "project", "summary": "codex view"},
                ],
                "recent_sessions": [
                    {"date": "2026-06-08", "cwd": "/y", "project_focus": ["SecretProj"]}
                ],
            }
        ),
        encoding="utf-8",
    )
    registry = FederationRegistry(tmp_path / "federation.yaml")
    token = registry.add_agent("openclaw", tier="quarantined", grants=["claude-code"])
    audit = FederationAudit(tmp_path / "audit.jsonl")
    store = L6Store(tmp_path / "lib")
    server = server_module.create_l6_server(store, registry=registry, audit=audit)
    return {
        "server": server,
        "store": store,
        "registry": registry,
        "audit": audit,
        "token": token,
        "library": tmp_path / "lib",
        "quarantined": AgentIdentity(
            agent_id="openclaw", tier="quarantined", grants=("claude-code",)
        ),
    }


def _call(server, name, identity=None, /, **kwargs):
    """Invoke one MCP tool fn under an optional caller identity."""

    async def _inner():
        tool = await server.get_tool(name)
        result = tool.fn(**kwargs)
        if asyncio.iscoroutine(result):
            result = await result
        return result

    ctx = set_caller(identity) if identity is not None else None
    try:
        return asyncio.run(_inner())
    finally:
        if ctx is not None:
            reset_caller(ctx)


# -- Operator / trusted behavior is unchanged (migration acceptance) -----------


def test_operator_default_sees_everything(fed):
    result = _call(fed["server"], "find_entity", name="Bourdon")
    agents = {a for m in result["matches"] for a in m["agents"]}
    assert agents == {"claude-code", "codex"}
    listed = _call(fed["server"], "list_agents")
    assert set(listed["agents"]) == {"claude-code", "codex"}


# -- Quarantined reads: deny-by-default on non-granted namespaces ---------------


def test_quarantined_query_non_granted_namespace_denied_and_audited(fed):
    result = _call(
        fed["server"], "query_agent_memory", fed["quarantined"],
        agent="codex", topic="SecretProj",
    )
    assert result["error"] == "access denied"
    assert result["tier"] == "quarantined"
    denials = fed["audit"].entries(agent="openclaw", denials_only=True)
    assert denials and denials[-1]["namespace"] == "codex"


def test_quarantined_query_granted_namespace_allowed(fed):
    result = _call(
        fed["server"], "query_agent_memory", fed["quarantined"],
        agent="claude-code", topic="Bourdon",
    )
    assert "error" not in result
    assert result["matches"]


def test_quarantined_find_entity_filters_to_granted_agents(fed):
    result = _call(fed["server"], "find_entity", fed["quarantined"], name="Bourdon")
    agents = {a for m in result["matches"] for a in m["agents"]}
    assert agents == {"claude-code"}
    summaries = {
        agent for m in result["matches"] for agent in m["summaries"]
    }
    assert "codex" not in summaries
    # Entity known ONLY by a non-granted agent disappears entirely.
    result = _call(fed["server"], "find_entity", fed["quarantined"], name="SecretProj")
    assert result["matches"] == []


def test_quarantined_list_recent_work_filters_sessions(fed):
    result = _call(
        fed["server"], "list_recent_work", fed["quarantined"], since="2026-06-01"
    )
    agents = {s["agent"] for s in result["sessions"]}
    assert agents == {"claude-code"}
    # Asking explicitly for a non-granted agent's sessions is a denial.
    result = _call(
        fed["server"], "list_recent_work", fed["quarantined"],
        since="2026-06-01", agent="codex",
    )
    assert result["error"] == "access denied"
    assert result["sessions"] == []


def test_quarantined_list_agents_and_export_agents_filtered(fed):
    listed = _call(fed["server"], "list_agents", fed["quarantined"])
    assert listed["agents"] == ["claude-code"]
    envelope = _call(fed["server"], "export_agents", fed["quarantined"])
    assert [a["id"] for a in envelope["agents"]] == ["claude-code"]


def test_quarantined_aggregate_tools_denied(fed):
    for tool_name, kwargs in (
        ("get_cross_agent_summary", {"project": "Bourdon"}),
        ("prepare_recognition_context", {"prompt": "what about Bourdon"}),
        ("get_deeper_context", {"prompt": "what about Bourdon"}),
        ("compile_codex_turn", {"prompt": "what about Bourdon"}),
    ):
        result = _call(fed["server"], tool_name, fed["quarantined"], **kwargs)
        assert result["error"] == "access denied", tool_name
    ops = {e["op"] for e in fed["audit"].entries(denials_only=True)}
    assert {
        "get_cross_agent_summary",
        "prepare_recognition_context",
        "get_deeper_context",
        "compile_codex_turn",
    } <= ops


# -- Quarantined writes: staged, own namespace only -----------------------------


def test_quarantined_write_stages_and_stays_invisible_until_promoted(fed):
    result = _call(
        fed["server"], "commit_to_federation", fed["quarantined"],
        agent_id="openclaw", agent_type="other",
        entities=[{"name": "ClawFinding", "type": "topic", "summary": "from openclaw"}],
        sessions=[{"date": "2026-06-09"}],
    )
    assert result["staged"] is True
    staged_path = fed["library"] / "staging" / "openclaw" / "openclaw.l5.yaml"
    assert staged_path.exists()

    # Invisible to every read until promoted — including the operator's.
    seen = _call(fed["server"], "find_entity", name="ClawFinding")
    assert seen["matches"] == []
    assert "openclaw" not in fed["store"].list_agents()

    from core.federation_staging import list_staged, promote

    staged = list_staged(fed["library"])
    assert [s.agent_id for s in staged] == ["openclaw"]
    promote(fed["library"], "openclaw")
    fed["store"].reload_all()
    assert not staged_path.exists()
    seen = _call(fed["server"], "find_entity", name="ClawFinding")
    assert {a for m in seen["matches"] for a in m["agents"]} == {"openclaw"}


def test_quarantined_write_foreign_namespace_denied(fed):
    """A poisoned member must not stage content under another agent's id."""
    result = _call(
        fed["server"], "commit_to_federation", fed["quarantined"],
        agent_id="claude-code", agent_type="code-assistant",
        entities=[{"name": "Poisoned", "summary": "injected"}],
    )
    assert result["error"] == "access denied"
    assert not (fed["library"] / "staging").exists()


def test_quarantined_repeat_writes_accumulate_in_one_staged_manifest(fed):
    for name in ("A", "B", "A"):
        _call(
            fed["server"], "commit_to_federation", fed["quarantined"],
            agent_id="openclaw", agent_type="other",
            entities=[{"name": name, "summary": f"s-{name}"}],
        )
    data = yaml.safe_load(
        (fed["library"] / "staging" / "openclaw" / "openclaw.l5.yaml").read_text(
            encoding="utf-8"
        )
    )
    names = sorted(e["name"] for e in data["known_entities"])
    assert names == ["A", "B"]  # deduped by name


def test_trusted_write_unchanged(fed):
    result = _call(
        fed["server"], "commit_to_federation",
        agent_id="clyde", agent_type="other",
        entities=[{"name": "TrustedThing", "summary": "ok"}],
    )
    assert "staged" not in result
    assert result["entities_added"] == 1
    assert "clyde" in fed["store"].list_agents()


# -- Resources -------------------------------------------------------------------


def test_quarantined_resource_access_filtered(fed):
    async def _read(agent_id):
        template = await fed["server"].get_resource_template(
            "agent-library://agents/{agent_id}/memory"
        )
        return template.fn(agent_id=agent_id)

    ctx = set_caller(fed["quarantined"])
    try:
        denied = asyncio.run(_read("codex"))
        assert denied["error"] == "access denied"
        allowed = asyncio.run(_read("claude-code"))
        assert allowed["agent"]["id"] == "claude-code"
    finally:
        reset_caller(ctx)


# -- Full-trail audit acceptance ---------------------------------------------------


def test_scripted_session_leaves_complete_audit_trail(fed):
    _call(fed["server"], "find_entity", fed["quarantined"], name="Bourdon")
    _call(fed["server"], "query_agent_memory", fed["quarantined"],
          agent="codex", topic="x")
    _call(fed["server"], "commit_to_federation", fed["quarantined"],
          agent_id="openclaw", agent_type="other",
          entities=[{"name": "E", "summary": "s"}])
    entries = fed["audit"].entries(agent="openclaw")
    ops = [(e["op"], e["decision"]) for e in entries]
    assert ("find_entity", "allow") in ops
    assert ("query_agent_memory", "deny") in ops
    assert ("commit_to_federation", "allow") in ops


# -- End-to-end over HTTP: token -> middleware -> handler --------------------------


async def _post(client, payload, token=None):
    headers = {
        "content-type": "application/json",
        "accept": "application/json, text/event-stream",
    }
    if token:
        headers["authorization"] = f"Bearer {token}"
    return await client.post("/mcp", json=payload, headers=headers)


def _rpc(name, args, id=1):
    return {
        "jsonrpc": "2.0",
        "id": id,
        "method": "tools/call",
        "params": {"name": name, "arguments": args},
    }


def test_http_end_to_end_auth_and_tier_enforcement(fed, monkeypatch):
    """The release-blocking integration: anonymous 401s before any handler,
    a quarantined Bearer token resolves to a filtered view, denials audit."""
    httpx = pytest.importorskip("httpx")
    from starlette.middleware import Middleware

    monkeypatch.delenv("BOURDON_PEER_TOKEN_SERVER", raising=False)
    app = fed["server"].http_app(
        middleware=[Middleware(server_module._build_auth_middleware(fed["registry"]))],
        stateless_http=True,
        json_response=True,
    )

    async def _scenario():
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://test"
            ) as client:
                # 1. No token -> 401, no handler ran.
                r = await _post(client, _rpc("list_agents", {}))
                assert r.status_code == 401

                # 2. Invalid token -> 401.
                r = await _post(client, _rpc("list_agents", {}), token="bdn_wrong")
                assert r.status_code == 401

                # 3. Valid quarantined token -> filtered to granted namespace.
                r = await _post(client, _rpc("list_agents", {}), token=fed["token"])
                assert r.status_code == 200
                body = r.json()["result"]["structuredContent"]
                assert body["agents"] == ["claude-code"]

                # 4. Non-granted read denied THROUGH the transport.
                r = await _post(
                    client,
                    _rpc("query_agent_memory", {"agent": "codex", "topic": "x"}),
                    token=fed["token"],
                )
                body = r.json()["result"]["structuredContent"]
                assert body["error"] == "access denied"

                # 5. Revoke -> the same token is dead on the next request.
                fed["registry"].revoke("openclaw")
                r = await _post(client, _rpc("list_agents", {}), token=fed["token"])
                assert r.status_code == 401

    asyncio.run(_scenario())
    # Audit captured the allowed call, the denial — and history survives
    # revocation (R5 acceptance).
    entries = fed["audit"].entries(agent="openclaw")
    assert any(e["decision"] == "deny" for e in entries)
    assert any(e["decision"] == "allow" for e in entries)


def test_http_legacy_shared_token_maps_to_trusted_operator(fed, monkeypatch):
    """v0.8.0 migration path: BOURDON_PEER_TOKEN_SERVER keeps working and
    resolves to the trusted operator identity (existing peers unaffected)."""
    httpx = pytest.importorskip("httpx")
    from starlette.middleware import Middleware

    monkeypatch.setenv("BOURDON_PEER_TOKEN_SERVER", "legacy-shared-token")
    app = fed["server"].http_app(
        middleware=[Middleware(server_module._build_auth_middleware(fed["registry"]))],
        stateless_http=True,
        json_response=True,
    )

    async def _scenario():
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://test"
            ) as client:
                r = await _post(
                    client, _rpc("list_agents", {}), token="legacy-shared-token"
                )
                assert r.status_code == 200
                body = r.json()["result"]["structuredContent"]
                assert set(body["agents"]) == {"claude-code", "codex"}

    asyncio.run(_scenario())


def test_http_no_token_material_in_logs(fed, monkeypatch, caplog):
    """R2 acceptance: grep test — no token appears in logs at any level."""
    import logging

    httpx = pytest.importorskip("httpx")
    from starlette.middleware import Middleware

    monkeypatch.delenv("BOURDON_PEER_TOKEN_SERVER", raising=False)
    app = fed["server"].http_app(
        middleware=[Middleware(server_module._build_auth_middleware(fed["registry"]))],
        stateless_http=True,
        json_response=True,
    )

    async def _scenario():
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://test"
            ) as client:
                await _post(client, _rpc("list_agents", {}), token=fed["token"])
                await _post(client, _rpc("list_agents", {}), token="bdn_invalid_tok")

    with caplog.at_level(logging.DEBUG):
        asyncio.run(_scenario())
    assert fed["token"] not in caplog.text
    assert "bdn_invalid_tok" not in caplog.text
    audit_text = (fed["audit"].path.read_text(encoding="utf-8")
                  if fed["audit"].path.exists() else "")
    assert fed["token"] not in audit_text
