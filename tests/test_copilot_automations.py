"""Tests for participants.copilot_automations — Copilot Automations participant."""

from datetime import datetime, timezone
from pathlib import Path

import pytest

from participants.copilot_automations import (
    CopilotAutomationsParticipant,
    AutomationConfig,
    AutomationRun,
    default_copilot_automations_dir,
    init_automation,
    _build_config,
    _extract_memory_runs,
    _iter_configs,
    _bounded,
)
from participants.base import ParticipantDiscoveryError


@pytest.fixture
def fake_automations_dir(tmp_path):
    """Create a fake automations directory with one automation."""
    auto_dir = tmp_path / "automations" / "pr-review-bot"
    auto_dir.mkdir(parents=True)

    toml_path = auto_dir / "automation.toml"
    toml_path.write_text(
        'id = "pr-review-bot"\n'
        'name = "PR Review Bot"\n'
        'status = "ACTIVE"\n'
        'trigger = "pull_request"\n'
        'kind = "pr-review"\n'
        'repos = ["RADLABtech/iltt-app", "RADLABtech/prun"]\n',
        encoding="utf-8",
    )

    memory_path = auto_dir / "memory.md"
    memory_path.write_text(
        "# PR Review Bot — Run Memory\n\n"
        "2026-06-01 Weekly PR sweep\n"
        "- Reviewed 5 PRs across iltt-app\n"
        "- Flagged 2 security issues in auth module\n"
        "- Approved 3 clean PRs\n\n"
        "2026-06-04 Hotfix review\n"
        "- Emergency PR for payment gateway timeout\n"
        "- Approved with condition: add retry logic test\n",
        encoding="utf-8",
    )

    return tmp_path / "automations"


@pytest.fixture
def fake_automations_dir_empty(tmp_path):
    """Automations dir exists but has no automation.toml files."""
    auto_dir = tmp_path / "automations"
    auto_dir.mkdir(parents=True)
    return auto_dir


class TestCopilotAutomationsParticipant:
    def test_discover_success(self, fake_automations_dir):
        participant = CopilotAutomationsParticipant(automations_dir=fake_automations_dir)
        store = participant.discover()
        assert store.metadata["automations"] == 1
        assert store.metadata["with_memory"] == 1

    def test_discover_missing_dir(self, tmp_path):
        participant = CopilotAutomationsParticipant(automations_dir=tmp_path / "nope")
        with pytest.raises(ParticipantDiscoveryError):
            participant.discover()

    def test_export_sessions(self, fake_automations_dir):
        participant = CopilotAutomationsParticipant(automations_dir=fake_automations_dir)
        since = datetime(2026, 5, 1, tzinfo=timezone.utc)
        sessions = participant.export_sessions(since=since)
        assert len(sessions) == 2
        # Most recent first
        assert sessions[0].date == "2026-06-04"
        assert sessions[1].date == "2026-06-01"

    def test_export_sessions_since_filter(self, fake_automations_dir):
        participant = CopilotAutomationsParticipant(automations_dir=fake_automations_dir)
        since = datetime(2026, 6, 3, tzinfo=timezone.utc)
        sessions = participant.export_sessions(since=since)
        assert len(sessions) == 1
        assert sessions[0].date == "2026-06-04"

    def test_export_l5_manifest(self, fake_automations_dir):
        participant = CopilotAutomationsParticipant(automations_dir=fake_automations_dir)
        manifest = participant.export_l5()
        assert manifest.agent.id == "copilot-automations"
        assert manifest.agent.type == "other"
        assert manifest.agent.role_narrative is not None
        assert len(manifest.recent_sessions) == 2
        assert len(manifest.known_entities) >= 1

    def test_export_l5_entities(self, fake_automations_dir):
        participant = CopilotAutomationsParticipant(automations_dir=fake_automations_dir)
        manifest = participant.export_l5()
        entity_names = [e.name for e in manifest.known_entities]
        assert "pr-review-bot" in entity_names
        # Repos from config
        assert "RADLABtech/iltt-app" in entity_names
        assert "RADLABtech/prun" in entity_names

    def test_session_includes_trigger(self, fake_automations_dir):
        participant = CopilotAutomationsParticipant(automations_dir=fake_automations_dir)
        sessions = participant.export_sessions(since=datetime(2026, 5, 1, tzinfo=timezone.utc))
        actions = " ".join(sessions[0].key_actions)
        assert "trigger: pull_request" in actions

    def test_health_check_ok(self, fake_automations_dir):
        participant = CopilotAutomationsParticipant(automations_dir=fake_automations_dir)
        health = participant.health_check()
        assert health.status == "ok"
        assert health.details["automation_count"] == 1
        assert health.details["runs_extracted"] == 2

    def test_health_check_blocked(self, tmp_path):
        participant = CopilotAutomationsParticipant(automations_dir=tmp_path / "nope")
        health = participant.health_check()
        assert health.status == "blocked"
        assert health.proposed_fix is not None

    def test_health_check_degraded_empty(self, fake_automations_dir_empty):
        participant = CopilotAutomationsParticipant(automations_dir=fake_automations_dir_empty)
        health = participant.health_check()
        assert health.status == "degraded"


