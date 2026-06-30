"""Conformance parity for the L6 FEDERATION families (the trust boundary).

The pytest half of the cross-impl contract: the live oracle must reproduce its
own committed fixtures byte/structure-for-structure. The TS mirror's vitest suite
asserts the SAME fixtures. Regenerate via ``python tools/gen_conformance.py``.

Families covered here:
- ``tier_matrix.json``        -- D4 tool x trust-tier x granted -> allow|deny
                                 (+ the verbatim structured-denial dict), driven
                                 through the real ``create_l6_server`` enforcement.
- ``fed_seed_library/``       -- the 2-agent seeded agent-library loads cleanly.
- ``on_disk/federation.yaml`` -- parses + authenticates per ``auth_vectors.json``.
- ``on_disk/audit.jsonl``     -- the append-only record shape.
"""

from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path

import pytest
import yaml

from core.federation_audit import FederationAudit
from core.federation_registry import AgentIdentity, FederationRegistry
from core.l6_store import L6Store

pytest.importorskip("fastmcp")

from core import l6_server as server_module  # noqa: E402

_CONF = Path(__file__).resolve().parent.parent / "conformance"
_TIER_MATRIX = json.loads((_CONF / "tier_matrix.json").read_text(encoding="utf-8"))
_AUTH_VECTORS = json.loads((_CONF / "on_disk" / "auth_vectors.json").read_text(encoding="utf-8"))
_SEED_DIR = _CONF / "fed_seed_library"


def _call(server, name, identity, kwargs):
    async def _inner():
        tool = await server.get_tool(name)
        res = tool.fn(**kwargs)
        if asyncio.iscoroutine(res):
            res = await res
        return res

    from core.federation_registry import reset_caller, set_caller

    ctx = set_caller(identity) if identity is not None else None
    try:
        return asyncio.run(_inner())
    finally:
        if ctx is not None:
            reset_caller(ctx)


@pytest.fixture
def server(tmp_path):
    """Stand up the real L6 server over a TEMP copy of the committed seed library."""
    lib = tmp_path / "lib"
    shutil.copytree(_SEED_DIR, lib)
    registry = FederationRegistry(tmp_path / "federation.yaml")
    registry.add_agent("openclaw", tier="quarantined", grants=["claude-code"])
    audit = FederationAudit(tmp_path / "audit.jsonl")
    store = L6Store(lib)
    return server_module.create_l6_server(store, registry=registry, audit=audit)


def test_seed_library_loads_both_agents():
    store = L6Store(_SEED_DIR)
    assert store.list_agents() == ["claude-code", "codex"]
    # Cross-agent shared entity + visibility spread are present.
    matches = store.find_entity("Bourdon", access_level="public")
    assert {a for m in matches for a in m.agents} == {"claude-code", "codex"}


@pytest.mark.parametrize(
    "case",
    _TIER_MATRIX["cases"],
    ids=lambda c: f"{c['tier']}-{c['tool']}-{c['namespace']}-{c['granted']}",
)
def test_tier_matrix_decision_matches_oracle(server, case):
    """Replaying each case through the live enforcement reproduces the committed
    decision and (on deny) the verbatim structured-denial dict."""
    caller = _TIER_MATRIX["caller"]
    if case["tier"] == "trusted":
        identity = None  # default OPERATOR
    else:
        q = caller["quarantined"]
        identity = AgentIdentity(
            agent_id=q["agent_id"], tier=q["tier"], grants=tuple(q["grants"])
        )
    result = _call(server, case["tool"], identity, dict(case["args"]))
    is_deny = isinstance(result, dict) and result.get("error") == "access denied"
    assert ("deny" if is_deny else "allow") == case["decision"]
    if case["decision"] == "deny":
        assert result == case["denial"]
    else:
        assert case["denial"] is None


def test_on_disk_registry_authenticates_per_vectors(tmp_path):
    """The committed federation.yaml, loaded by the real registry, resolves each
    synthetic token to exactly the auth_vectors expectation."""
    # Copy into a writable temp path (the registry stats its own file).
    reg_path = tmp_path / "federation.yaml"
    reg_path.write_text(
        (_CONF / "on_disk" / "federation.yaml").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    reg = FederationRegistry(reg_path)
    for vec in _AUTH_VECTORS["vectors"]:
        token = "".join(vec["token_fragments"])
        ident = reg.authenticate(token)
        if vec["expect"] is None:
            assert ident is None, vec["name"]
        else:
            assert ident is not None, vec["name"]
            assert {
                "agent_id": ident.agent_id,
                "tier": ident.tier,
                "grants": list(ident.grants),
            } == vec["expect"]


def test_on_disk_federation_yaml_stores_only_hashes():
    """No synthetic token plaintext is at rest; every row is sha256-only."""
    data = yaml.safe_load((_CONF / "on_disk" / "federation.yaml").read_text(encoding="utf-8"))
    assert data["version"] == 1
    serialized = json.dumps(data)
    for vec in _AUTH_VECTORS["vectors"]:
        token = "".join(vec["token_fragments"])
        if token:  # the empty-token negative vector is trivially a substring
            assert token not in serialized
    for row in data["agents"].values():
        assert len(row["token_sha256"]) == 64  # hex sha256


def test_on_disk_audit_jsonl_record_shape():
    """Append-only JSONL: ts then agent/op/namespace/decision, detail only when set."""
    lines = [
        ln
        for ln in (_CONF / "on_disk" / "audit.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
        if ln.strip()
    ]
    assert lines
    for ln in lines:
        e = json.loads(ln)
        assert set(e) >= {"ts", "agent", "op", "namespace", "decision"}
        assert e["ts"].endswith("Z")
        assert e["decision"] in ("allow", "deny")
    # A deny record carries a detail; an allow-without-detail omits the key.
    parsed = [json.loads(ln) for ln in lines]
    assert any(e["decision"] == "deny" and "detail" in e for e in parsed)
    assert any(e["decision"] == "allow" and "detail" not in e for e in parsed)
