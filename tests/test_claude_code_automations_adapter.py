"""Tests for adapters.claude_code_automations."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from adapters.base import BourdonAdapter, Visibility
from adapters.claude_code_automations import (
    AGENT_ID,
    AGENT_TYPE,
    ClaudeCodeAutomationsAdapter,
    MergeResult,
    _build_config,
    _extract_memory_runs,
    _parse_memory_sections,
    merge_automation_tree,
)


def _write_automation(
    root: Path,
    automation_id: str = "weekly-pr-digest",
    memory: str | None = None,
) -> Path:
    automation_dir = root / automation_id
    automation_dir.mkdir(parents=True)
    (automation_dir / "automation.toml").write_text(
        f"""\
version = 1
id = "{automation_id}"
kind = "loop"
name = "Weekly PR Digest"
status = "ACTIVE"
rrule = "FREQ=WEEKLY;BYDAY=MO"
cwds = ["/Users/radman/claudework"]
""",
        encoding="utf-8",
    )
    if memory is not None:
        (automation_dir / "memory.md").write_text(memory, encoding="utf-8")
    return automation_dir


def test_adapter_satisfies_protocol(tmp_path):
    _write_automation(tmp_path, memory="2026-06-03\n- ShipStable launch gate found\n")

    adapter = ClaudeCodeAutomationsAdapter(automations_dir=tmp_path)

    assert isinstance(adapter, BourdonAdapter)
    assert adapter.agent_id == AGENT_ID
    assert adapter.agent_type == AGENT_TYPE


def test_agent_id_is_claude_code_automations():
    """Regression guard: federation graph distinguishes this from claude-code."""
    assert AGENT_ID == "claude-code-automations"
    assert AGENT_TYPE == "other"


def test_build_config_reads_toml_and_memory_path(tmp_path):
    automation_dir = _write_automation(tmp_path, memory="2026-06-03\n- Run summary\n")

    config = _build_config(automation_dir / "automation.toml")

    assert config is not None
    assert config.automation_id == "weekly-pr-digest"
    assert config.name == "Weekly PR Digest"
    assert config.status == "ACTIVE"
    assert config.kind == "loop"
    assert config.cwds == ("/Users/radman/claudework",)
    assert config.memory_path == automation_dir / "memory.md"


def test_extract_memory_runs_from_dated_sections(tmp_path):
    automation_dir = _write_automation(
        tmp_path,
        memory="""\
2026-06-02 run: opened PR digest for ShipStable + ILTT
- ShipStable launch gate verified.
- Runtime: ~4 minutes.

2026-06-03
- Castmore RevenueCat key swap still blocking.
- Federated memory manifests confirm CHIP Step 5 gate green.
""",
    )
    config = _build_config(automation_dir / "automation.toml")
    assert config is not None

    runs = _extract_memory_runs(config)

    assert [run.date for run in runs] == ["2026-06-02", "2026-06-03"]
    assert "opened PR digest" in runs[0].key_actions[0]
    assert "Runtime" not in " ".join(runs[0].key_actions)
    assert "ShipStable" in runs[0].projects
    assert "ILTT" in runs[0].projects
    assert "Castmore" in runs[1].projects
    assert "billing-drift" in runs[1].signals
    assert "memory-coverage-gap" in runs[1].signals


def test_export_l5_emits_automation_sessions_and_entities(tmp_path):
    _write_automation(
        tmp_path,
        memory="""\
2026-06-03
- Production release gate cleared for ShipStable.
- Bourdon needs claude-code-automations L5 publisher.
""",
    )

    manifest = ClaudeCodeAutomationsAdapter(automations_dir=tmp_path).export_l5()
    data = manifest.to_dict()

    assert data["agent"]["id"] == "claude-code-automations"
    assert data["agent"]["type"] == "other"
    assert data["recent_sessions"][0]["date"] == "2026-06-03"
    assert data["recent_sessions"][0]["cwd"] == "/Users/radman/claudework"
    assert (
        "automation_id: weekly-pr-digest"
        in data["recent_sessions"][0]["key_actions"]
    )
    entity_names = {entity["name"] for entity in data["known_entities"]}
    assert "weekly-pr-digest" in entity_names
    assert "ShipStable" in entity_names
    assert "release-gate" in entity_names
    assert "memory-coverage-gap" in entity_names


def test_export_sessions_filters_since(tmp_path):
    _write_automation(
        tmp_path,
        memory="""\