class TestIterConfigs:
    def test_finds_configs(self, fake_automations_dir):
        configs = _iter_configs(fake_automations_dir)
        assert len(configs) == 1
        assert configs[0].automation_id == "pr-review-bot"
        assert configs[0].status == "ACTIVE"
        assert configs[0].trigger == "pull_request"
        assert "RADLABtech/iltt-app" in configs[0].repos

    def test_empty_dir(self, fake_automations_dir_empty):
        configs = _iter_configs(fake_automations_dir_empty)
        assert configs == []


class TestExtractMemoryRuns:
    def test_extracts_runs(self, fake_automations_dir):
        configs = _iter_configs(fake_automations_dir)
        runs = _extract_memory_runs(configs[0])
        assert len(runs) == 2
        assert runs[0].date == "2026-06-01"
        assert runs[1].date == "2026-06-04"
        assert len(runs[0].key_actions) == 4  # header suffix + 3 bullets
        assert "Reviewed 5 PRs" in runs[0].key_actions[1]

    def test_no_memory_file(self, tmp_path):
        config = AutomationConfig(
            automation_id="test",
            name="Test",
            status="ACTIVE",
            trigger="schedule",
            kind="test",
            repos=(),
            path=tmp_path / "automation.toml",
            memory_path=None,
        )
        assert _extract_memory_runs(config) == []


class TestInitAutomation:
    def test_creates_scaffold(self, tmp_path):
        auto_dir = tmp_path / "automations"
        result = init_automation(
            automations_dir=auto_dir,
            automation_id="my-bot",
            name="My Bot",
        )
        assert result.is_dir()
        assert (result / "automation.toml").is_file()
        assert (result / "memory.md").is_file()
        # Verify TOML content
        content = (result / "automation.toml").read_text()
        assert 'id = "my-bot"' in content
        assert 'name = "My Bot"' in content

    def test_raises_on_existing(self, tmp_path):
        auto_dir = tmp_path / "automations"
        init_automation(automations_dir=auto_dir, automation_id="my-bot", name="My Bot")
        with pytest.raises(FileExistsError):
            init_automation(automations_dir=auto_dir, automation_id="my-bot", name="My Bot")

    def test_force_overwrites(self, tmp_path):
        auto_dir = tmp_path / "automations"
        init_automation(automations_dir=auto_dir, automation_id="my-bot", name="My Bot")
        # Force should not raise
        result = init_automation(
            automations_dir=auto_dir, automation_id="my-bot", name="My Bot v2", force=True
        )
        content = (result / "automation.toml").read_text()
        assert 'name = "My Bot v2"' in content


class TestBounded:
    def test_short_unchanged(self):
        assert _bounded("hello", 10) == "hello"

    def test_long_truncated(self):
        result = _bounded("x" * 200, 50)
        assert len(result) <= 50
        assert result.endswith("…")
