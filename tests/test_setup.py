"""Tests for the ``bourdon setup`` wizard (cli.setup)."""

from __future__ import annotations

import argparse
import io
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from cli.setup import (
    AgentDetection,
    SetupChoices,
    SetupOutcome,
    apply_choices,
    claude_code_settings_path,
    collect_choices_interactive,
    detect_agents,
    ensure_agent_library,
    handle_setup,
    init_cascade_memory_if_missing,
    init_copilot_memory_if_missing,
    is_claude_code_hook_wired,
    render_detection,
    render_outcome,
    resolve_bourdon_binary,
    wire_claude_code_hook,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    """Make Path.home() return a fresh tmp dir so tests don't touch real ~/."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    return home


# ---------------------------------------------------------------------------
# detect_agents
# ---------------------------------------------------------------------------


def test_detect_agents_reports_all_absent(fake_home):
    out = detect_agents(home=fake_home)
    ids = [a.id for a in out]
    # Sourced from participants.discover_participants(), sorted by agent id, with
    # the -automations sub-surfaces excluded (the wizard wires the parent agent).
    assert ids == [
        "cascade",
        "claude-code",
        "claude-desktop-code",
        "claude-desktop-cowork",
        "codex",
        "copilot",
        "copilot-cli",
        "copilot-vscode",
        "cursor",
        "openclaw",
    ]
    assert all(not a.present for a in out)


def test_detect_agents_reports_present_when_paths_exist(fake_home):
    (fake_home / ".claude").mkdir()
    (fake_home / ".codex").mkdir()
    out = detect_agents(home=fake_home)
    presence = {a.id: a.present for a in out}
    assert presence == {
        "claude-code": True,
        "claude-desktop-code": False,
        "claude-desktop-cowork": False,
        "codex": True,
        "cursor": False,
        "copilot": False,
        "copilot-cli": False,
        "copilot-vscode": False,
        "cascade": False,
        "openclaw": False,
    }


def test_detect_agents_ordering_is_stable(fake_home):
    out = detect_agents(home=fake_home)
    # Deterministic order = agent ids sorted ascending (single source of truth is
    # the package scan, so there is no hand-curated ordering to drift).
    assert [a.id for a in out] == [
        "cascade",
        "claude-code",
        "claude-desktop-code",
        "claude-desktop-cowork",
        "codex",
        "copilot",
        "copilot-cli",
        "copilot-vscode",
        "cursor",
        "openclaw",
    ]


# ---------------------------------------------------------------------------
# ensure_agent_library
# ---------------------------------------------------------------------------


def test_ensure_agent_library_creates_if_missing(tmp_path):
    lib = tmp_path / "agent-library"
    assert ensure_agent_library(lib) is True
    assert (lib / "agents").is_dir()


def test_ensure_agent_library_returns_false_when_present(tmp_path):
    lib = tmp_path / "agent-library"
    (lib / "agents").mkdir(parents=True)
    assert ensure_agent_library(lib) is False


def test_ensure_agent_library_dry_run_does_not_create(tmp_path):
    lib = tmp_path / "agent-library"
    assert ensure_agent_library(lib, dry_run=True) is True
    assert not lib.exists()


# ---------------------------------------------------------------------------
# Claude Code SessionEnd hook
# ---------------------------------------------------------------------------


def test_wire_claude_code_hook_creates_settings_with_hook(fake_home):
    settings = claude_code_settings_path(home=fake_home)
    assert not settings.exists()
    assert wire_claude_code_hook(settings, "bourdon") is True
    assert settings.is_file()
    data = json.loads(settings.read_text(encoding="utf-8"))
    entries = data["hooks"]["SessionEnd"]
    assert isinstance(entries, list)
    assert any(
        h.get("command", "").endswith("claude-code export")
        for entry in entries
        for h in entry.get("hooks", [])
    )


def test_wire_claude_code_hook_is_idempotent(fake_home):
    settings = claude_code_settings_path(home=fake_home)
    assert wire_claude_code_hook(settings, "bourdon") is True
    # Second call should be a no-op.
    assert wire_claude_code_hook(settings, "bourdon") is False
    data = json.loads(settings.read_text(encoding="utf-8"))
    assert len(data["hooks"]["SessionEnd"]) == 1


def test_wire_claude_code_hook_preserves_existing_unrelated_hooks(fake_home):
    settings = claude_code_settings_path(home=fake_home)
    settings.parent.mkdir(parents=True)
    settings.write_text(
        json.dumps(
            {
                "hooks": {
                    "SessionStart": [
                        {
                            "matcher": "*",
                            "hooks": [{"type": "command", "command": "echo start"}],
                        }
                    ],
                    "SessionEnd": [
                        {
                            "matcher": "*",
                            "hooks": [{"type": "command", "command": "echo other"}],
                        }
                    ],
                }
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    assert wire_claude_code_hook(settings, "bourdon") is True
    data = json.loads(settings.read_text(encoding="utf-8"))
    # SessionStart untouched.
    assert data["hooks"]["SessionStart"][0]["hooks"][0]["command"] == "echo start"
    # SessionEnd has the existing "echo other" AND the new bourdon entry.
    session_end_commands = [
        h["command"]
        for entry in data["hooks"]["SessionEnd"]
        for h in entry["hooks"]
    ]
    assert "echo other" in session_end_commands
    assert any("claude-code export" in c for c in session_end_commands)


def test_wire_claude_code_hook_dry_run_does_not_write(fake_home):
    settings = claude_code_settings_path(home=fake_home)
    assert wire_claude_code_hook(settings, "bourdon", dry_run=True) is True
    assert not settings.exists()


def test_is_claude_code_hook_wired_false_when_settings_missing(fake_home):
    settings = claude_code_settings_path(home=fake_home)
    assert is_claude_code_hook_wired(settings) is False


def test_is_claude_code_hook_wired_handles_malformed_json(fake_home):
    settings = claude_code_settings_path(home=fake_home)
    settings.parent.mkdir(parents=True)
    settings.write_text("{not json", encoding="utf-8")
    assert is_claude_code_hook_wired(settings) is False


def test_is_claude_code_hook_wired_handles_non_dict_root(fake_home):
    settings = claude_code_settings_path(home=fake_home)
    settings.parent.mkdir(parents=True)
    settings.write_text("[]", encoding="utf-8")
    assert is_claude_code_hook_wired(settings) is False


@pytest.mark.parametrize(
    "command",
    [
        "bourdon claude-code export",  # bare name (Unix-ish)
        "/usr/local/bin/bourdon claude-code export",  # absolute Unix
        r"C:\Users\runner\AppData\Local\Programs\Python\Python312\Scripts\bourdon.exe claude-code export",  # Windows .exe path
        "bourdon.exe claude-code export",  # bare Windows
    ],
)
def test_is_claude_code_hook_wired_recognizes_path_variants(fake_home, command):
    """Issue: Windows path .exe broke exact-substring marker (#88 fix).

    Confirm the detector picks up the hook regardless of how the binary is
    spelled at the front of the command string.
    """
    settings = claude_code_settings_path(home=fake_home)
    settings.parent.mkdir(parents=True)
    settings.write_text(
        json.dumps(
            {
                "hooks": {
                    "SessionEnd": [
                        {
                            "matcher": "*",
                            "hooks": [{"type": "command", "command": command}],
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    assert is_claude_code_hook_wired(settings) is True


# ---------------------------------------------------------------------------
# resolve_bourdon_binary
# ---------------------------------------------------------------------------


def test_resolve_bourdon_binary_returns_absolute_when_on_path(monkeypatch):
    monkeypatch.setattr("cli.setup.shutil.which", lambda name: "/usr/local/bin/bourdon")
    assert resolve_bourdon_binary() == "/usr/local/bin/bourdon"


def test_resolve_bourdon_binary_falls_back_to_bare_name(monkeypatch):
    monkeypatch.setattr("cli.setup.shutil.which", lambda name: None)
    assert resolve_bourdon_binary() == "bourdon"


# ---------------------------------------------------------------------------
# apply_choices
# ---------------------------------------------------------------------------


def test_apply_choices_creates_library_only_when_no_agents(fake_home, monkeypatch):
    monkeypatch.setattr("cli.setup._run_bourdon_subprocess", lambda *a, **k: True)
    detected = detect_agents(home=fake_home)
    choices = SetupChoices(
        library_path=fake_home / "agent-library",
        wire_claude_code_hook=False,
        init_copilot=False,
        init_cascade=False,
        run_codex_sync=False,
        run_initial_export=False,
    )
    outcome = apply_choices(detected, choices, home=fake_home)
    assert outcome.library_created is True
    assert outcome.claude_code_hook_wired is None  # skipped (no claude-code)
    assert outcome.codex_sync_ran is None
    assert outcome.copilot_init is None


def test_apply_choices_wires_claude_code_hook_when_present(fake_home, monkeypatch):
    monkeypatch.setattr("cli.setup._run_bourdon_subprocess", lambda *a, **k: True)
    (fake_home / ".claude").mkdir()
    detected = detect_agents(home=fake_home)
    choices = SetupChoices(
        library_path=fake_home / "agent-library",
        wire_claude_code_hook=True,
        run_codex_sync=False,
        run_initial_export=False,
    )
    outcome = apply_choices(detected, choices, home=fake_home)
    assert outcome.claude_code_hook_wired is True
    settings = claude_code_settings_path(home=fake_home)
    assert is_claude_code_hook_wired(settings)


def test_apply_choices_runs_codex_sync_when_codex_present(fake_home, monkeypatch):
    (fake_home / ".codex").mkdir()
    detected = detect_agents(home=fake_home)
    calls = []

    def fake_subprocess(argv, **kwargs):
        calls.append(list(argv))
        return True

    monkeypatch.setattr("cli.setup._run_bourdon_subprocess", fake_subprocess)
    choices = SetupChoices(
        library_path=fake_home / "agent-library",
        wire_claude_code_hook=False,
        run_codex_sync=True,
        run_initial_export=False,
    )
    outcome = apply_choices(detected, choices, home=fake_home)
    assert outcome.codex_sync_ran is True
    # The codex sync-native --from-library call was invoked.
    assert any(
        "codex" in c and "sync-native" in c and "--from-library" in c for c in calls
    )


def test_apply_choices_dry_run_does_not_shell_out(fake_home, monkeypatch):
    (fake_home / ".codex").mkdir()
    detected = detect_agents(home=fake_home)
    monkeypatch.setattr(
        "cli.setup._run_bourdon_subprocess",
        lambda *a, **k: pytest.fail("subprocess should not run in dry-run"),
    )
    choices = SetupChoices(
        library_path=fake_home / "agent-library",
        wire_claude_code_hook=False,
        run_codex_sync=True,
        run_initial_export=True,
    )
    outcome = apply_choices(detected, choices, home=fake_home, dry_run=True)
    assert outcome.codex_sync_ran is True
    assert outcome.initial_export_ran is True
    # Library wasn't actually created in dry-run.
    assert not (fake_home / "agent-library").exists()


def test_apply_choices_skips_codex_sync_when_not_detected(fake_home, monkeypatch):
    detected = detect_agents(home=fake_home)
    called = []
    monkeypatch.setattr(
        "cli.setup._run_bourdon_subprocess", lambda *a, **k: called.append(1) or True
    )
    choices = SetupChoices(
        library_path=fake_home / "agent-library",
        wire_claude_code_hook=False,
        run_codex_sync=True,
        run_initial_export=False,
    )
    outcome = apply_choices(detected, choices, home=fake_home)
    assert outcome.codex_sync_ran is None
    assert not called


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def test_render_detection_lists_each_agent(fake_home):
    (fake_home / ".claude").mkdir()
    detected = detect_agents(home=fake_home)
    out = io.StringIO()
    render_detection(detected, stream=out)
    text = out.getvalue()
    assert "Claude Code" in text
    assert "[OK]" in text
    assert "Cursor" in text  # also appears (not present)


def test_render_outcome_shows_next_steps(fake_home):
    detected = detect_agents(home=fake_home)
    outcome = SetupOutcome(detected=detected, library_created=True)
    out = io.StringIO()
    render_outcome(outcome, stream=out)
    text = out.getvalue()
    assert "Next steps" in text
    assert "bourdon doctor" in text
    assert "bourdon export-all" in text
    assert "bourdon sync push" in text


# ---------------------------------------------------------------------------
# Interactive prompts
# ---------------------------------------------------------------------------


def test_collect_choices_interactive_uses_defaults_on_empty(fake_home, monkeypatch):
    (fake_home / ".claude").mkdir()
    (fake_home / ".codex").mkdir()
    detected = detect_agents(home=fake_home)
    monkeypatch.setattr("builtins.input", lambda: "")
    choices = collect_choices_interactive(
        detected,
        default_library=fake_home / "agent-library",
        stream=io.StringIO(),
    )
    assert choices.library_path == fake_home / "agent-library"
    assert choices.wire_claude_code_hook is True
    assert choices.run_codex_sync is True
    # Copilot/Cascade defaults are False for absent agents.
    assert choices.init_copilot is False
    assert choices.init_cascade is False


def test_collect_choices_interactive_user_can_decline_hook(fake_home, monkeypatch):
    (fake_home / ".claude").mkdir()
    answers = iter(["", "n", "", "", ""])  # path default, decline hook, defaults
    monkeypatch.setattr("builtins.input", lambda: next(answers))
    detected = detect_agents(home=fake_home)
    choices = collect_choices_interactive(
        detected,
        default_library=fake_home / "agent-library",
        stream=io.StringIO(),
    )
    assert choices.wire_claude_code_hook is False


# ---------------------------------------------------------------------------
# End-to-end via handle_setup (non-interactive)
# ---------------------------------------------------------------------------


def test_handle_setup_non_interactive_dry_run_does_not_touch_fs(fake_home, monkeypatch):
    (fake_home / ".claude").mkdir()
    monkeypatch.setattr(
        "cli.setup._run_bourdon_subprocess",
        lambda *a, **k: pytest.fail("no subprocess in dry-run"),
    )
    args = argparse.Namespace(
        library_path=str(fake_home / "agent-library"),
        non_interactive=True,
        dry_run=True,
    )
    with patch("sys.stdout", new=io.StringIO()):
        rc = handle_setup(args)
    assert rc == 0
    settings = claude_code_settings_path(home=fake_home)
    assert not settings.exists()
    assert not (fake_home / "agent-library").exists()


def test_handle_setup_non_interactive_wires_real_fs(fake_home, monkeypatch):
    (fake_home / ".claude").mkdir()
    monkeypatch.setattr("cli.setup._run_bourdon_subprocess", lambda *a, **k: True)
    args = argparse.Namespace(
        library_path=str(fake_home / "agent-library"),
        non_interactive=True,
        dry_run=False,
    )
    with patch("sys.stdout", new=io.StringIO()):
        rc = handle_setup(args)
    assert rc == 0
    assert (fake_home / "agent-library" / "agents").is_dir()
    settings = claude_code_settings_path(home=fake_home)
    assert is_claude_code_hook_wired(settings)
