"""Tests for core.l5_io -- atomic L5 write + read helpers."""

from __future__ import annotations

from datetime import datetime, timezone

import yaml

from participants.base import SPEC_VERSION, AgentInfo, Entity, L5Manifest, Visibility
from core.l5_io import read_l5_dict, write_l5, write_l5_dict

# -- write_l5_dict -------------------------------------------------------------


def test_write_l5_dict_creates_file(tmp_path):
    target = tmp_path / "agent.l5.yaml"
    write_l5_dict({"spec_version": "0.1"}, target)
    assert target.is_file()
    loaded = yaml.safe_load(target.read_text(encoding="utf-8"))
    assert loaded == {"spec_version": "0.1"}


def test_write_l5_dict_creates_parent_dirs(tmp_path):
    target = tmp_path / "nested" / "dirs" / "agent.l5.yaml"
    write_l5_dict({"spec_version": "0.1"}, target)
    assert target.is_file()


def test_write_l5_dict_overwrites_existing_file(tmp_path):
    target = tmp_path / "agent.l5.yaml"
    write_l5_dict({"spec_version": "0.1"}, target)
    write_l5_dict({"spec_version": "0.2"}, target)
    loaded = yaml.safe_load(target.read_text(encoding="utf-8"))
    assert loaded == {"spec_version": "0.2"}


def test_write_l5_dict_cleans_up_tmp_on_success(tmp_path):
    target = tmp_path / "agent.l5.yaml"
    write_l5_dict({"spec_version": "0.1"}, target)
    tmp_file = target.with_suffix(target.suffix + ".tmp")
    assert not tmp_file.exists()


def test_write_l5_dict_writes_utf8(tmp_path):
    target = tmp_path / "agent.l5.yaml"
    write_l5_dict({"text": "hello with em-dash -- world"}, target)
    content = target.read_bytes().decode("utf-8")
    assert "em-dash" in content


# -- write_l5 (manifest-aware) -------------------------------------------------


def test_write_l5_round_trip(tmp_path):
    target = tmp_path / "agent.l5.yaml"
    manifest = L5Manifest(
        spec_version=SPEC_VERSION,
        agent=AgentInfo(id="test-agent", type="code-assistant"),
        last_updated=datetime.now(timezone.utc).isoformat(),
        known_entities=[
            Entity(name="ILTT", type="project", summary="Fitness app"),
        ],
    )
    write_l5(manifest, target)

    read_back = read_l5_dict(target)
    assert read_back is not None
    assert read_back["spec_version"] == SPEC_VERSION
    assert read_back["agent"]["id"] == "test-agent"
    assert len(read_back["known_entities"]) == 1
    assert read_back["known_entities"][0]["name"] == "ILTT"


def test_write_l5_drops_empty_lists_per_dataclass_rules(tmp_path):
    target = tmp_path / "agent.l5.yaml"
    manifest = L5Manifest(
        spec_version="0.1",
        agent=AgentInfo(id="x", type="code-assistant"),
        last_updated="2026-04-15T12:00:00+00:00",
        # known_entities intentionally left as [] default
    )
    write_l5(manifest, target)
    read_back = read_l5_dict(target)
    # Empty list dropped by to_dict()
    assert "known_entities" not in read_back


def test_write_l5_preserves_visibility_enum_as_string(tmp_path):
    target = tmp_path / "agent.l5.yaml"
    manifest = L5Manifest(
        spec_version="0.1",
        agent=AgentInfo(id="x", type="code-assistant"),
        last_updated="2026-04-15T12:00:00+00:00",
        known_entities=[Entity(name="PII", visibility=Visibility.PRIVATE)],
    )
    write_l5(manifest, target)
    read_back = read_l5_dict(target)
    assert read_back["known_entities"][0]["visibility"] == "private"


# -- read_l5_dict --------------------------------------------------------------


def test_read_l5_dict_returns_none_for_missing_file(tmp_path):
    assert read_l5_dict(tmp_path / "nope.l5.yaml") is None


def test_read_l5_dict_returns_none_for_malformed_yaml(tmp_path, caplog):
    target = tmp_path / "broken.l5.yaml"
    target.write_text("{{{not yaml{{{", encoding="utf-8")
    with caplog.at_level("WARNING"):
        assert read_l5_dict(target) is None
    assert any("Failed to read" in r.message for r in caplog.records)


def test_read_l5_dict_returns_none_for_non_dict_yaml(tmp_path):
    target = tmp_path / "list.l5.yaml"
    target.write_text("- just\n- a\n- list\n", encoding="utf-8")
    assert read_l5_dict(target) is None
