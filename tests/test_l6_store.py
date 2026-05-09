"""Tests for core.l6_store -- pure-Python federation store."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

from core.l6_store import (
    EntityMatch,
    L6Store,
    ProjectSummary,
    SessionRef,
)

# -- Fixture -------------------------------------------------------------------


@pytest.fixture
def library(tmp_path):
    """Return (library_path, write_manifest) helpers."""
    lib = tmp_path / "agent-library"
    agents_dir = lib / "agents"
    agents_dir.mkdir(parents=True)

    def write(agent_id: str, manifest: dict) -> Path:
        path = agents_dir / f"{agent_id}.l5.yaml"
        path.write_text(yaml.safe_dump(manifest), encoding="utf-8")
        return path

    return {"path": lib, "write": write, "agents_dir": agents_dir}


def _manifest(
    agent_id: str,
    entities: list[dict] | None = None,
    sessions: list[dict] | None = None,
) -> dict:
    """Build a minimal-but-valid L5 manifest dict for tests."""
    return {
        "spec_version": "0.1",
        "agent": {"id": agent_id, "type": "code-assistant"},
        "last_updated": "2026-04-15T12:00:00+00:00",
        "known_entities": entities or [],
        "recent_sessions": sessions or [],
    }


# -- Empty / initialization ----------------------------------------------------


def test_empty_store_when_library_missing(tmp_path):
    store = L6Store(tmp_path / "does-not-exist")
    assert store.list_agents() == []


def test_empty_store_when_agents_dir_empty(library):
    store = L6Store(library["path"])
    assert store.list_agents() == []


def test_default_library_path_is_home_agent_library():
    """Default constructor points at ~/agent-library, not cwd."""
    from core.l6_store import DEFAULT_LIBRARY_PATH

    assert Path.home() / "agent-library" == DEFAULT_LIBRARY_PATH


# -- Loading manifests ---------------------------------------------------------


def test_load_single_manifest(library):
    library["write"](
        "claude-code",
        _manifest(
            "claude-code",
            entities=[{"name": "ILTT", "type": "project", "summary": "Fitness app"}],
        ),
    )
    store = L6Store(library["path"])
    assert store.list_agents() == ["claude-code"]
    matches = store.find_entity("ILTT")
    assert len(matches) == 1
    assert matches[0].name == "ILTT"
    assert "claude-code" in matches[0].agents


def test_load_multiple_manifests(library):
    library["write"](
        "claude-code",
        _manifest("claude-code", entities=[{"name": "ILTT", "summary": "From CC"}]),
    )
    library["write"](
        "codex",
        _manifest("codex", entities=[{"name": "ILTT", "summary": "From Codex"}]),
    )
    store = L6Store(library["path"])
    assert sorted(store.list_agents()) == ["claude-code", "codex"]
    matches = store.find_entity("ILTT")
    assert len(matches) == 1  # Same-named entity merges into one match
    assert set(matches[0].agents) == {"claude-code", "codex"}
    assert matches[0].summaries == {
        "claude-code": "From CC",
        "codex": "From Codex",
    }


def test_malformed_yaml_is_skipped(library, caplog):
    library["write"](
        "valid",
        _manifest("valid", entities=[{"name": "Good"}]),
    )
    bad = library["agents_dir"] / "bad.l5.yaml"
    bad.write_text("{{{not valid yaml", encoding="utf-8")

    with caplog.at_level("WARNING"):
        store = L6Store(library["path"])

    assert store.list_agents() == ["valid"]
    assert any("Failed to load" in r.message for r in caplog.records)


def test_non_dict_yaml_is_skipped(library):
    (library["agents_dir"] / "list.l5.yaml").write_text(
        "- just\n- a\n- list\n", encoding="utf-8"
    )
    library["write"]("valid", _manifest("valid"))
    store = L6Store(library["path"])
    assert store.list_agents() == ["valid"]


def test_manifest_without_agent_id_falls_back_to_filename(library):
    (library["agents_dir"] / "fallback.l5.yaml").write_text(
        yaml.safe_dump(
            {
                "spec_version": "0.1",
                "agent": {"type": "code-assistant"},  # no id
                "last_updated": "2026-04-15T00:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )
    store = L6Store(library["path"])
    assert store.list_agents() == ["fallback"]


# -- find_entity ---------------------------------------------------------------


def test_find_entity_case_insensitive(library):
    library["write"](
        "a",
        _manifest("a", entities=[{"name": "ILTT", "summary": "Project"}]),
    )
    store = L6Store(library["path"])
    assert len(store.find_entity("iltt")) == 1
    assert len(store.find_entity("ILTT")) == 1
    assert len(store.find_entity("IlTt")) == 1


def test_find_entity_by_alias(library):
    library["write"](
        "a",
        _manifest(
            "a",
            entities=[
                {
                    "name": "ILTT",
                    "aliases": ["if_lift_then_that"],
                    "summary": "Project",
                }
            ],
        ),
    )
    store = L6Store(library["path"])
    matches = store.find_entity("if_lift_then_that")
    assert len(matches) == 1
    assert matches[0].name == "ILTT"


def test_find_entity_unknown_returns_empty(library):
    library["write"]("a", _manifest("a", entities=[{"name": "Alpha"}]))
    store = L6Store(library["path"])
    assert store.find_entity("nothing") == []


def test_find_entity_filters_private_by_default(library):
    library["write"](
        "a",
        _manifest(
            "a",
            entities=[
                {"name": "Public thing"},
                {"name": "Secret", "visibility": "private"},
            ],
        ),
    )
    store = L6Store(library["path"])
    assert len(store.find_entity("Secret")) == 0
    assert len(store.find_entity("Secret", include_private=True)) == 1


def test_find_entity_team_hidden_from_public_visible_to_team(library):
    library["write"](
        "a",
        _manifest(
            "a",
            entities=[
                {"name": "Team secret", "visibility": "team"},
            ],
        ),
    )
    store = L6Store(library["path"])
    assert store.find_entity("Team secret") == []
    assert len(store.find_entity("Team secret", access_level="team")) == 1
    assert len(store.find_entity("Team secret", include_private=True)) == 1


def test_find_entity_aggregates_tags_across_agents(library):
    library["write"](
        "a", _manifest("a", entities=[{"name": "X", "tags": ["alpha", "shared"]}])
    )
    library["write"](
        "b", _manifest("b", entities=[{"name": "X", "tags": ["beta", "shared"]}])
    )
    store = L6Store(library["path"])
    matches = store.find_entity("X")
    assert len(matches) == 1
    assert set(matches[0].tags) == {"alpha", "beta", "shared"}


# -- list_recent_work ----------------------------------------------------------


def test_list_recent_work_across_all_agents(library):
    library["write"](
        "a",
        _manifest("a", sessions=[{"date": "2026-04-15", "key_actions": ["shipped"]}]),
    )
    library["write"](
        "b",
        _manifest("b", sessions=[{"date": "2026-04-13", "key_actions": ["refactor"]}]),
    )
    store = L6Store(library["path"])
    results = store.list_recent_work()
    assert len(results) == 2
    # Newest first
    assert results[0].date == "2026-04-15"
    assert results[1].date == "2026-04-13"


def test_list_recent_work_filtered_by_agent(library):
    library["write"](
        "a",
        _manifest("a", sessions=[{"date": "2026-04-15", "key_actions": ["a work"]}]),
    )
    library["write"](
        "b",
        _manifest("b", sessions=[{"date": "2026-04-14", "key_actions": ["b work"]}]),
    )
    store = L6Store(library["path"])
    only_b = store.list_recent_work(agent="b")
    assert len(only_b) == 1
    assert only_b[0].agent == "b"


def test_list_recent_work_since_cutoff(library):
    library["write"](
        "a",
        _manifest(
            "a",
            sessions=[
                {"date": "2026-04-15", "key_actions": ["recent"]},
                {"date": "2026-04-01", "key_actions": ["old"]},
            ],
        ),
    )
    store = L6Store(library["path"])
    cutoff = datetime(2026, 4, 10, tzinfo=timezone.utc)
    results = store.list_recent_work(since=cutoff)
    assert len(results) == 1
    assert results[0].date == "2026-04-15"


def test_list_recent_work_skips_private_sessions(library):
    library["write"](
        "a",
        _manifest(
            "a",
            sessions=[
                {"date": "2026-04-15", "key_actions": ["public"]},
                {
                    "date": "2026-04-14",
                    "key_actions": ["secret"],
                    "visibility": "private",
                },
            ],
        ),
    )
    store = L6Store(library["path"])
    public = store.list_recent_work()
    assert len(public) == 1
    assert public[0].key_actions == ["public"]
    with_private = store.list_recent_work(include_private=True)
    assert len(with_private) == 2


def test_list_recent_work_team_hidden_from_public_visible_to_team(library):
    library["write"](
        "a",
        _manifest(
            "a",
            sessions=[
                {
                    "date": "2026-04-15",
                    "key_actions": ["team-only"],
                    "visibility": "team",
                }
            ],
        ),
    )
    store = L6Store(library["path"])
    assert store.list_recent_work() == []
    assert len(store.list_recent_work(access_level="team")) == 1


def test_list_recent_work_skips_malformed_dates(library):
    library["write"](
        "a",
        _manifest(
            "a",
            sessions=[
                {"date": "not-a-date", "key_actions": ["bad"]},
                {"date": "2026-04-15", "key_actions": ["good"]},
            ],
        ),
    )
    store = L6Store(library["path"])
    # With cutoff, bad dates are dropped; without cutoff, they still appear
    # (we include them so they're visible, just un-sortable by date)
    results = store.list_recent_work(
        since=datetime(2026, 4, 1, tzinfo=timezone.utc)
    )
    assert len(results) == 1
    assert results[0].key_actions == ["good"]


# -- get_agent_manifest --------------------------------------------------------


def test_get_agent_manifest_returns_none_for_unknown(library):
    library["write"]("a", _manifest("a"))
    store = L6Store(library["path"])
    assert store.get_agent_manifest("unknown") is None


def test_get_agent_manifest_filters_private_entities(library):
    library["write"](
        "a",
        _manifest(
            "a",
            entities=[
                {"name": "Public"},
                {"name": "Secret", "visibility": "private"},
            ],
        ),
    )
    store = L6Store(library["path"])
    manifest = store.get_agent_manifest("a")
    names = [e["name"] for e in manifest["known_entities"]]
    assert "Public" in names
    assert "Secret" not in names


def test_get_agent_manifest_include_private_shows_private(library):
    library["write"](
        "a",
        _manifest(
            "a",
            entities=[{"name": "Secret", "visibility": "private"}],
        ),
    )
    store = L6Store(library["path"])
    manifest = store.get_agent_manifest("a", include_private=True)
    names = [e["name"] for e in manifest["known_entities"]]
    assert "Secret" in names


def test_get_agent_manifest_access_level_team_includes_team_not_private(library):
    library["write"](
        "a",
        _manifest(
            "a",
            entities=[
                {"name": "Public"},
                {"name": "Team only", "visibility": "team"},
                {"name": "Private", "visibility": "private"},
            ],
        ),
    )
    store = L6Store(library["path"])
    manifest = store.get_agent_manifest("a", access_level="team")
    names = [e["name"] for e in manifest["known_entities"]]
    assert "Public" in names
    assert "Team only" in names
    assert "Private" not in names


def test_build_recognition_manifest_merges_visible_entities_across_agents(library):
    library["write"](
        "claude-code",
        _manifest(
            "claude-code",
            entities=[
                {
                    "name": "Bourdon",
                    "type": "topic",
                    "aliases": ["Continuo"],
                    "summary": "Claude thesis context.",
                    "visibility": "team",
                    "tags": ["claude"],
                }
            ],
        ),
    )
    library["write"](
        "codex",
        _manifest(
            "codex",
            entities=[
                {
                    "name": "Bourdon",
                    "type": "topic",
                    "aliases": ["runtime recognition"],
                    "summary": "Codex fallback concept.",
                    "visibility": "team",
                    "tags": ["codex"],
                },
                {
                    "name": "Private Anchor",
                    "type": "topic",
                    "visibility": "private",
                },
            ],
        ),
    )

    store = L6Store(library["path"])
    manifest = store.build_recognition_manifest(access_level="team")
    entities = {entity["name"]: entity for entity in manifest["known_entities"]}

    assert "Bourdon" in entities
    assert "Private Anchor" not in entities
    assert set(entities["Bourdon"]["source_agents"]) == {"claude-code", "codex"}
    assert set(entities["Bourdon"]["aliases"]) == {
        "Continuo",
        "runtime recognition",
    }
    assert entities["Bourdon"]["summaries"] == {
        "claude-code": "Claude thesis context.",
        "codex": "Codex fallback concept.",
    }


def test_build_recognition_manifest_respects_public_default(library):
    library["write"](
        "codex",
        _manifest(
            "codex",
            entities=[
                {"name": "Public Anchor", "visibility": "public"},
                {"name": "Team Anchor", "visibility": "team"},
            ],
        ),
    )

    store = L6Store(library["path"])
    manifest = store.build_recognition_manifest()
    names = {entity["name"] for entity in manifest["known_entities"]}

    assert names == {"Public Anchor"}


# -- get_cross_agent_summary ---------------------------------------------------


def test_cross_agent_summary_rollup(library):
    library["write"](
        "claude-code",
        _manifest(
            "claude-code",
            entities=[
                {"name": "ILTT", "type": "project", "summary": "From CC"}
            ],
            sessions=[
                {
                    "date": "2026-04-15",
                    "project_focus": ["ILTT"],
                    "key_actions": ["shipped v1.0.4"],
                }
            ],
        ),
    )
    library["write"](
        "codex",
        _manifest(
            "codex",
            entities=[
                {"name": "ILTT", "type": "project", "summary": "From Codex"}
            ],
            sessions=[
                {
                    "date": "2026-04-14",
                    "project_focus": ["ILTT"],
                    "key_actions": ["debugged auth"],
                }
            ],
        ),
    )
    store = L6Store(library["path"])
    summary = store.get_cross_agent_summary("ILTT")
    assert set(summary.agents) == {"claude-code", "codex"}
    assert len(summary.recent_sessions) == 2
    assert summary.recent_sessions[0].date == "2026-04-15"  # newest first
    assert len(summary.entities) == 1
    assert set(summary.entities[0].agents) == {"claude-code", "codex"}


def test_cross_agent_summary_project_focus_case_insensitive(library):
    library["write"](
        "a",
        _manifest(
            "a",
            sessions=[
                {"date": "2026-04-15", "project_focus": ["iltt"]},
                {"date": "2026-04-14", "project_focus": ["Other"]},
            ],
        ),
    )
    store = L6Store(library["path"])
    summary = store.get_cross_agent_summary("ILTT")
    assert len(summary.recent_sessions) == 1


# -- Reload --------------------------------------------------------------------


def test_reload_agent_picks_up_new_entity(library):
    library["write"]("a", _manifest("a", entities=[{"name": "Old"}]))
    store = L6Store(library["path"])
    assert len(store.find_entity("new")) == 0

    # Update the file on disk
    library["write"](
        "a",
        _manifest("a", entities=[{"name": "Old"}, {"name": "New"}]),
    )
    store.reload_agent("a")
    assert len(store.find_entity("new")) == 1


def test_reload_agent_drops_removed_agent(library):
    path = library["write"]("a", _manifest("a", entities=[{"name": "X"}]))
    store = L6Store(library["path"])
    assert store.list_agents() == ["a"]

    path.unlink()  # remove the file
    reloaded = store.reload_agent("a")
    assert reloaded is False
    assert store.list_agents() == []
    assert store.find_entity("X") == []


def test_reload_all_rebuilds_index(library):
    library["write"]("a", _manifest("a", entities=[{"name": "Alpha"}]))
    store = L6Store(library["path"])

    # Add another agent to disk + rebuild
    library["write"]("b", _manifest("b", entities=[{"name": "Beta"}]))
    store.reload_all()
    assert sorted(store.list_agents()) == ["a", "b"]
    assert len(store.find_entity("Alpha")) == 1
    assert len(store.find_entity("Beta")) == 1


# -- to_dict serialization -----------------------------------------------------


def test_entity_match_to_dict_shape():
    m = EntityMatch(
        name="X", agents=["a"], types=["project"], summaries={"a": "s"}, tags=["t"]
    )
    d = m.to_dict()
    assert d == {
        "name": "X",
        "agents": ["a"],
        "types": ["project"],
        "summaries": {"a": "s"},
        "tags": ["t"],
    }


def test_session_ref_to_dict_shape():
    s = SessionRef(
        agent="a",
        date="2026-04-15",
        cwd="/tmp",
        project_focus=["P"],
        key_actions=["did thing"],
        files_touched=["f.py"],
    )
    d = s.to_dict()
    assert d["agent"] == "a"
    assert d["date"] == "2026-04-15"
    assert d["project_focus"] == ["P"]


def test_project_summary_to_dict_shape():
    ps = ProjectSummary(
        project="ILTT",
        agents=["a"],
        recent_sessions=[SessionRef(agent="a", date="2026-04-15")],
        entities=[EntityMatch(name="ILTT", agents=["a"])],
    )
    d = ps.to_dict()
    assert d["project"] == "ILTT"
    assert d["agents"] == ["a"]
    assert len(d["recent_sessions"]) == 1
    assert len(d["entities"]) == 1
