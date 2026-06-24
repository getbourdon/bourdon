"""Tests for participants.claude_desktop_cowork.

Privacy-critical: these stores contain full conversation content. The
participant must emit recognition METADATA ONLY. The mandatory
no-content-leakage test plants sentinels in conversation bodies and asserts
they never reach the L5 output.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from participants.base import BourdonParticipant, Visibility
from participants.claude_desktop_cowork import (
    AGENT_ID,
    AGENT_TYPE,
    ClaudeDesktopCoworkParticipant,
)

ACCT = "acct-uuid"
ORG = "org-uuid"
STORE = "local-agent-mode-sessions"

# Sentinel that must NEVER appear anywhere in emitted output.
SENTINEL = "PRIVATE_SENTINEL_DO_NOT_LEAK_42"


# ---- Fake-tree builders -----------------------------------------------------


def _store_dir(desktop: Path) -> Path:
    d = desktop / STORE / ACCT / ORG
    d.mkdir(parents=True, exist_ok=True)
    return d


def _write_run(
    desktop: Path,
    run_id: str = "run1",
    *,
    title: str = "Wire the Co-Work export",
    cwd: str = "/Users/radman/repos/ShipStable",
    model: str = "claude-opus-4-8",
    created_at: int = 1_717_200_000_000,  # ms epoch (2024-06-01)
    user_selected_folders: list[str] | None = None,
    mcq_answers: dict | None = None,
    initial_message: str | None = None,
    enabled_mcp_tools: dict | None = None,
    audit_records: list[dict] | None = None,
    write_audit: bool = True,
) -> Path:
    """Create a ``local_<id>.json`` state file + a sibling ``audit.jsonl``."""
    store = _store_dir(desktop)
    state: dict = {
        "sessionId": run_id,
        "processName": "claude",
        "cliSessionId": f"cli-{run_id}",
        "cwd": cwd,
        "userSelectedFolders": user_selected_folders or [],
        "createdAt": created_at,
        "lastActivityAt": created_at + 60_000,
        "model": model,
        "permissionMode": "acceptEdits",
        "isArchived": False,
        "title": title,
        "userApprovedFileAccessPaths": [],
        "vmProcessName": "vm-claude",
        "slashCommands": [],
        "enabledMcpTools": enabled_mcp_tools
        if enabled_mcp_tools is not None
        else {"srv-a:read": True, "srv-a:write": True, "srv-b:list": False},
    }
    if mcq_answers is not None:
        state["mcqAnswers"] = mcq_answers
    if initial_message is not None:
        state["initialMessage"] = initial_message

    state_path = store / f"local_{run_id}.json"
    state_path.write_text(json.dumps(state), encoding="utf-8")

    if write_audit:
        run_dir = store / f"local_{run_id}"
        run_dir.mkdir(parents=True, exist_ok=True)
        records = (
            audit_records
            if audit_records is not None
            else _default_audit_records()
        )
        lines = [json.dumps(rec) for rec in records]
        (run_dir / "audit.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")

    return state_path


def _default_audit_records() -> list[dict]:
    return [
        {
            "type": "system",
            "subtype": "init",
            "cwd": "/Users/radman/repos/ShipStable",
            "model": "claude-opus-4-8",
            "mcp_servers": [{"name": "srv-a"}, {"name": "srv-b"}],
            "tools": ["Read", "Edit", "Bash", "Grep"],
            "skills": ["notes"],
            "plugins": [],
            "slash_commands": ["/notes", "/loop"],
            "claude_code_version": "1.2.3",
            "_audit_hmac": "deadbeef",
        },
        {
            "type": "user",
            "message": {"role": "user", "content": "free-form user body"},
            "_audit_hmac": "deadbeef",
        },
        {
            "type": "assistant",
            "message": {"role": "assistant", "content": "free-form assistant body"},
            "_audit_hmac": "deadbeef",
        },
        {
            "type": "result",
            "subtype": "success",
            "total_cost_usd": 0.4231,
            "num_turns": 7,
            "is_error": False,
            "duration_ms": 81234,
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 1000, "output_tokens": 500},
            "result": "a free-form natural-language summary of the run",
            "_audit_hmac": "deadbeef",
        },
    ]


# ---- 1. Identity ------------------------------------------------------------


def test_participant_satisfies_protocol(tmp_path):
    _write_run(tmp_path)
    participant = ClaudeDesktopCoworkParticipant(store_dir=tmp_path / STORE)
    assert isinstance(participant, BourdonParticipant)
    assert participant.agent_id == AGENT_ID
    assert participant.agent_type == AGENT_TYPE


def test_identity_constants():
    assert AGENT_ID == "claude-desktop-cowork"
    assert AGENT_TYPE == "code-assistant"
    # Slug must differ from the CLI claude-code participant.
    assert AGENT_ID != "claude-code"


# ---- 2. Export shape --------------------------------------------------------


def test_export_l5_shape(tmp_path):
    _write_run(tmp_path, run_id="r1", title="Build the cowork reader")
    participant = ClaudeDesktopCoworkParticipant(store_dir=tmp_path / STORE)

    manifest = participant.export_l5()
    data = manifest.to_dict()

    assert data["agent"]["id"] == "claude-desktop-cowork"
    assert data["agent"]["type"] == "code-assistant"

    session = data["recent_sessions"][0]
    assert session["date"] == "2024-06-01"
    assert session["cwd"] == "/Users/radman/repos/ShipStable"
    # title-derived key_action + model key_action present
    joined = " ".join(session["key_actions"])
    assert "Build the cowork reader" in joined
    assert "model: claude-opus-4-8" in joined
    # audit-sourced safe scalars surfaced
    assert "7 turns" in joined
    assert "$0.42" in joined
    # never list user files
    assert session.get("files_touched", []) == []

    # surface entity present + project entity inferred from cwd
    entity_names = {e["name"] for e in data["known_entities"]}
    assert "Claude Desktop Co-Work" in entity_names
    assert "ShipStable" in entity_names


def test_capabilities_are_counts_only(tmp_path):
    _write_run(tmp_path)
    participant = ClaudeDesktopCoworkParticipant(store_dir=tmp_path / STORE)
    caps = participant.export_l5().capabilities
    assert "claude-desktop-cowork" in caps
    # 2 enabled mcp tools in the default fixture
    assert "mcp-tools:2" in caps
    # init tool count (4 tools) surfaced as a count, never tool names
    assert "tools:4" in caps
    assert not any("Read" in c or "Bash" in c for c in caps)


def test_project_inference_includes_user_selected_folders(tmp_path):
    _write_run(
        tmp_path,
        run_id="rfolders",
        cwd="/tmp/scratch",
        user_selected_folders=["/Users/radman/repos/ILTT"],
    )
    participant = ClaudeDesktopCoworkParticipant(store_dir=tmp_path / STORE)
    names = {e["name"] for e in participant.export_l5().to_dict()["known_entities"]}
    assert "ILTT" in names
    assert "scratch" in names  # basename of cwd


# ---- 3. Redaction -----------------------------------------------------------


def test_redacts_secret_in_title(tmp_path):
    _write_run(tmp_path, run_id="rsec", title="ship sk_live_DEADBEEF1234 to prod")
    participant = ClaudeDesktopCoworkParticipant(store_dir=tmp_path / STORE)

    blob = json.dumps(participant.export_l5().to_dict())
    assert "sk_live_DEADBEEF1234" not in blob
    assert "redacted" in blob.lower()


# ---- 4. NO-CONTENT-LEAKAGE (mandatory) --------------------------------------


def test_no_conversation_content_leaks(tmp_path):
    """Plant the sentinel in every content surface; assert it never escapes."""
    audit = _default_audit_records()
    # Inject sentinel into a user message body.
    audit[1]["message"]["content"] = f"user said {SENTINEL} here"
    # ...and into the result free-form text.
    audit[3]["result"] = f"summary mentioning {SENTINEL}"

    _write_run(
        tmp_path,
        run_id="rleak",
        title="ordinary safe title",
        mcq_answers={"q1": f"answer {SENTINEL}"},
        initial_message=f"initial {SENTINEL} prompt",
        audit_records=audit,
    )

    participant = ClaudeDesktopCoworkParticipant(store_dir=tmp_path / STORE)
    blob = json.dumps(participant.export_l5().to_dict())

    assert SENTINEL not in blob, "conversation content leaked into the L5 manifest"
    # Sanity: the safe surface metadata did make it through.
    assert "ordinary safe title" in blob
    assert "7 turns" in blob


# ---- 5. Health --------------------------------------------------------------


def test_health_blocked_when_store_missing(tmp_path):
    participant = ClaudeDesktopCoworkParticipant(store_dir=tmp_path / "nope" / STORE)
    health = participant.health_check()
    assert health.status == "blocked"
    assert health.proposed_fix
    assert "co-work" in (health.reason or "").lower()


def test_health_degraded_when_empty(tmp_path):
    (tmp_path / STORE).mkdir(parents=True)
    participant = ClaudeDesktopCoworkParticipant(store_dir=tmp_path / STORE)
    health = participant.health_check()
    assert health.status == "degraded"


def test_health_ok_with_counts(tmp_path):
    _write_run(tmp_path, run_id="h1")
    _write_run(tmp_path, run_id="h2")
    participant = ClaudeDesktopCoworkParticipant(store_dir=tmp_path / STORE)
    health = participant.health_check()
    assert health.status == "ok"
    assert health.details["run_count"] == 2
    assert health.details["runs_extracted"] == 2
    assert health.details["runs_with_scalars"] == 2


def test_health_never_raises_on_malformed(tmp_path):
    store = _store_dir(tmp_path)
    (store / "local_bad.json").write_text("{ not json", encoding="utf-8")
    participant = ClaudeDesktopCoworkParticipant(store_dir=tmp_path / STORE)
    health = participant.health_check()
    # One malformed file, no good ones -> still degraded (no runs), never raises.
    assert health.status in {"ok", "degraded"}


def test_missing_audit_is_not_a_failure(tmp_path):
    """A run with no audit.jsonl => no scalars, but a valid session."""
    _write_run(tmp_path, run_id="noaudit", write_audit=False)
    participant = ClaudeDesktopCoworkParticipant(store_dir=tmp_path / STORE)
    manifest = participant.export_l5()
    session = manifest.to_dict()["recent_sessions"][0]
    joined = " ".join(session["key_actions"])
    assert "ordinary" not in joined  # sanity
    assert "turns" not in joined  # no audit scalars
    assert participant.health_check().status == "ok"


# ---- 6. since + epoch + cross-platform --------------------------------------


def test_export_sessions_filters_since(tmp_path):
    # old run (2023-01-01) + fresh run (2024-06-01)
    _write_run(tmp_path, run_id="old", created_at=1_672_531_200_000)
    _write_run(tmp_path, run_id="new", created_at=1_717_200_000_000)
    participant = ClaudeDesktopCoworkParticipant(store_dir=tmp_path / STORE)

    sessions = participant.export_sessions(since=datetime(2024, 1, 1, tzinfo=timezone.utc))
    dates = [s.date for s in sessions]
    assert "2024-06-01" in dates
    assert "2023-01-01" not in dates


def test_epoch_seconds_and_millis_both_resolve(tmp_path):
    from participants._claude_desktop import epoch_to_date

    # ms vs s for the same instant resolve to the same date
    assert epoch_to_date(1_717_200_000_000) == "2024-06-01"
    assert epoch_to_date(1_717_200_000) == "2024-06-01"
    assert epoch_to_date("1717200000000") == "2024-06-01"
    assert epoch_to_date(None) == ""
    assert epoch_to_date(True) == ""  # bool rejected


def test_session_visibility_is_team(tmp_path):
    _write_run(tmp_path)
    participant = ClaudeDesktopCoworkParticipant(store_dir=tmp_path / STORE)
    assert participant.export_l5().recent_sessions[0].visibility == Visibility.TEAM


@pytest.mark.parametrize(
    "platform, env, expected_suffix",
    [
        ("darwin", {}, ("Library", "Application Support", "Claude")),
        ("linux", {}, (".config", "Claude")),
        ("win32", {}, ("AppData", "Roaming", "Claude")),
    ],
)
def test_default_claude_desktop_dir_cross_platform(
    monkeypatch, tmp_path, platform, env, expected_suffix
):
    from participants import _claude_desktop

    monkeypatch.setattr(_claude_desktop.sys, "platform", platform)
    monkeypatch.delenv("BOURDON_CLAUDE_DESKTOP_DIR", raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)

    resolved = _claude_desktop.default_claude_desktop_dir(home=tmp_path)
    assert resolved is not None
    assert resolved.parts[-len(expected_suffix):] == expected_suffix
    assert str(tmp_path) in str(resolved)


def test_default_dir_env_override_wins(monkeypatch, tmp_path):
    from participants import _claude_desktop

    monkeypatch.setenv("BOURDON_CLAUDE_DESKTOP_DIR", str(tmp_path / "custom"))
    resolved = _claude_desktop.default_claude_desktop_dir(home=Path("/somewhere/else"))
    assert resolved == tmp_path / "custom"


def test_default_native_path_points_at_cowork_substore(monkeypatch, tmp_path):
    monkeypatch.delenv("BOURDON_CLAUDE_DESKTOP_DIR", raising=False)
    path = ClaudeDesktopCoworkParticipant.default_native_path(home=tmp_path)
    assert path.name == STORE
