"""Tests for adapters.cursor_automations."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from adapters.base import AdapterDiscoveryError, L5Manifest
from adapters.cursor_automations import (
    AGENT_ID,
    AGENT_TYPE,
    CursorAutomationsAdapter,
    _build_config,
    _extract_memory_runs,
    _iter_configs,
    default_cursor_automations_dir,
)

# ---- Helpers ----------------------------------------------------------------


def _make_automation(tmp_path: Path, automation_id: str, *, memory: str = "") -> Path:
    """Seed a Cursor automation directory with toml + optional memory.md."""
    auto_dir = tmp_path / automation_id
    auto_dir.mkdir(parents=True, exist_ok=True)
    toml = auto_dir / "automation.toml"
    toml.write_text(
        f'id = "{automation_id}"\nname = "{automation_id}"\nstatus = "ACTIVE"\n'
        f'rrule = "FREQ=DAILY"\nkind = "monitor"\ncwds = ["/workspace/bourdon"]\n',
        encoding="utf-8",
    )
    if memory:
        (auto_dir / "memory.md").write_text(memory, encoding="utf-8")
    return auto_dir


# ---- default_cursor_automations_dir ----------------------------------------


def test_default_dir_uses_cursor_home():
    d = default_cursor_automations_dir(cursor_home=Path("/custom"))
    assert d == Path("/custom/automations")


def test_default_dir_uses_env(monkeypatch):
    monkeypatch.setenv("CURSOR_DIR", "/env-cursor")
    d = default_cursor_automations_dir()
    assert d == Path("/env-cursor/automations")


def test_default_dir_falls_back_to_home():
    d = default_cursor_automations_dir()
    assert str(d).endswith(".cursor/automations")


# ---- discover() ------------------------------------------------------------


def test_discover_raises_when_dir_missing(tmp_path):
    adapter = CursorAutomationsAdapter(automations_dir=tmp_path / "nope")
    with pytest.raises(AdapterDiscoveryError):
        adapter.discover()


def test_discover_returns_store_when_dir_exists(tmp_path):
    automations_dir = tmp_path / "automations"
    _make_automation(automations_dir, "pr-audit")
    adapter = CursorAutomationsAdapter(automations_dir=automations_dir)
    store = adapter.discover()
    assert store.path == str(automations_dir)
    assert store.metadata["automations"] == 1


# ---- _build_config / _iter_configs ----------------------------------------


def test_build_config_reads_toml(tmp_path):
    automations_dir = tmp_path / "automations"
    _make_automation(automations_dir, "weekly-review")
    configs = _iter_configs(automations_dir)
    assert len(configs) == 1
    assert configs[0].automation_id == "weekly-review"
    assert configs[0].status == "ACTIVE"
    assert configs[0].cwds == ("/workspace/bourdon",)


def test_build_config_missing_toml(tmp_path):
    config = _build_config(tmp_path / "nonexistent" / "automation.toml")
    assert config is not None
    assert config.automation_id == "nonexistent"


# ---- _extract_memory_runs --------------------------------------------------


def test_extract_memory_runs_parses_dated_sections(tmp_path):
    automations_dir = tmp_path / "automations"
    _make_automation(
        automations_dir,
        "pr-audit",
        memory=(
            "2026-06-01\n"
            "- Reviewed 3 open PRs across ILTT and Bourdon.\n"
            "- No critical issues found.\n"
            "\n"
            "2026-06-02\n"
            "- Ran memory coverage check.\n"
        ),
    )
    configs = _iter_configs(automations_dir)
    runs = _extract_memory_runs(configs[0])
    assert len(runs) == 2
    assert runs[0].date == "2026-06-01"
    assert len(runs[0].key_actions) == 2
    assert "ILTT" in runs[0].projects
    assert "Bourdon" in runs[0].projects


def test_extract_memory_runs_skips_runtime_lines(tmp_path):
    automations_dir = tmp_path / "automations"
    _make_automation(
        automations_dir,
        "check",
        memory="2026-06-01\n- Did work\nRuntime: 5m\nfirst run\n- Second action\n",
    )
    configs = _iter_configs(automations_dir)
    runs = _extract_memory_runs(configs[0])
    assert len(runs) == 1
    assert len(runs[0].key_actions) == 2
    assert all("Runtime" not in a for a in runs[0].key_actions)


def test_extract_memory_runs_detects_signals(tmp_path):
    automations_dir = tmp_path / "automations"
    _make_automation(
        automations_dir,
        "billing-check",
        memory="2026-06-03\n- Checked Stripe billing integration status.\n",
    )
    configs = _iter_configs(automations_dir)
    runs = _extract_memory_runs(configs[0])
    assert len(runs) == 1
    assert "billing-drift" in runs[0].signals


# ---- export_l5() -----------------------------------------------------------


def test_export_l5_empty_when_no_automations(tmp_path):
    automations_dir = tmp_path / "automations"
    automations_dir.mkdir()
    adapter = CursorAutomationsAdapter(automations_dir=automations_dir)
    manifest = adapter.export_l5()
    assert isinstance(manifest, L5Manifest)
    assert manifest.agent.id == AGENT_ID
    assert manifest.agent.type == AGENT_TYPE
    assert manifest.recent_sessions == []
    assert manifest.known_entities == []


def test_export_l5_produces_sessions_and_entities(tmp_path):
    automations_dir = tmp_path / "automations"
    _make_automation(
        automations_dir,
        "weekly-review",
        memory="2026-06-01\n- Ran weekly PR audit across Bourdon repos.\n",
    )
    adapter = CursorAutomationsAdapter(automations_dir=automations_dir)
    manifest = adapter.export_l5()

    assert len(manifest.recent_sessions) >= 1
    session = manifest.recent_sessions[0]
    assert "automation_id: weekly-review" in session.key_actions

    entity_names = {e.name for e in manifest.known_entities}
    assert "weekly-review" in entity_names
    assert "Bourdon" in entity_names


def test_export_l5_filters_by_since(tmp_path):
    automations_dir = tmp_path / "automations"
    _make_automation(
        automations_dir,
        "monitor",
        memory="2025-01-01\n- Old run.\n\n2026-06-01\n- New run.\n",
    )
    adapter = CursorAutomationsAdapter(automations_dir=automations_dir)
    cutoff = datetime(2026, 1, 1, tzinfo=timezone.utc)
    manifest = adapter.export_l5(since=cutoff)
    dates = [s.date for s in manifest.recent_sessions]
    assert all(d >= "2026-01-01" for d in dates if d)
    assert len(manifest.recent_sessions) == 1


def test_export_l5_redacts_credentials(tmp_path):
    automations_dir = tmp_path / "automations"
    _make_automation(
        automations_dir,
        "creds-test",
        memory="2026-06-01\n- Found leaked api_key in config.\n",
    )
    adapter = CursorAutomationsAdapter(automations_dir=automations_dir)
    manifest = adapter.export_l5()
    for session in manifest.recent_sessions:
        for action in session.key_actions:
            assert "api_key" not in action.lower() or "redacted" in action.lower()


# ---- export_sessions() -----------------------------------------------------


def test_export_sessions_respects_limit(tmp_path):
    automations_dir = tmp_path / "automations"
    memory = "\n\n".join(
        f"2026-06-{i + 1:02d}\n- Run {i}" for i in range(10)
    )
    _make_automation(automations_dir, "many-runs", memory=memory)
    adapter = CursorAutomationsAdapter(automations_dir=automations_dir)
    sessions = adapter.export_sessions(
        since=datetime(2020, 1, 1, tzinfo=timezone.utc), limit=3
    )
    assert len(sessions) == 3


# ---- health_check() --------------------------------------------------------


def test_health_check_blocked_when_dir_missing(tmp_path):
    adapter = CursorAutomationsAdapter(automations_dir=tmp_path / "missing")
    health = adapter.health_check()
    assert health.status == "blocked"
    assert health.proposed_fix is not None


def test_health_check_degraded_when_no_automations(tmp_path):
    automations_dir = tmp_path / "automations"
    automations_dir.mkdir()
    adapter = CursorAutomationsAdapter(automations_dir=automations_dir)
    health = adapter.health_check()
    assert health.status == "degraded"
    assert "No automation.toml" in (health.reason or "")


def test_health_check_ok_when_automations_present(tmp_path):
    automations_dir = tmp_path / "automations"
    _make_automation(automations_dir, "daily-check")
    adapter = CursorAutomationsAdapter(automations_dir=automations_dir)
    health = adapter.health_check()
    assert health.status == "ok"
    assert health.details["automation_count"] == 1
    assert health.details["active_automations"] == 1


# ---- Protocol conformance ---------------------------------------------------


def test_class_attrs():
    assert CursorAutomationsAdapter.agent_id == "cursor-automations"
    assert CursorAutomationsAdapter.agent_type == "other"


def test_native_path_resolves(tmp_path):
    adapter = CursorAutomationsAdapter(automations_dir=tmp_path / "automations")
    assert adapter.native_path == str(tmp_path / "automations")
