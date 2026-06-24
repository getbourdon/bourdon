"""Tests for core/federation_registry.py — identities, tokens, tiers (v0.9.0).

Every security control gets a negative test that proves it blocks.
"""

from __future__ import annotations

import logging

import pytest
import yaml

from core.federation_registry import (
    OPERATOR,
    AgentIdentity,
    FederationRegistry,
    RegistryError,
    get_caller,
    reset_caller,
    set_caller,
)


@pytest.fixture
def registry(tmp_path):
    return FederationRegistry(tmp_path / "federation.yaml")


def test_add_agent_returns_token_once_and_stores_only_hash(registry):
    token = registry.add_agent("openclaw", tier="quarantined")
    assert token.startswith("bdn_")
    raw = registry.path.read_text(encoding="utf-8")
    assert token not in raw  # plaintext NEVER at rest
    assert "token_sha256" in raw


def test_authenticate_resolves_identity_with_tier_and_grants(registry):
    token = registry.add_agent("openclaw", tier="quarantined", grants=["claude-code"])
    ident = registry.authenticate(token)
    assert ident is not None
    assert ident.agent_id == "openclaw"
    assert ident.tier == "quarantined"
    assert ident.grants == ("claude-code",)


def test_authenticate_rejects_wrong_token(registry):
    registry.add_agent("openclaw")
    assert registry.authenticate("bdn_" + "0" * 48) is None
    assert registry.authenticate("") is None


def test_revoked_token_never_authenticates(registry):
    token = registry.add_agent("openclaw")
    assert registry.authenticate(token) is not None
    registry.revoke("openclaw")
    assert registry.authenticate(token) is None  # effective immediately


def test_rotate_invalidates_old_token(registry):
    old = registry.add_agent("clyde", tier="trusted")
    new = registry.rotate_token("clyde")
    assert new != old
    assert registry.authenticate(old) is None
    ident = registry.authenticate(new)
    assert ident is not None and ident.agent_id == "clyde"


def test_rotate_revoked_agent_refuses(registry):
    registry.add_agent("openclaw")
    registry.revoke("openclaw")
    with pytest.raises(RegistryError):
        registry.rotate_token("openclaw")


def test_duplicate_add_refuses(registry):
    registry.add_agent("openclaw")
    with pytest.raises(RegistryError):
        registry.add_agent("openclaw")


def test_invalid_agent_id_and_tier_refused(registry):
    with pytest.raises(RegistryError):
        registry.add_agent("Bad Slug!")
    with pytest.raises(RegistryError):
        registry.add_agent("ok-slug", tier="observer")  # P2 tier doesn't exist yet


def test_grants_mutate_and_persist(registry):
    registry.add_agent("openclaw")
    registry.grant("openclaw", "claude-code")
    registry.grant("openclaw", "codex")
    registry.ungrant("openclaw", "codex")
    reloaded = FederationRegistry(registry.path)
    row = reloaded.get("openclaw")
    assert row["grants"] == ["claude-code"]


def test_running_server_sees_revocation_via_mtime_reload(registry, tmp_path):
    """`bourdon revoke` from another process must bite a live server."""
    token = registry.add_agent("openclaw")
    other_process = FederationRegistry(registry.path)
    assert other_process.authenticate(token) is not None
    registry.revoke("openclaw")  # simulates the CLI in another process
    assert other_process.authenticate(token) is None


def test_corrupt_registry_fails_closed(tmp_path):
    path = tmp_path / "federation.yaml"
    path.write_text("agents: [unbalanced", encoding="utf-8")
    reg = FederationRegistry(path)
    assert reg.authenticate("bdn_" + "0" * 48) is None
    assert not reg.has_active_agents()


def test_list_agents_never_exposes_token_hash(registry):
    registry.add_agent("openclaw")
    rows = registry.list_agents()
    assert "token_sha256" not in yaml.safe_dump(rows)
    assert rows["openclaw"]["has_token"] is True


def test_no_token_material_in_logs(registry, caplog):
    """R2 acceptance: no token material appears in any log output."""
    with caplog.at_level(logging.DEBUG):
        token = registry.add_agent("openclaw")
        registry.authenticate(token)
        registry.authenticate("bdn_wrong")
        registry.revoke("openclaw")
        registry.authenticate(token)
    assert token not in caplog.text
    assert "bdn_wrong" not in caplog.text


def test_caller_contextvar_defaults_to_trusted_operator():
    assert get_caller() is OPERATOR
    assert OPERATOR.is_trusted
    quarantined = AgentIdentity(agent_id="openclaw", tier="quarantined")
    ctx = set_caller(quarantined)
    try:
        assert get_caller().agent_id == "openclaw"
        assert not get_caller().is_trusted
    finally:
        reset_caller(ctx)
    assert get_caller() is OPERATOR


def test_may_read_deny_by_default():
    ident = AgentIdentity(agent_id="openclaw", tier="quarantined", grants=("a",))
    assert ident.may_read("a")
    assert not ident.may_read("b")  # deny-by-default
    assert AgentIdentity(agent_id="x", tier="trusted").may_read("anything")
