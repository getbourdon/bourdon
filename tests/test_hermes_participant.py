"""Tests for participants.hermes -- Hermes Agent external participant."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import jsonschema
import pytest

from participants.base import (
    SPEC_VERSION,
    BourdonParticipant,
    HealthStatus,
    ParticipantDiscoveryError,
    Visibility,
)
from participants.hermes import (
    AGENT_ID,
    AGENT_TYPE,
    HermesParticipant,
    _project_key_from_cwd,
    default_native_path,
)

_SCHEMA_PATH = Path(__file__).resolve().parent.parent / "spec" / "L5_schema.json"


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _make_state_db(path: Path, *, with_archived: bool = True) -> None:
    db = sqlite3.connect(path)
    db.execute(
        "CREATE TABLE sessions(id TEXT, source TEXT, model TEXT, title TEXT, "
        "cwd TEXT, started_at REAL, ended_at REAL, message_count INT, "
        "tool_call_count INT, archived INT)"
    )
    db.execute(
        "CREATE TABLE messages(id INTEGER, session_id TEXT, role TEXT, "
        "content TEXT, tool_name TEXT)"
    )
    ts = datetime(2026, 6, 20, tzinfo=timezone.utc).timestamp()
    db.execute(
        "INSERT INTO sessions VALUES('s1','tui','m','Refactor auth',"
        "'/projects/bourdon',?,?,10,4,0)",
        (ts, ts),
    )
    db.execute(
        "INSERT INTO sessions VALUES('s2','cli','m','Fix tests',"
        "'/projects/bourdon',?,?,5,2,0)",
        (ts, ts),
    )
    if with_archived:
        db.execute(
            "INSERT INTO sessions VALUES('s3','slack','m','archived',"
            "'/projects/secretwork',?,?,2,0,1)",
            (ts, ts),
        )
    for tn in ("terminal", "read_file", "patch"):
        db.execute("INSERT INTO messages VALUES(1,'s1','tool','x',?)", (tn,))
    db.commit()
    db.close()


@pytest.fixture
def hermes_home(tmp_path: Path) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    (home / "memories").mkdir()
    (home / "skills").mkdir()
    (home / "memories" / "user.md").write_text(
        "- Prefers concise answers\n- Works in Pacific timezone\n"
    )
    (home / "memories" / "memory.md").write_text(
        "- Project uses pytest with xdist\n"
        "- API_KEY=sk-supersecretkey1234567890abcdef\n"
    )
    _make_state_db(home / "state.db")
    return home


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


def test_structural_conformance(hermes_home: Path) -> None:
    p = HermesParticipant(hermes_home=hermes_home)
    assert isinstance(p, BourdonParticipant)
    assert p.agent_id == AGENT_ID == "hermes"
    assert p.agent_type == AGENT_TYPE


def test_default_native_path_honors_env(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "custom"))
    assert default_native_path() == tmp_path / "custom"
    monkeypatch.delenv("HERMES_HOME", raising=False)
    assert default_native_path(tmp_path).name == ".hermes"


# ---------------------------------------------------------------------------
# discover()
# ---------------------------------------------------------------------------


def test_discover_raises_when_home_missing(tmp_path: Path) -> None:
    p = HermesParticipant(hermes_home=tmp_path / "nope")
    with pytest.raises(ParticipantDiscoveryError):
        p.discover()


def test_discover_returns_store(hermes_home: Path) -> None:
    p = HermesParticipant(hermes_home=hermes_home)
    store = p.discover()
    assert store.version == "hermes-home-v1"
    assert store.metadata["sources"]["state_db"]
    assert store.metadata["sources"]["memories_dir"]


# ---------------------------------------------------------------------------
# Schema conformance + content
# ---------------------------------------------------------------------------


def test_export_l5_validates_against_schema(hermes_home: Path) -> None:
    schema = json.loads(_SCHEMA_PATH.read_text())
    p = HermesParticipant(hermes_home=hermes_home)
    data = p.export_l5().to_dict()
    jsonschema.validate(data, schema)
    assert data["spec_version"] == SPEC_VERSION
    assert data["agent"]["id"] == "hermes"
    assert data["agent"]["role_narrative"]


def test_export_l5_extracts_project_and_memory_entities(hermes_home: Path) -> None:
    p = HermesParticipant(hermes_home=hermes_home)
    data = p.export_l5().to_dict()
    names = {e["name"] for e in data["known_entities"]}
    types = {e["type"] for e in data["known_entities"]}
    assert "Bourdon" in names  # project from cwd
    assert "project" in types
    assert "preference" in types  # user.md
    assert "fact" in types  # memory.md


def test_export_l5_redacts_secrets(hermes_home: Path) -> None:
    """A credential-shaped memory line must never leak verbatim into L5."""
    p = HermesParticipant(hermes_home=hermes_home)
    blob = json.dumps(p.export_l5().to_dict())
    assert "sk-supersecretkey1234567890abcdef" not in blob


def test_export_sessions_excludes_archived(hermes_home: Path) -> None:
    p = HermesParticipant(hermes_home=hermes_home)
    sessions = p.export_sessions(datetime(1970, 1, 1, tzinfo=timezone.utc))
    cwds = {s.cwd for s in sessions}
    assert "/projects/secretwork" not in cwds  # archived row dropped
    assert "/projects/bourdon" in cwds


def test_export_sessions_captures_tool_names(hermes_home: Path) -> None:
    p = HermesParticipant(hermes_home=hermes_home)
    sessions = p.export_sessions(datetime(1970, 1, 1, tzinfo=timezone.utc))
    s1 = next(s for s in sessions if s.cwd == "/projects/bourdon" and s.key_actions)
    joined = " ".join(s1.key_actions)
    assert "terminal" in joined


def test_export_sessions_since_filter(hermes_home: Path) -> None:
    p = HermesParticipant(hermes_home=hermes_home)
    future = datetime(2027, 1, 1, tzinfo=timezone.utc)
    assert p.export_sessions(future) == []


def test_export_l5_is_deterministic(hermes_home: Path) -> None:
    """Idempotency: same store state -> same manifest (minus last_updated)."""
    p = HermesParticipant(hermes_home=hermes_home)
    a = p.export_l5().to_dict()
    b = p.export_l5().to_dict()
    a.pop("last_updated")
    b.pop("last_updated")
    assert a == b


# ---------------------------------------------------------------------------
# Visibility
# ---------------------------------------------------------------------------


def test_private_memory_dropped(tmp_path: Path) -> None:
    home = tmp_path / ".hermes"
    (home / "memories").mkdir(parents=True)
    # A memory line tagged via private-class keyword path: simulate by writing a
    # raw entry that, after redaction, would still be team -- so instead verify
    # the policy via a direct private tag on the entity builder.
    from participants.hermes import _memory_entity
    from participants.base import apply_visibility, filter_for_federation
    from participants.hermes import DEFAULT_POLICY

    ent = _memory_entity("memory", "some fact")
    ent.tags = ["personal"]  # private-class tag
    assert apply_visibility(ent, DEFAULT_POLICY) == Visibility.PRIVATE
    assert filter_for_federation([ent], DEFAULT_POLICY) == []


# ---------------------------------------------------------------------------
# health_check()
# ---------------------------------------------------------------------------


def test_health_blocked_when_missing(tmp_path: Path) -> None:
    p = HermesParticipant(hermes_home=tmp_path / "nope")
    hs = p.health_check()
    assert isinstance(hs, HealthStatus)
    assert hs.status == "blocked"
    assert hs.proposed_fix


def test_health_ok_with_sessions(hermes_home: Path) -> None:
    p = HermesParticipant(hermes_home=hermes_home)
    hs = p.health_check()
    assert hs.status == "ok"


def test_health_degraded_without_state_db(tmp_path: Path) -> None:
    home = tmp_path / ".hermes"
    (home / "memories").mkdir(parents=True)
    (home / "memories" / "user.md").write_text("- likes tests\n")
    p = HermesParticipant(hermes_home=home)
    hs = p.health_check()
    assert hs.status == "degraded"


def test_health_never_raises_on_corrupt_db(tmp_path: Path) -> None:
    home = tmp_path / ".hermes"
    home.mkdir()
    (home / "state.db").write_text("not a database")
    (home / "memories").mkdir()
    p = HermesParticipant(hermes_home=home)
    hs = p.health_check()  # must not raise
    assert hs.status in {"ok", "degraded", "blocked"}


# ---------------------------------------------------------------------------
# CLI handlers (hook contract: never traceback when there's nothing to publish)
# ---------------------------------------------------------------------------


def test_hermes_export_handler_returns_0_when_nothing_to_federate(tmp_path) -> None:
    """`bourdon hermes export` is wired as a SessionEnd hook; a missing/empty
    ~/.hermes makes export_l5 -> discover() raise ParticipantDiscoveryError, and
    the handler must swallow it and exit 0 (not dump a traceback)."""
    import argparse

    from cli.main import _handle_hermes_export

    empty = tmp_path / "no-hermes-here"  # never created -> no sources
    out = tmp_path / "hermes.l5.yaml"
    ns = argparse.Namespace(
        hermes_home=str(empty),
        since=None,
        access_level="team",
        out=str(out),
        print_manifest=False,
        verbose=False,
    )
    assert _handle_hermes_export(ns) == 0
    assert not out.exists()  # nothing written when there is nothing to federate


def test_hermes_doctor_handler_never_crashes_on_missing_home(tmp_path) -> None:
    import argparse

    from cli.main import _handle_hermes_doctor

    ns = argparse.Namespace(hermes_home=str(tmp_path / "no-hermes-here"), report_out=None)
    assert _handle_hermes_doctor(ns) == 0


# ---------------------------------------------------------------------------
# Unit helpers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "cwd,expected",
    [
        ("/projects/bourdon", "bourdon"),
        ("/root", None),
        ("/home", None),
        ("", None),
        (None, None),
    ],
)
def test_project_key_from_cwd(cwd, expected) -> None:
    assert _project_key_from_cwd(cwd) == expected
