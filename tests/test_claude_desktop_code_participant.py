"""Tests for participants.claude_desktop_code (desktop GUI Claude Code).

Metadata-only surface (no transcript on disk), but still privacy-enforced: the
participant emits recognition metadata only, and the no-content-leakage test
proves a planted sentinel never escapes.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from participants.base import BourdonParticipant, Visibility
from participants.claude_desktop_code import (
    AGENT_ID,
    AGENT_TYPE,
    ClaudeDesktopCodeParticipant,
)

ACCT = "acct-uuid"
ORG = "org-uuid"
STORE = "claude-code-sessions"

SENTINEL = "PRIVATE_SENTINEL_DO_NOT_LEAK_42"


def _store_dir(desktop: Path) -> Path:
    d = desktop / STORE / ACCT / ORG
    d.mkdir(parents=True, exist_ok=True)
    return d


def _write_conv(
    desktop: Path,
    conv_id: str = "conv1",
    *,
    title: str = "Refactor the auth module",
    cwd: str = "/Users/radman/repos/ILTT",
    model: str = "claude-sonnet-4-5",
    effort: str = "high",
    created_at: int = 1_717_200_000_000,
    plan_path: str | None = None,
    enabled_mcp_tools: dict | None = None,
) -> Path:
    store = _store_dir(desktop)
    state: dict = {
        "sessionId": conv_id,
        "cliSessionId": f"cli-{conv_id}",
        "cwd": cwd,
        "originCwd": cwd,
        "createdAt": created_at,
        "lastActivityAt": created_at + 60_000,
        "model": model,
        "effort": effort,
        "isArchived": False,
        "title": title,
        "titleSource": "auto",
        "permissionMode": "default",
        "enabledMcpTools": enabled_mcp_tools
        if enabled_mcp_tools is not None
        else {"srv-x:read": True, "srv-x:write": False},
    }
    if plan_path is not None:
        state["planPath"] = plan_path
    state_path = store / f"local_{conv_id}.json"
    state_path.write_text(json.dumps(state), encoding="utf-8")
    return state_path


# ---- 1. Identity ------------------------------------------------------------


def test_participant_satisfies_protocol(tmp_path):
    _write_conv(tmp_path)
    participant = ClaudeDesktopCodeParticipant(store_dir=tmp_path / STORE)
    assert isinstance(participant, BourdonParticipant)
    assert participant.agent_id == AGENT_ID
    assert participant.agent_type == AGENT_TYPE


def test_identity_constants():
    assert AGENT_ID == "claude-desktop-code"
    assert AGENT_TYPE == "code-assistant"
    assert AGENT_ID != "claude-code"
    # The two desktop surfaces are distinct slugs.
    assert AGENT_ID != "claude-desktop-cowork"


# ---- 2. Export shape --------------------------------------------------------


def test_export_l5_shape(tmp_path):
    _write_conv(tmp_path, conv_id="c1", title="Wire the desktop reader")
    participant = ClaudeDesktopCodeParticipant(store_dir=tmp_path / STORE)

    data = participant.export_l5().to_dict()
    assert data["agent"]["id"] == "claude-desktop-code"
    assert data["agent"]["type"] == "code-assistant"

    session = data["recent_sessions"][0]
    assert session["date"] == "2024-06-01"
    assert session["cwd"] == "/Users/radman/repos/ILTT"
    joined = " ".join(session["key_actions"])
    assert "Wire the desktop reader" in joined
    assert "model: claude-sonnet-4-5" in joined
    assert "effort: high" in joined
    assert session.get("files_touched", []) == []

    entity_names = {e["name"] for e in data["known_entities"]}
    assert "Claude Desktop Code" in entity_names
    assert "ILTT" in entity_names


def test_capabilities_are_counts_only(tmp_path):
    _write_conv(tmp_path)
    participant = ClaudeDesktopCodeParticipant(store_dir=tmp_path / STORE)
    caps = participant.export_l5().capabilities
    assert "claude-desktop-code" in caps
    assert "mcp-tools:1" in caps  # one enabled tool in fixture


# ---- 3. Redaction -----------------------------------------------------------


def test_redacts_secret_in_title(tmp_path):
    _write_conv(tmp_path, conv_id="csec", title="leak sk_live_DEADBEEF1234 oops")
    participant = ClaudeDesktopCodeParticipant(store_dir=tmp_path / STORE)
    blob = json.dumps(participant.export_l5().to_dict())
    assert "sk_live_DEADBEEF1234" not in blob
    assert "redacted" in blob.lower()


# ---- 4. NO-CONTENT-LEAKAGE (mandatory) --------------------------------------


def test_no_content_leaks_planpath(tmp_path):
    """planPath is never emitted; a sentinel in it must not escape."""
    _write_conv(
        tmp_path,
        conv_id="cleak",
        title="ordinary safe title",
        plan_path=f"/Users/radman/.plans/{SENTINEL}.md",
    )
    participant = ClaudeDesktopCodeParticipant(store_dir=tmp_path / STORE)
    blob = json.dumps(participant.export_l5().to_dict())
    assert SENTINEL not in blob
    assert "ordinary safe title" in blob


# ---- 5. Health --------------------------------------------------------------


def test_health_blocked_when_store_missing(tmp_path):
    participant = ClaudeDesktopCodeParticipant(store_dir=tmp_path / "nope" / STORE)
    health = participant.health_check()
    assert health.status == "blocked"
    assert health.proposed_fix


def test_health_degraded_when_empty(tmp_path):
    (tmp_path / STORE).mkdir(parents=True)
    participant = ClaudeDesktopCodeParticipant(store_dir=tmp_path / STORE)
    assert participant.health_check().status == "degraded"


def test_health_ok_with_counts(tmp_path):
    _write_conv(tmp_path, conv_id="h1")
    _write_conv(tmp_path, conv_id="h2")
    participant = ClaudeDesktopCodeParticipant(store_dir=tmp_path / STORE)
    health = participant.health_check()
    assert health.status == "ok"
    assert health.details["conversation_count"] == 2
    assert health.details["conversations_extracted"] == 2


def test_health_never_raises_on_malformed(tmp_path):
    store = _store_dir(tmp_path)
    (store / "local_bad.json").write_text("{ broken", encoding="utf-8")
    participant = ClaudeDesktopCodeParticipant(store_dir=tmp_path / STORE)
    assert participant.health_check().status in {"ok", "degraded"}


# ---- 6. since + epoch -------------------------------------------------------


def test_export_sessions_filters_since(tmp_path):
    _write_conv(tmp_path, conv_id="old", created_at=1_672_531_200_000)  # 2023-01-01
    _write_conv(tmp_path, conv_id="new", created_at=1_717_200_000_000)  # 2024-06-01
    participant = ClaudeDesktopCodeParticipant(store_dir=tmp_path / STORE)
    dates = [
        s.date
        for s in participant.export_sessions(
            since=datetime(2024, 1, 1, tzinfo=timezone.utc)
        )
    ]
    assert "2024-06-01" in dates
    assert "2023-01-01" not in dates


def test_epoch_seconds_handled(tmp_path):
    """createdAt given in seconds (< 1e12) still resolves to a date."""
    _write_conv(tmp_path, conv_id="secs", created_at=1_717_200_000)  # seconds
    participant = ClaudeDesktopCodeParticipant(store_dir=tmp_path / STORE)
    assert participant.export_l5().recent_sessions[0].date == "2024-06-01"


def test_session_visibility_is_team(tmp_path):
    _write_conv(tmp_path)
    participant = ClaudeDesktopCodeParticipant(store_dir=tmp_path / STORE)
    assert participant.export_l5().recent_sessions[0].visibility == Visibility.TEAM


def test_default_native_path_points_at_code_substore(monkeypatch, tmp_path):
    monkeypatch.delenv("BOURDON_CLAUDE_DESKTOP_DIR", raising=False)
    path = ClaudeDesktopCodeParticipant.default_native_path(home=tmp_path)
    assert path.name == STORE
