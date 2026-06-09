"""CLI e2e tests for the v0.9.0 federation trust commands.

`bourdon agent add/list/rotate/set-tier`, `grant`/`ungrant`, `revoke`,
`staging list/promote/reject`, `audit`. The autouse conftest fixture points
BOURDON_FEDERATION_CONFIG / BOURDON_AUDIT_PATH at tmp paths.
"""

from __future__ import annotations

import re

import yaml

from cli.main import main
from core.federation_audit import FederationAudit
from core.federation_registry import FederationRegistry
from core.federation_staging import merge_into_staged


def _extract_token(out: str) -> str:
    match = re.search(r"token: (bdn_[0-9a-f]+)", out)
    assert match, f"no token in output: {out!r}"
    return match.group(1)


def test_agent_add_list_rotate_revoke_cycle(capsys):
    assert main(["agent", "add", "openclaw", "--grant", "claude-code"]) == 0
    token = _extract_token(capsys.readouterr().out)

    registry = FederationRegistry()
    ident = registry.authenticate(token)
    assert ident.agent_id == "openclaw"
    assert ident.tier == "quarantined"  # default tier is quarantined

    assert main(["agent", "list"]) == 0
    out = capsys.readouterr().out
    assert "openclaw" in out and "quarantined" in out and "bdn_" not in out

    assert main(["agent", "rotate", "openclaw"]) == 0
    new_token = _extract_token(capsys.readouterr().out)
    assert registry.authenticate(token) is None
    assert registry.authenticate(new_token) is not None

    assert main(["revoke", "openclaw"]) == 0
    assert registry.authenticate(new_token) is None

    assert main(["agent", "list"]) == 0
    assert "REVOKED" in capsys.readouterr().out


def test_duplicate_add_fails_nonzero(capsys):
    assert main(["agent", "add", "clyde", "--tier", "trusted"]) == 0
    capsys.readouterr()
    assert main(["agent", "add", "clyde"]) == 1
    assert "already registered" in capsys.readouterr().err


def test_grant_and_ungrant(capsys):
    main(["agent", "add", "openclaw"])
    capsys.readouterr()
    assert main(["grant", "openclaw", "codex"]) == 0
    row = FederationRegistry().get("openclaw")
    assert row["grants"] == ["codex"]
    assert main(["ungrant", "openclaw", "codex"]) == 0
    assert FederationRegistry().get("openclaw")["grants"] == []


def test_revoke_unknown_agent_fails(capsys):
    assert main(["revoke", "ghost"]) == 1
    assert "unknown agent" in capsys.readouterr().err


def test_staging_list_promote_reject_e2e(tmp_path, capsys):
    library = tmp_path / "agent-library"
    (library / "agents").mkdir(parents=True)
    merge_into_staged(
        library, "openclaw", "openclaw",
        entities=[{"name": "Finding", "summary": "x"}],
        sessions=[{"date": "2026-06-09"}],
        agent_type="other",
    )
    merge_into_staged(
        library, "openclaw2", "openclaw2",
        entities=[{"name": "Other", "summary": "y"}],
        sessions=None,
        agent_type="other",
    )

    assert main(["staging", "list", "--library", str(library)]) == 0
    out = capsys.readouterr().out
    assert "openclaw" in out and "openclaw2" in out

    assert main(["staging", "promote", "openclaw", "--library", str(library)]) == 0
    out = capsys.readouterr().out
    assert "promoted openclaw" in out
    live = library / "agents" / "openclaw.l5.yaml"
    assert live.exists()
    data = yaml.safe_load(live.read_text(encoding="utf-8"))
    assert [e["name"] for e in data["known_entities"]] == ["Finding"]
    assert not (library / "staging" / "openclaw").exists()

    assert main(["staging", "reject", "openclaw2", "--library", str(library)]) == 0
    capsys.readouterr()
    assert not (library / "staging" / "openclaw2").exists()
    assert not (library / "agents" / "openclaw2.l5.yaml").exists()

    # Nothing left.
    assert main(["staging", "list", "--library", str(library)]) == 0
    assert "no staged writes" in capsys.readouterr().out


def test_audit_query_and_export(capsys):
    audit = FederationAudit()
    audit.record("openclaw", "find_entity", "claude-code", "allow")
    audit.record("openclaw", "query_agent_memory", "codex", "deny", "not granted")
    audit.record("clyde", "list_agents", "*", "allow")

    assert main(["audit"]) == 0
    out = capsys.readouterr().out
    assert "openclaw" in out and "clyde" in out

    assert main(["audit", "--agent", "openclaw", "--denials"]) == 0
    out = capsys.readouterr().out
    assert "query_agent_memory" in out and "find_entity" not in out

    assert main(["audit", "--export", "--agent", "clyde"]) == 0
    out = capsys.readouterr().out.strip()
    assert out.startswith("{") and '"clyde"' in out
