"""OpenClaw participant tests (v0.9.0, spec R4/D6/D9).

The handshake gate is hard-enforced: unpatched or auth-disabled instances are
refused with actionable errors (mocked negative tests). Compliant instances
export within quarantine rules — `bourdon openclaw export` stages, never
writes the live store.
"""

from __future__ import annotations

import pytest
import yaml

from cli.main import main
from participants.base import ParticipantDiscoveryError
from participants.openclaw import (
    MIN_PATCHED_VERSION,
    OpenClawParticipant,
    verify_instance,
)


class FakeClient:
    def __init__(self, status=None, sessions=None, memories=None):
        self._status = status or {}
        self._sessions = sessions or []
        self._memories = memories or []

    def status(self):
        if isinstance(self._status, Exception):
            raise self._status
        return self._status

    def sessions(self):
        return self._sessions

    def memories(self):
        return self._memories


GOOD_STATUS = {"version": "2026.2.3", "auth_enabled": True}


def _participant(**kwargs) -> OpenClawParticipant:
    return OpenClawParticipant(
        url="http://127.0.0.1:8080", client=FakeClient(**kwargs)
    )


# -- Handshake gate: negative tests (R4 acceptance) ------------------------------


def test_unpatched_version_refused_with_actionable_error():
    p = _participant(status={"version": "2026.1.20", "auth_enabled": True})
    with pytest.raises(ParticipantDiscoveryError) as excinfo:
        p.discover()
    msg = str(excinfo.value)
    assert "CVE-2026-25253" in msg
    assert MIN_PATCHED_VERSION in msg  # tells the user exactly how to fix it


def test_auth_disabled_refused_with_actionable_error():
    p = _participant(status={"version": "2026.2.3", "auth_enabled": False})
    with pytest.raises(ParticipantDiscoveryError) as excinfo:
        p.discover()
    msg = str(excinfo.value)
    assert "authentication DISABLED" in msg
    assert "enable auth" in msg


def test_missing_or_garbage_version_fails_closed():
    for status in ({"auth_enabled": True}, {"version": "???", "auth_enabled": True}):
        with pytest.raises(ParticipantDiscoveryError):
            verify_instance(status, "http://x:8080")


def test_unreachable_instance_blocked_health():
    p = _participant(status=ParticipantDiscoveryError(
        "OpenClaw instance unreachable at http://127.0.0.1:8080 (refused)"
    ))
    health = p.health_check()
    assert health.status == "blocked"
    assert "unreachable" in health.reason
    assert health.proposed_fix  # never a dead end


def test_health_check_never_raises_and_blocked_states_carry_fixes():
    unpatched = _participant(status={"version": "2026.1.1", "auth_enabled": True})
    health = unpatched.health_check()
    assert health.status == "blocked"
    assert "upgrade" in health.proposed_fix.lower()

    no_auth = _participant(status={"version": "2026.2.3", "auth_enabled": False})
    health = no_auth.health_check()
    assert health.status == "blocked"
    assert "enable auth" in health.proposed_fix.lower()


def test_exact_patch_version_passes():
    assert verify_instance(
        {"version": MIN_PATCHED_VERSION, "auth_enabled": True}, "http://x"
    ) == MIN_PATCHED_VERSION


def test_alternate_status_shapes_accepted():
    # auth flag under nested auth.enabled; version under openclaw_version.
    version = verify_instance(
        {"openclaw_version": "2026.3.1", "auth": {"enabled": True}}, "http://x"
    )
    assert version == "2026.3.1"


# -- Compliant instance: export within quarantine rules --------------------------


def test_export_l5_shapes_manifest_and_redacts():
    p = _participant(
        status=GOOD_STATUS,
        sessions=[
            {"started_at": "2026-06-08T10:00:00Z", "title": "planned trip"},
            {"started_at": "2026-06-09T10:00:00Z",
             "title": "my api_key is sk-12345 lol"},
        ],
        memories=[
            {"name": "Groceries", "summary": "weekly list", "tags": ["home"]},
            {"name": "Creds", "summary": "the password is hunter2"},
        ],
    )
    manifest = p.export_l5()
    assert manifest.agent.id == "openclaw"
    assert manifest.agent.type == "other"
    data = manifest.to_dict()
    text = yaml.safe_dump(data)
    assert "sk-12345" not in text
    assert "hunter2" not in text
    assert len(data["recent_sessions"]) == 2
    # Quarantined-class default visibility is TEAM, never PUBLIC.
    assert data["visibility_policy"]["default"] == "team"


def test_export_sessions_since_filter():
    from datetime import datetime, timezone

    p = _participant(
        status=GOOD_STATUS,
        sessions=[
            {"started_at": "2026-06-01T10:00:00Z", "title": "old"},
            {"started_at": "2026-06-09T10:00:00Z", "title": "new"},
        ],
    )
    rows = p.export_sessions(since=datetime(2026, 6, 5, tzinfo=timezone.utc))
    assert len(rows) == 1
    assert rows[0].key_actions == ["new"]


def test_quarantined_class_marker_present():
    assert OpenClawParticipant.QUARANTINED_CLASS is True


# -- CLI: export stages, never writes live; doctor exits non-zero on refusal ------


def test_cli_export_refused_handshake_exits_nonzero(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("OPENCLAW_URL", "http://127.0.0.1:1")  # nothing listens
    code = main(["openclaw", "export", "--library", str(tmp_path / "lib")])
    assert code == 1
    err = capsys.readouterr().err
    assert "handshake refused" in err
    assert not (tmp_path / "lib").exists()  # nothing written anywhere


def test_cli_export_stages_for_compliant_instance(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(
        "participants.openclaw.OpenClawApiClient",
        lambda url, token=None, timeout=5.0: FakeClient(
            status=GOOD_STATUS,
            memories=[{"name": "Finding", "summary": "x"}],
        ),
    )
    library = tmp_path / "lib"
    code = main(["openclaw", "export", "--library", str(library)])
    out, err = capsys.readouterr()
    assert code == 0
    staged = library / "staging" / "openclaw" / "openclaw.l5.yaml"
    assert staged.exists()
    assert not (library / "agents" / "openclaw.l5.yaml").exists()  # D6: never live
    assert "STAGED" in out
    assert "staging promote openclaw" in err


def test_cli_doctor_blocked_exits_nonzero(capsys, monkeypatch):
    monkeypatch.setenv("OPENCLAW_URL", "http://127.0.0.1:1")
    code = main(["openclaw", "doctor"])
    assert code == 1
    assert "blocked" in capsys.readouterr().out


def test_agent_add_trusted_requires_risk_ack_for_openclaw(capsys):
    """R4: registering OpenClaw as trusted needs --i-understand-the-risk."""
    assert main(["agent", "add", "openclaw", "--tier", "trusted"]) == 1
    assert "--i-understand-the-risk" in capsys.readouterr().err
    assert main([
        "agent", "add", "openclaw", "--tier", "trusted", "--i-understand-the-risk",
    ]) == 0
    capsys.readouterr()
    # Quarantined registration never needs the flag.
    assert main(["agent", "add", "openclaw2"]) == 0