2026-06-01
- Old PR digest run.

2026-06-03
- Fresh PR digest run.
""",
    )

    sessions = ClaudeCodeAutomationsAdapter(automations_dir=tmp_path).export_sessions(
        since=datetime(2026, 6, 2, tzinfo=timezone.utc)
    )

    assert [session.date for session in sessions] == ["2026-06-03"]


def test_health_check_reports_blocked_missing_dir(tmp_path):
    adapter = ClaudeCodeAutomationsAdapter(automations_dir=tmp_path / "missing")

    health = adapter.health_check()

    assert health.status == "blocked"
    assert "automations directory not found" in (health.reason or "").lower()
    assert health.proposed_fix and "automation.toml" in health.proposed_fix


def test_health_check_counts_runs(tmp_path):
    _write_automation(tmp_path, memory="2026-06-03\n- ShipStable launch report.\n")

    health = ClaudeCodeAutomationsAdapter(automations_dir=tmp_path).health_check()

    assert health.status == "ok"
    assert health.details["automation_count"] == 1
    assert health.details["memory_files"] == 1
    assert health.details["runs_extracted"] == 1
    assert health.details["active_automations"] == 1


def test_health_check_degraded_empty_dir(tmp_path):
    """Existing dir with no automation.toml -> degraded, not blocked."""
    adapter = ClaudeCodeAutomationsAdapter(automations_dir=tmp_path)

    health = adapter.health_check()

    assert health.status == "degraded"
    assert "no automation.toml" in (health.reason or "").lower()


def test_redacts_secret_words_in_run_actions(tmp_path):
    _write_automation(
        tmp_path,
        memory="2026-06-03\n- Found api_key in the automation note.\n",
    )

    manifest = ClaudeCodeAutomationsAdapter(automations_dir=tmp_path).export_l5()
    action_text = " ".join(manifest.recent_sessions[0].key_actions)

    assert "api_key" not in action_text
    assert "redacted" in action_text.lower()
    assert manifest.recent_sessions[0].visibility == Visibility.TEAM


def test_default_automations_dir_uses_claude_home(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    from adapters.claude_code_automations import default_claude_code_automations_dir

    assert default_claude_code_automations_dir() == tmp_path / "automations"


def test_default_automations_dir_falls_back_to_home(monkeypatch, tmp_path):
    monkeypatch.delenv("CLAUDE_HOME", raising=False)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    from adapters.claude_code_automations import default_claude_code_automations_dir

    assert default_claude_code_automations_dir() == tmp_path / ".claude" / "automations"


# ---- Path B: merge_automation_tree (GitHub Actions ingest) ------------------


def test_merge_creates_new_automation_when_dest_missing(tmp_path):
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    a = src / "gh-pr-digest"
    a.mkdir(parents=True)
    (a / "automation.toml").write_text(
        'id = "gh-pr-digest"\nname = "GH PR Digest"\n'
        'status = "ACTIVE"\nkind = "github-action"\nrrule = ""\ncwds = []\n',
        encoding="utf-8",
    )
    (a / "memory.md").write_text(
        "2026-06-03\n- Ran PR digest in CI run 42.\n- Found 2 flaky tests.\n",
        encoding="utf-8",
    )

    result = merge_automation_tree(src, dst)

    assert isinstance(result, MergeResult)
    assert result.automations_seen == 1
    assert result.automations_created == 1
    assert result.bullets_added == 2
    assert result.sections_created == 1
    assert (dst / "gh-pr-digest" / "automation.toml").is_file()
    body = (dst / "gh-pr-digest" / "memory.md").read_text(encoding="utf-8")
    assert "Ran PR digest in CI run 42." in body
    assert body.startswith("2026-06-03")


def test_merge_is_idempotent_on_repeat(tmp_path):
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    a = src / "gh-pr-digest"
    a.mkdir(parents=True)
    (a / "memory.md").write_text(
        "2026-06-03\n- Ran PR digest.\n", encoding="utf-8"
    )

    merge_automation_tree(src, dst)
    result2 = merge_automation_tree(src, dst)

    assert result2.automations_created == 0
    assert result2.bullets_added == 0
    assert result2.sections_created == 0


def test_merge_appends_new_bullets_to_existing_date(tmp_path):
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    (dst / "gh-digest").mkdir(parents=True)
    (dst / "gh-digest" / "automation.toml").write_text(
        'id = "gh-digest"\nname = "x"\nstatus = "ACTIVE"\n'
        'kind = "github-action"\nrrule = ""\ncwds = []\n',
        encoding="utf-8",
    )
    (dst / "gh-digest" / "memory.md").write_text(
        "2026-06-03\n- Existing bullet.\n", encoding="utf-8"
    )
    src_dir = src / "gh-digest"
    src_dir.mkdir(parents=True)
    (src_dir / "memory.md").write_text(
        "2026-06-03\n- Existing bullet.\n- New bullet from CI.\n",
        encoding="utf-8",
    )

    result = merge_automation_tree(src, dst)

    assert result.automations_created == 0
    assert result.bullets_added == 1
    body = (dst / "gh-digest" / "memory.md").read_text(encoding="utf-8")
    assert "Existing bullet." in body
    assert "New bullet from CI." in body
    # No duplicate of the existing bullet
    assert body.count("Existing bullet.") == 1


def test_merge_synthesizes_automation_toml_if_missing_in_source(tmp_path):
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    a = src / "untracked-ci-job"
    a.mkdir(parents=True)
    (a / "memory.md").write_text(
        "2026-06-03\n- CI run logged from a runner with no toml.\n",
        encoding="utf-8",
    )

    merge_automation_tree(src, dst, default_kind="github-action")

    toml_text = (dst / "untracked-ci-job" / "automation.toml").read_text(
        encoding="utf-8"
    )
    assert 'id = "untracked-ci-job"' in toml_text
    assert 'kind = "github-action"' in toml_text


def test_merge_skips_invalid_ids(tmp_path):
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    bad = src / "bad name with spaces"
    bad.mkdir(parents=True)
    (bad / "memory.md").write_text("2026-06-03\n- nope\n", encoding="utf-8")

    result = merge_automation_tree(src, dst)

    assert result.automations_created == 0
    assert "bad name with spaces" in result.skipped


def test_merge_preserves_chronological_ordering(tmp_path):
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    (dst / "x").mkdir(parents=True)
    (dst / "x" / "automation.toml").write_text(
        'id = "x"\nname = "x"\nstatus = "ACTIVE"\n'
        'kind = "github-action"\nrrule = ""\ncwds = []\n',
        encoding="utf-8",
    )
    (dst / "x" / "memory.md").write_text(
        "2026-06-04\n- Later.\n", encoding="utf-8"
    )
    src_dir = src / "x"
    src_dir.mkdir(parents=True)
    (src_dir / "memory.md").write_text(
        "2026-06-02\n- Earlier.\n", encoding="utf-8"
    )

    merge_automation_tree(src, dst)

    body = (dst / "x" / "memory.md").read_text(encoding="utf-8")
    assert body.index("2026-06-02") < body.index("2026-06-04")


def test_merge_raises_when_source_missing(tmp_path):
    src = tmp_path / "does-not-exist"
    dst = tmp_path / "dst"

    try:
        merge_automation_tree(src, dst)
    except FileNotFoundError as exc:
        assert "merge source not found" in str(exc)
    else:
        raise AssertionError("expected FileNotFoundError")


def test_parse_memory_sections_handles_same_line_subtitle():
    text = "2026-06-03 run: did a thing\n- bullet one\n"
    sections = _parse_memory_sections(text)
    assert sections == [("2026-06-03", ["run: did a thing", "bullet one"])]
