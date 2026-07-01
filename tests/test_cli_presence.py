"""Hook-safety tests for the `bourdon presence` register/heartbeat/deregister
verbs. These run as Claude Code / Codex hooks, so they must NEVER raise and
NEVER exit non-zero — a failing presence hook must not block a user's turn or
spew a traceback into the model's context."""

from __future__ import annotations

import argparse

import pytest

from cli import main as cli_main
from core import presence


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("BOURDON_HOME", str(tmp_path / "dot-bourdon"))
    monkeypatch.delenv("BOURDON_SESSION_ID", raising=False)
    monkeypatch.delenv("TERM_SESSION_ID", raising=False)
    monkeypatch.setattr(cli_main, "_hook_stdin", lambda: {})
    yield


def _ns(**kw):
    kw.setdefault("agent", "codex")
    kw.setdefault("session", None)
    kw.setdefault("cwd", None)
    return argparse.Namespace(**kw)


def test_missing_session_is_silent_noop():
    # No session id from flags, stdin, or env → exit 0, nothing registered.
    for handler in (
        cli_main._handle_presence_register,
        cli_main._handle_presence_heartbeat,
        cli_main._handle_presence_deregister,
    ):
        assert handler(_ns()) == 0
    assert presence.live_sessions() == []


def test_presence_layer_failure_is_swallowed(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(presence, "register", boom)
    monkeypatch.setattr(presence, "heartbeat", boom)
    monkeypatch.setattr(presence, "deregister", boom)

    ns = _ns(session="s1", cwd="/x/bourdon")
    assert cli_main._handle_presence_register(ns) == 0
    assert cli_main._handle_presence_heartbeat(ns) == 0
    assert cli_main._handle_presence_deregister(ns) == 0


def test_env_fallback_resolves_session(monkeypatch):
    monkeypatch.setenv("TERM_SESSION_ID", "TAB-XYZ")
    monkeypatch.setenv("PWD", "/Users/x/repos/ILTT")
    # session="" (as the codex hook passes when $TERM_SESSION_ID is empty) still
    # resolves via the env fallback.
    assert cli_main._handle_presence_register(_ns(session="")) == 0
    (session,) = presence.live_sessions()
    assert session["instance"] == "TAB-XYZ"[:8]
    assert session["project"] == "ILTT"


def test_bourdon_session_id_takes_precedence(monkeypatch):
    monkeypatch.setenv("BOURDON_SESSION_ID", "explicit-id")
    monkeypatch.setenv("TERM_SESSION_ID", "term-id")
    assert cli_main._handle_presence_register(_ns(cwd="/x/bourdon")) == 0
    (session,) = presence.live_sessions()
    assert session["instance"] == "explicit"  # "explicit-id"[:8]
