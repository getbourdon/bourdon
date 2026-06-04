"""Tests for adapters.codex_automations."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from adapters.base import BourdonAdapter, Visibility
from adapters.codex_automations import (
    AGENT_ID,
    AGENT_TYPE,
    CodexAutomationsAdapter,
    _build_config,
    _extract_memory_runs,
)


def _write_automation(
    root: Path,
    automation_id: str = "radlab-mission-control-brief",
    memory: str | None = None,
) -> Path:
    automation_dir = root / automation_id
    automation_dir.mkdir(parents=True)
    (automation_dir / "automation.toml").write_text(
        f"""\
version = 1
id = "{automation_id}"
kind = "cron"
name = "Mission Control Brief"
status = "ACTIVE"
rrule = "FREQ=WEEKLY;BYDAY=MO"
cwds = ["/Users/radman"]
""",
        encoding="utf-8",
    )
    if memory is not None:
        (automation_dir / "memory.md").write_text(memory, encoding="utf-8")
    return automation_dir


def test_adapter_satisfies_protocol(tmp_path):
    _write_automation(tmp_path, memory="2026-06-03\n- ShipStable launch gate found\n")

    adapter = CodexAutomationsAdapter(automations_dir=tmp_path)

    assert isinstance(adapter, BourdonAdapter)
    assert adapter.agent_id == AGENT_ID
    assert adapter.agent_type == AGENT_TYPE


def test_build_config_reads_toml_and_memory_path(tmp_path):
    automation_dir = _write_automation(tmp_path, memory="2026-06-03\n- Run summary\n")

    config = _build_config(automation_dir / "automation.toml")

    assert config is not None
    assert config.automation_id == "radlab-mission-control-brief"
    assert config.name == "Mission Control Brief"
    assert config.status == "ACTIVE"
    assert config.cwds == ("/Users/radman",)
    assert config.memory_path == automation_dir / "memory.md"


def test_extract_memory_runs_from_dated_sections(tmp_path):
    automation_dir = _write_automation(
        tmp_path,
        memory="""\
2026-06-02 run: created first pass report
- ShipStable live purchase verified.
- Runtime: ~8 minutes.

2026-06-03
- ILTT remains blocked on RevenueCat dashboard setup.
- Federated memory manifests lag current automation evidence.
""",
    )
    config = _build_config(automation_dir / "automation.toml")
    assert config is not None

    runs = _extract_memory_runs(config)

    assert [run.date for run in runs] == ["2026-06-02", "2026-06-03"]
    assert "created first pass report" in runs[0].key_actions[0]
    assert "Runtime" not in " ".join(runs[0].key_actions)
    assert "ShipStable" in runs[0].projects
    assert "ILTT" in runs[1].projects
    assert "billing-drift" in runs[1].signals
    assert "memory-coverage-gap" in runs[1].signals


def test_export_l5_emits_automation_sessions_and_entities(tmp_path):
    _write_automation(
        tmp_path,
        memory="""\
2026-06-03
- ShipStable launch gates are now human dashboard actions.
- Bourdon needs a codex-automations L5 publisher.
""",
    )

    manifest = CodexAutomationsAdapter(automations_dir=tmp_path).export_l5()
    data = manifest.to_dict()

    assert data["agent"]["id"] == "codex-automations"
    assert data["agent"]["type"] == "other"
    assert data["recent_sessions"][0]["date"] == "2026-06-03"
    assert data["recent_sessions"][0]["cwd"] == "/Users/radman"
    assert (
        "automation_id: radlab-mission-control-brief"
        in data["recent_sessions"][0]["key_actions"]
    )
    entity_names = {entity["name"] for entity in data["known_entities"]}
    assert "radlab-mission-control-brief" in entity_names
    assert "ShipStable" in entity_names
    assert "human-dashboard-action" in entity_names
    assert "launch-decision" in entity_names


def test_export_sessions_filters_since(tmp_path):
    _write_automation(
        tmp_path,
        memory="""\
2026-06-01
- Old report.

2026-06-03
- Fresh ShipStable report.
""",
    )

    sessions = CodexAutomationsAdapter(automations_dir=tmp_path).export_sessions(
        since=datetime(2026, 6, 2, tzinfo=timezone.utc)
    )

    assert [session.date for session in sessions] == ["2026-06-03"]


def test_health_check_reports_blocked_missing_dir(tmp_path):
    adapter = CodexAutomationsAdapter(automations_dir=tmp_path / "missing")

    health = adapter.health_check()

    assert health.status == "blocked"
    assert "automations directory not found" in (health.reason or "").lower()


def test_health_check_counts_runs(tmp_path):
    _write_automation(tmp_path, memory="2026-06-03\n- ShipStable launch report.\n")

    health = CodexAutomationsAdapter(automations_dir=tmp_path).health_check()

    assert health.status == "ok"
    assert health.details["automation_count"] == 1
    assert health.details["memory_files"] == 1
    assert health.details["runs_extracted"] == 1
    assert health.details["active_automations"] == 1


def test_redacts_secret_words_in_run_actions(tmp_path):
    _write_automation(
        tmp_path,
        memory="2026-06-03\n- Found api_key in the automation note.\n",
    )

    manifest = CodexAutomationsAdapter(automations_dir=tmp_path).export_l5()
    action_text = " ".join(manifest.recent_sessions[0].key_actions)

    assert "api_key" not in action_text
    assert "redacted" in action_text.lower()
    assert manifest.recent_sessions[0].visibility == Visibility.TEAM
