"""Tests for participants.cascade_automations."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from participants.base import ParticipantDiscoveryError
from participants.cascade_automations import (
    AutomationConfig,
    AutomationRun,
    CascadeAutomationsParticipant,
    _actions_from_lines,
    _bounded,
    _build_config,
    _entities_from_configs_and_runs,
    _extract_memory_runs,
    _infer_projects,
    _infer_signals,
    _iter_configs,
    _session_from_run,
    _title_from_actions,
    default_cascade_automations_dir,
)


def _write_automation(
    base: Path, name: str, toml: str, memory: str = "",
) -> Path:
    automation_dir = base / name
    automation_dir.mkdir(parents=True, exist_ok=True)
    (automation_dir / "automation.toml").write_text(
        toml, encoding="utf-8",
    )
    if memory:
        (automation_dir / "memory.md").write_text(
            memory, encoding="utf-8",
        )
    return automation_dir


class TestHelpers:
    def test_default_dir(self):
        result = default_cascade_automations_dir()
        assert str(result).endswith("automations")

    def test_default_with_override(self, tmp_path: Path):
        result = default_cascade_automations_dir(tmp_path)
        assert result == tmp_path / "automations"

    def test_bounded_short(self):
        assert _bounded("hello", 100) == "hello"

    def test_bounded_long(self):
        result = _bounded("a" * 200, 50)
        assert result.endswith("...")
        assert len(result) < 60

    def test_infer_projects(self):
        projects = _infer_projects(
            "Working on Bourdon and ShipStable"
        )
        assert "Bourdon" in projects
        assert "ShipStable" in projects

    def test_infer_signals(self):
        signals = _infer_signals(
            "Reviewed billing drift in Stripe"
        )
        assert "billing-drift" in signals

    def test_actions_from_lines_basic(self):
        lines = ["- Setup CI", "- Fix lint", ""]
        actions = _actions_from_lines(lines)
        assert len(actions) == 2
        assert actions[0] == "Setup CI"

    def test_actions_from_lines_max_limit(self):
        lines = [f"- action {i}" for i in range(20)]
        actions = _actions_from_lines(lines)
        assert len(actions) == 6

    def test_title_from_actions(self):
        config = AutomationConfig(
            automation_id="test",
            name="Test Auto",
            status="ACTIVE",
            rrule="",
            kind="check",
            cwds=(),
            path=Path("/tmp/test/automation.toml"),
            memory_path=None,
        )
        title = _title_from_actions(
            config, ["Fixed the thing"],
        )
        assert title == "Test Auto: Fixed the thing"


class TestBuildConfig:
    def test_reads_toml(self, tmp_path: Path):
        toml = (
            'id = "daily-check"\n'
            'name = "Daily Check"\n'
            'status = "ACTIVE"\n'
            'rrule = "FREQ=DAILY"\n'
        )
        _write_automation(tmp_path, "daily-check", toml)
        config = _build_config(
            tmp_path / "daily-check" / "automation.toml",
        )
        assert config is not None
        assert config.automation_id == "daily-check"
        assert config.name == "Daily Check"
        assert config.status == "ACTIVE"

    def test_missing_id_uses_dirname(self, tmp_path: Path):
        toml = 'name = "Unnamed"\nstatus = "ACTIVE"\n'
        _write_automation(tmp_path, "my-auto", toml)
        config = _build_config(
            tmp_path / "my-auto" / "automation.toml",
        )
        assert config is not None
        assert config.automation_id == "my-auto"


class TestIterConfigs:
    def test_finds_configs(self, tmp_path: Path):
        _write_automation(
            tmp_path, "a1", 'id = "a1"\nstatus = "ACTIVE"\n',
        )
        _write_automation(
            tmp_path, "a2", 'id = "a2"\nstatus = "PAUSED"\n',
        )
        assert len(_iter_configs(tmp_path)) == 2

    def test_empty_dir(self, tmp_path: Path):
        assert _iter_configs(tmp_path) == []

    def test_nonexistent_dir(self, tmp_path: Path):
        assert _iter_configs(tmp_path / "nope") == []


class TestExtractMemoryRuns:
    def test_extracts_runs(self, tmp_path: Path):
        toml = 'id = "t"\nname = "T"\nstatus = "ACTIVE"\n'
        memory = (
            "2025-06-01\n- Checked CI\n- Fixed test\n\n"
            "2025-06-02\n- Deployed\n"
        )
        _write_automation(tmp_path, "t", toml, memory)
        config = _build_config(
            tmp_path / "t" / "automation.toml",
        )
        runs = _extract_memory_runs(config)
        assert len(runs) == 2
        assert runs[0].date == "2025-06-01"
        assert "Checked CI" in runs[0].key_actions

    def test_no_memory_file(self, tmp_path: Path):
        toml = 'id = "t"\nname = "T"\nstatus = "ACTIVE"\n'
        _write_automation(tmp_path, "t", toml)
        config = _build_config(
            tmp_path / "t" / "automation.toml",
        )
        assert _extract_memory_runs(config) == []


class TestEntitiesFromRuns:
    def test_creates_automation_entity(self, tmp_path: Path):
        config = AutomationConfig(
            automation_id="daily",
            name="Daily",
            status="ACTIVE",
            rrule="FREQ=DAILY",
            kind="check",
            cwds=(),
            path=tmp_path / "daily" / "automation.toml",
            memory_path=None,
        )
        entities = _entities_from_configs_and_runs(
            [config], [],
        )
        assert len(entities) == 1
        assert entities[0].name == "daily"
        assert entities[0].type == "automation"

    def test_infers_project_entities(self, tmp_path: Path):
        config = AutomationConfig(
            automation_id="t",
            name="T",
            status="ACTIVE",
            rrule="",
            kind="check",
            cwds=(),
            path=tmp_path / "t" / "automation.toml",
            memory_path=None,
        )
        run = AutomationRun(
            automation=config,
            date="2025-06-01",
            title="T: worked on Bourdon",
            key_actions=("worked on Bourdon",),
            projects=("Bourdon",),
            signals=(),
        )
        entities = _entities_from_configs_and_runs(
            [config], [run],
        )
        names = [e.name for e in entities]
        assert "Bourdon" in names


class TestSessionFromRun:
    def test_creates_session(self, tmp_path: Path):
        config = AutomationConfig(
            automation_id="daily",
            name="Daily",
            status="ACTIVE",
            rrule="FREQ=DAILY",
            kind="check",
            cwds=("/Users/test/project",),
            path=tmp_path / "daily" / "automation.toml",
            memory_path=(
                tmp_path / "daily" / "memory.md"
            ),
        )
        run = AutomationRun(
            automation=config,
            date="2025-06-01",
            title="Daily: did stuff",
            key_actions=("did stuff",),
            projects=(),
            signals=(),
        )
        session = _session_from_run(run)
        assert session.date == "2025-06-01"
        assert session.cwd == "/Users/test/project"


class TestCascadeAutomationsParticipant:
    def test_health_check_blocked(self, tmp_path: Path):
        p = CascadeAutomationsParticipant(
            automations_dir=tmp_path / "nonexistent",
        )
        assert p.health_check().status == "blocked"

    def test_health_check_degraded_empty(self, tmp_path: Path):
        auto_dir = tmp_path / "autos"
        auto_dir.mkdir()
        p = CascadeAutomationsParticipant(
            automations_dir=auto_dir,
        )
        assert p.health_check().status == "degraded"

    def test_health_check_ok(self, tmp_path: Path):
        auto_dir = tmp_path / "autos"
        _write_automation(
            auto_dir, "t", 'id = "t"\nstatus = "ACTIVE"\n',
        )
        p = CascadeAutomationsParticipant(
            automations_dir=auto_dir,
        )
        assert p.health_check().status == "ok"

    def test_export_l5(self, tmp_path: Path):
        auto_dir = tmp_path / "autos"
        toml = (
            'id = "daily"\nname = "Daily"\n'
            'status = "ACTIVE"\nrrule = "FREQ=DAILY"\n'
        )
        memory = "2025-06-01\n- Checked Bourdon\n"
        _write_automation(auto_dir, "daily", toml, memory)
        p = CascadeAutomationsParticipant(
            automations_dir=auto_dir,
        )
        manifest = p.export_l5()
        assert manifest.agent.id == "cascade-automations"
        assert len(manifest.recent_sessions) >= 1
        assert len(manifest.known_entities) >= 1

    def test_discover(self, tmp_path: Path):
        auto_dir = tmp_path / "autos"
        _write_automation(
            auto_dir, "t", 'id = "t"\nstatus = "ACTIVE"\n',
        )
        p = CascadeAutomationsParticipant(
            automations_dir=auto_dir,
        )
        store = p.discover()
        assert store.metadata["automations"] == 1

    def test_discover_missing(self, tmp_path: Path):
        p = CascadeAutomationsParticipant(
            automations_dir=tmp_path / "nope",
        )
        with pytest.raises(ParticipantDiscoveryError):
            p.discover()

    def test_export_sessions_since(self, tmp_path: Path):
        auto_dir = tmp_path / "autos"
        memory = (
            "2025-01-01\n- Old\n\n"
            "2025-06-01\n- Recent\n"
        )
        _write_automation(
            auto_dir, "t",
            'id = "t"\nstatus = "ACTIVE"\n',
            memory,
        )
        p = CascadeAutomationsParticipant(
            automations_dir=auto_dir,
        )
        since = datetime(2025, 3, 1, tzinfo=timezone.utc)
        sessions = p.export_sessions(since=since)
        dates = [s.date for s in sessions]
        assert "2025-06-01" in dates
        assert "2025-01-01" not in dates
