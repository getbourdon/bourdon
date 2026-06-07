"""Tests for participants.cursor_automations."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from participants.base import L5Manifest, ParticipantDiscoveryError
from participants.cursor_automations import (
    AGENT_ID,
    CursorAutomationsParticipant,
    _extract_memory_runs,
    _iter_configs,
    _parse_memory_sections,
    _serialize_sections,
    default_cursor_automations_dir,
    init_automations_dir,
    merge_automation_tree,
)


def _make_automation(tmp_path: Path, automation_id: str, *, memory: str = "") -> Path:
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


# ---- default dir / env ------------------------------------------------------

def test_default_dir_uses_cursor_home():
    assert default_cursor_automations_dir(cursor_home=Path("/c")) == Path("/c/automations")


def test_default_dir_uses_env(monkeypatch):
    monkeypatch.setenv("CURSOR_DIR", "/env-cursor")
    assert default_cursor_automations_dir() == Path("/env-cursor/automations")


def test_default_dir_falls_back_to_home():
    # Compare as Path so the assertion is OS-separator agnostic (Windows uses
    # backslashes; the old str.endswith(".cursor/automations") failed there).
    assert default_cursor_automations_dir() == Path.home() / ".cursor" / "automations"


# ---- discover ---------------------------------------------------------------

def test_discover_raises_when_dir_missing(tmp_path):
    with pytest.raises(ParticipantDiscoveryError):
        CursorAutomationsParticipant(automations_dir=tmp_path / "nope").discover()


def test_discover_returns_store(tmp_path):
    d = tmp_path / "automations"
    _make_automation(d, "pr-audit")
    store = CursorAutomationsParticipant(automations_dir=d).discover()
    assert store.metadata["automations"] == 1


# ---- parsing -----------------------------------------------------------------

def test_build_config_reads_toml(tmp_path):
    d = tmp_path / "automations"
    _make_automation(d, "weekly-review")
    configs = _iter_configs(d)
    assert len(configs) == 1
    assert configs[0].automation_id == "weekly-review"


def test_extract_memory_runs_parses_dated_sections(tmp_path):
    d = tmp_path / "automations"
    _make_automation(d, "pr-audit", memory=(
        "2026-06-01\n- Reviewed PRs in ILTT and Bourdon.\n\n"
        "2026-06-02\n- Ran memory coverage check.\n"
    ))
    runs = _extract_memory_runs(_iter_configs(d)[0])
    assert len(runs) == 2
    assert "ILTT" in runs[0].projects
    assert "Bourdon" in runs[0].projects


def test_extract_memory_runs_detects_signals(tmp_path):
    d = tmp_path / "automations"
    _make_automation(d, "billing", memory="2026-06-03\n- Checked Stripe billing.\n")
    runs = _extract_memory_runs(_iter_configs(d)[0])
    assert "billing-drift" in runs[0].signals


def test_ci_signal_detected(tmp_path):
    d = tmp_path / "automations"
    _make_automation(d, "ci", memory="2026-06-01\n- GitHub Action workflow run completed.\n")
    runs = _extract_memory_runs(_iter_configs(d)[0])
    assert "ci-signal" in runs[0].signals


# ---- export_l5 ---------------------------------------------------------------

def test_export_l5_empty_when_no_automations(tmp_path):
    d = tmp_path / "automations"
    d.mkdir()
    manifest = CursorAutomationsParticipant(automations_dir=d).export_l5()
    assert isinstance(manifest, L5Manifest)
    assert manifest.agent.id == AGENT_ID


def test_export_l5_produces_sessions_and_entities(tmp_path):
    d = tmp_path / "automations"
    _make_automation(d, "weekly", memory="2026-06-01\n- Ran review for Bourdon.\n")
    manifest = CursorAutomationsParticipant(automations_dir=d).export_l5()
    assert len(manifest.recent_sessions) >= 1
    entity_names = {e.name for e in manifest.known_entities}
    assert "weekly" in entity_names
    assert "Bourdon" in entity_names


def test_export_l5_filters_by_since(tmp_path):
    d = tmp_path / "automations"
    _make_automation(d, "m", memory="2025-01-01\n- Old.\n\n2026-06-01\n- New.\n")
    manifest = CursorAutomationsParticipant(automations_dir=d).export_l5(
        since=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    assert len(manifest.recent_sessions) == 1


def test_export_l5_redacts_credentials(tmp_path):
    d = tmp_path / "automations"
    _make_automation(d, "creds", memory="2026-06-01\n- Found leaked api_key.\n")
    manifest = CursorAutomationsParticipant(automations_dir=d).export_l5()
    for s in manifest.recent_sessions:
        for a in s.key_actions:
            assert "api_key" not in a.lower() or "redacted" in a.lower()


# ---- health_check ------------------------------------------------------------

def test_health_blocked_when_missing(tmp_path):
    h = CursorAutomationsParticipant(automations_dir=tmp_path / "nope").health_check()
    assert h.status == "blocked"


def test_health_degraded_when_empty(tmp_path):
    d = tmp_path / "automations"
    d.mkdir()
    h = CursorAutomationsParticipant(automations_dir=d).health_check()
    assert h.status == "degraded"


def test_health_ok(tmp_path):
    d = tmp_path / "automations"
    _make_automation(d, "check")
    h = CursorAutomationsParticipant(automations_dir=d).health_check()
    assert h.status == "ok"
    assert h.details["automation_count"] == 1


# ---- init_automations_dir ---------------------------------------------------

def test_init_creates_files(tmp_path):
    path = init_automations_dir(automations_dir=tmp_path / "a", automation_id="test")
    assert (path / "automation.toml").is_file()
    assert (path / "memory.md").is_file()


def test_init_raises_if_exists(tmp_path):
    init_automations_dir(automations_dir=tmp_path / "a", automation_id="x")
    with pytest.raises(FileExistsError):
        init_automations_dir(automations_dir=tmp_path / "a", automation_id="x")


# ---- merge_automation_tree ---------------------------------------------------

def test_merge_creates_new(tmp_path):
    src = tmp_path / "src"
    _make_automation(src, "ci", memory="2026-06-01\n- Ran CI.\n")
    dest = tmp_path / "dest"
    dest.mkdir()
    r = merge_automation_tree(src, dest)
    assert r.automations_created == 1
    assert r.bullets_added == 1
    assert (dest / "ci" / "memory.md").is_file()


def test_merge_idempotent(tmp_path):
    src = tmp_path / "src"
    _make_automation(src, "m", memory="2026-06-01\n- Check.\n")
    dest = tmp_path / "dest"
    dest.mkdir()
    merge_automation_tree(src, dest)
    r2 = merge_automation_tree(src, dest)
    assert r2.bullets_added == 0


def test_merge_appends_new_bullets(tmp_path):
    src = tmp_path / "src"
    _make_automation(src, "d", memory="2026-06-01\n- New.\n")
    dest = tmp_path / "dest" / "d"
    dest.mkdir(parents=True)
    (dest / "automation.toml").write_text('id = "d"\n', encoding="utf-8")
    (dest / "memory.md").write_text("2026-06-01\n- Existing.\n", encoding="utf-8")
    r = merge_automation_tree(tmp_path / "src", tmp_path / "dest")
    assert r.bullets_added == 1
    text = (dest / "memory.md").read_text()
    assert "Existing." in text and "New." in text


def test_merge_skips_invalid_ids(tmp_path):
    bad = tmp_path / "src" / "bad id"
    bad.mkdir(parents=True)
    (bad / "automation.toml").write_text('id = "bad"\n', encoding="utf-8")
    dest = tmp_path / "dest"
    dest.mkdir()
    r = merge_automation_tree(tmp_path / "src", dest)
    assert "bad id" in r.skipped


def test_merge_raises_on_missing_source(tmp_path):
    with pytest.raises(FileNotFoundError):
        merge_automation_tree(tmp_path / "nope", tmp_path / "dest")


# ---- parse / serialize roundtrip ---------------------------------------------

def test_parse_memory_sections_basic():
    sections = _parse_memory_sections("2026-06-01\n- A.\n- B.\n\n2026-06-02\n- C.\n")
    assert len(sections) == 2
    assert sections[0] == ("2026-06-01", ["A.", "B."])


def test_serialize_roundtrip():
    sections = [("2026-06-01", ["A", "B"]), ("2026-06-02", ["C"])]
    assert _parse_memory_sections(_serialize_sections(sections)) == sections


# ---- protocol conformance ----------------------------------------------------

def test_class_attrs():
    assert CursorAutomationsParticipant.agent_id == "cursor-automations"
    assert CursorAutomationsParticipant.agent_type == "other"
