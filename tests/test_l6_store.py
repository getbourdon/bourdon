"""Tests for core.l6_store -- pure-Python federation store."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
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


# All list_recent_work tests pass an explicit ``since`` covering the
# fixture dates. The store now applies a 14-day default-since window when
# ``since`` is None, so naked calls against hard-coded historical dates
# would return empty and the assertions would also rot when the absolute
# clock advances past their window. Explicit ``since`` is deterministic.
_TEST_EPOCH = datetime(2026, 1, 1, tzinfo=timezone.utc)


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
    results = store.list_recent_work(since=_TEST_EPOCH)
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
    only_b = store.list_recent_work(since=_TEST_EPOCH, agent="b")
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
    public = store.list_recent_work(since=_TEST_EPOCH)
    assert len(public) == 1
    assert public[0].key_actions == ["public"]
    with_private = store.list_recent_work(since=_TEST_EPOCH, include_private=True)
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
    assert len(store.list_recent_work(since=_TEST_EPOCH)) == 0
    assert len(store.list_recent_work(since=_TEST_EPOCH, access_level="team")) == 1


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
    results = store.list_recent_work(
        since=datetime(2026, 4, 1, tzinfo=timezone.utc)
    )
    assert len(results) == 1
    assert results[0].key_actions == ["good"]


# -- list_recent_work pagination + payload bounds (issue #48) ----------------


def _seed_dated_sessions(library, agent_id: str, count: int) -> None:
    """Seed ``count`` sessions on consecutive dates starting at 2026-04-30."""
    sessions = [
        {
            "date": (date(2026, 4, 30) - timedelta(days=i)).isoformat(),
            "key_actions": [f"action {i}"],
            "files_touched": [f"file{i}.py"],
        }
        for i in range(count)
    ]
    library["write"](agent_id, _manifest(agent_id, sessions=sessions))


def test_list_recent_work_default_limit_caps_payload(library):
    """First call with no args returns at most DEFAULT_LIMIT sessions."""
    _seed_dated_sessions(library, "a", 30)
    store = L6Store(library["path"])
    # Explicit far-past since so the 14-day default-since window doesn't
    # also drop these test dates; we're testing the limit independently.
    page = store.list_recent_work(since=_TEST_EPOCH)
    assert len(page) == 20  # DEFAULT_LIMIT
    assert page.has_more is True
    assert page.next_cursor is not None


def test_list_recent_work_limit_caps_at_max(library):
    """An explicit limit above MAX_LIMIT is clamped to MAX_LIMIT (100)."""
    _seed_dated_sessions(library, "a", 150)
    store = L6Store(library["path"])
    page = store.list_recent_work(since=_TEST_EPOCH, limit=500)
    assert len(page) == 100  # MAX_LIMIT, not 500


def test_list_recent_work_limit_under_one_is_coerced_up(library):
    """``limit=0`` and negative values are coerced to 1 -- never silently empty."""
    _seed_dated_sessions(library, "a", 5)
    store = L6Store(library["path"])
    assert len(store.list_recent_work(since=_TEST_EPOCH, limit=0)) == 1
    assert len(store.list_recent_work(since=_TEST_EPOCH, limit=-5)) == 1


def test_list_recent_work_cursor_walks_through_all_pages(library):
    """Paginating with cursor returns every session exactly once, in order."""
    _seed_dated_sessions(library, "a", 25)
    store = L6Store(library["path"])
    collected: list[str] = []
    cursor: str | None = None
    pages = 0
    while True:
        pages += 1
        assert pages < 10, "pagination loop should terminate well before this"
        page = store.list_recent_work(
            since=_TEST_EPOCH,
            limit=10,
            cursor=cursor,
        )
        collected.extend(s.date for s in page)
        if not page.has_more:
            break
        cursor = page.next_cursor
    # All 25 sessions, no duplicates, newest-first.
    assert len(collected) == 25
    assert collected == sorted(collected, reverse=True)


def test_list_recent_work_last_page_has_no_cursor(library):
    """``has_more`` is False and ``next_cursor`` is None on the terminal page."""
    _seed_dated_sessions(library, "a", 5)
    store = L6Store(library["path"])
    page = store.list_recent_work(since=_TEST_EPOCH, limit=10)
    assert len(page) == 5
    assert page.has_more is False
    assert page.next_cursor is None


def test_list_recent_work_default_since_window_filters_old_sessions(library):
    """Without explicit ``since`` or ``cursor``, sessions older than 14 days
    are filtered out -- this is the throttle that prevents the unbounded-payload
    stall observed in the Layer 3 acceptance demo (issue #48)."""
    today = datetime.now(timezone.utc).date()
    library["write"](
        "a",
        _manifest(
            "a",
            sessions=[
                {"date": today.isoformat(), "key_actions": ["fresh"]},
                {
                    "date": (today - timedelta(days=30)).isoformat(),
                    "key_actions": ["old"],
                },
            ],
        ),
    )
    store = L6Store(library["path"])
    page = store.list_recent_work()  # no args -> default-since-14-days applies
    dates = [s.date for s in page]
    assert today.isoformat() in dates
    assert (today - timedelta(days=30)).isoformat() not in dates


def test_list_recent_work_explicit_since_bypasses_default_window(library):
    """Passing an explicit ``since`` overrides the 14-day default."""
    today = datetime.now(timezone.utc).date()
    library["write"](
        "a",
        _manifest(
            "a",
            sessions=[
                {
                    "date": (today - timedelta(days=60)).isoformat(),
                    "key_actions": ["older than default-since"],
                },
            ],
        ),
    )
    store = L6Store(library["path"])
    # The default-since would hide this; explicit since=epoch must show it.
    page = store.list_recent_work(since=_TEST_EPOCH)
    assert len(page) == 1


def test_list_recent_work_invalid_cursor_raises(library):
    """A malformed cursor surfaces as ValueError rather than silent reset."""
    _seed_dated_sessions(library, "a", 3)
    store = L6Store(library["path"])
    with pytest.raises(ValueError, match="invalid cursor"):
        store.list_recent_work(since=_TEST_EPOCH, cursor="not-base64-or-json")


def test_paginated_sessions_to_dict_summary_omits_narrative_fields():
    """``PaginatedSessions.to_dict(summary=True)`` drops the verbose fields."""
    from core.l6_store import PaginatedSessions, SessionRef

    page = PaginatedSessions(
        sessions=[
            SessionRef(
                agent="a",
                date="2026-04-15",
                key_actions=["lots of narrative"],
                files_touched=["a.py", "b.py"],
                project_focus=["proj"],
            )
        ],
        next_cursor=None,
        has_more=False,
    )
    full = page.to_dict()
    assert "key_actions" in full["sessions"][0]
    assert "files_touched" in full["sessions"][0]

    summary = page.to_dict(summary=True)
    assert "key_actions" not in summary["sessions"][0]
    assert "files_touched" not in summary["sessions"][0]
    # Identifying fields stay.
    assert summary["sessions"][0]["agent"] == "a"
    assert summary["sessions"][0]["date"] == "2026-04-15"
    assert summary["sessions"][0]["project_focus"] == ["proj"]


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


# -- commit_l5 / write-side federation (issue #51) ----------------------------


def test_commit_l5_creates_new_manifest(library):
    """Fresh write of a previously-unknown agent."""
    store = L6Store(library["path"])
    result = store.commit_l5(
        "claude-desktop",
        agent_type="code-assistant",
        entities=[{"name": "Bourdon", "type": "project", "summary": "a thing"}],
        sessions=[{"date": "2026-05-11", "cwd": "/tmp/foo"}],
    )
    assert result["entities_added"] == 1
    assert result["sessions_added"] == 1
    assert result["total_entities"] == 1
    assert result["total_sessions"] == 1
    # Manifest persisted to disk
    assert (library["agents_dir"] / "claude-desktop.l5.yaml").is_file()
    # And immediately visible via the read APIs (reload happens in commit).
    assert "claude-desktop" in store.list_agents()


def test_commit_l5_merge_unions_list_fields_and_overwrites_scalars(library):
    """A second call merges: existing entity gets tags/aliases unioned,
    scalar fields overwritten by the new value."""
    store = L6Store(library["path"])
    store.commit_l5(
        "claude-desktop",
        agent_type="code-assistant",
        entities=[
            {
                "name": "Bourdon",
                "type": "project",
                "summary": "v1 summary",
                "tags": ["ai", "memory"],
                "aliases": ["bourdon-cli"],
            }
        ],
    )
    result = store.commit_l5(
        "claude-desktop",
        entities=[
            {
                "name": "Bourdon",
                "summary": "v2 summary",          # overwrite
                "tags": ["memory", "federation"],  # union -> ai, memory, federation
                "aliases": ["bourdon-mcp"],        # union -> bourdon-cli, bourdon-mcp
            }
        ],
    )
    assert result["entities_updated"] == 1
    assert result["entities_added"] == 0

    manifest = store.get_agent_manifest("claude-desktop", access_level="team")
    bourdon = next(e for e in manifest["known_entities"] if e["name"] == "Bourdon")
    assert bourdon["summary"] == "v2 summary"
    assert set(bourdon["tags"]) == {"ai", "memory", "federation"}
    assert set(bourdon["aliases"]) == {"bourdon-cli", "bourdon-mcp"}


def test_commit_l5_merge_session_dedupes_by_date_and_cwd(library):
    store = L6Store(library["path"])
    store.commit_l5(
        "claude-desktop",
        agent_type="code-assistant",
        sessions=[
            {
                "date": "2026-05-11",
                "cwd": "/tmp/foo",
                "key_actions": ["first action"],
            }
        ],
    )
    result = store.commit_l5(
        "claude-desktop",
        sessions=[
            # Same (date, cwd) -> updates the existing row.
            {
                "date": "2026-05-11",
                "cwd": "/tmp/foo",
                "key_actions": ["second action"],
            },
            # Different cwd -> new row.
            {
                "date": "2026-05-11",
                "cwd": "/tmp/bar",
                "key_actions": ["other project"],
            },
        ],
    )
    assert result["sessions_added"] == 1
    assert result["sessions_updated"] == 1
    assert result["total_sessions"] == 2

    manifest = store.get_agent_manifest("claude-desktop", access_level="team")
    foo = next(s for s in manifest["recent_sessions"] if s.get("cwd") == "/tmp/foo")
    assert set(foo["key_actions"]) == {"first action", "second action"}


def test_commit_l5_replace_mode_wipes_existing(library):
    store = L6Store(library["path"])
    store.commit_l5(
        "claude-desktop",
        agent_type="code-assistant",
        entities=[{"name": "OldEntity"}, {"name": "AnotherOldEntity"}],
        sessions=[{"date": "2026-05-01", "cwd": "/old"}],
    )
    result = store.commit_l5(
        "claude-desktop",
        agent_type="code-assistant",
        entities=[{"name": "OnlyNew"}],
        mode="replace",
    )
    assert result["mode"] == "replace"
    assert result["total_entities"] == 1
    assert result["total_sessions"] == 0
    manifest = store.get_agent_manifest("claude-desktop", access_level="team")
    names = {e["name"] for e in manifest["known_entities"]}
    assert names == {"OnlyNew"}
    assert manifest["recent_sessions"] == []


def test_commit_l5_requires_agent_type_for_new_manifest(library):
    store = L6Store(library["path"])
    with pytest.raises(ValueError, match="agent_type is required"):
        store.commit_l5("newagent", entities=[{"name": "X"}])


def test_commit_l5_inherits_agent_type_on_merge(library):
    """After the first call sets agent_type, subsequent merges don't need it."""
    store = L6Store(library["path"])
    store.commit_l5("agent-x", agent_type="other", entities=[{"name": "A"}])
    # Second call omits agent_type -> should succeed by inheriting from disk.
    result = store.commit_l5("agent-x", entities=[{"name": "B"}])
    assert result["total_entities"] == 2


def test_commit_l5_rejects_invalid_agent_id(library):
    store = L6Store(library["path"])
    with pytest.raises(ValueError, match="invalid agent_id"):
        store.commit_l5("Invalid Agent ID", agent_type="other")
    with pytest.raises(ValueError, match="invalid agent_id"):
        store.commit_l5("", agent_type="other")
    with pytest.raises(ValueError, match="invalid agent_id"):
        store.commit_l5("-leading-dash", agent_type="other")


def test_commit_l5_rejects_invalid_agent_type(library):
    store = L6Store(library["path"])
    with pytest.raises(ValueError, match="not in the L5 schema enum"):
        store.commit_l5("agent-x", agent_type="bogus-category")


def test_commit_l5_rejects_invalid_mode(library):
    store = L6Store(library["path"])
    with pytest.raises(ValueError, match="invalid mode"):
        store.commit_l5("agent-x", agent_type="other", mode="upsert")


def test_commit_l5_rejects_entity_missing_name(library):
    store = L6Store(library["path"])
    with pytest.raises(ValueError, match="missing non-empty 'name'"):
        store.commit_l5(
            "agent-x", agent_type="other", entities=[{"type": "concept"}]
        )


def test_commit_l5_rejects_session_missing_date(library):
    store = L6Store(library["path"])
    with pytest.raises(ValueError, match="missing non-empty 'date'"):
        store.commit_l5(
            "agent-x", agent_type="other", sessions=[{"cwd": "/no/date"}]
        )


def test_commit_l5_persists_across_l6store_instances(library):
    """A write through one L6Store instance is visible to a fresh instance --
    proves atomicity isn't accidentally using in-memory-only state."""
    store_a = L6Store(library["path"])
    store_a.commit_l5(
        "claude-desktop",
        agent_type="code-assistant",
        entities=[{"name": "Persisted"}],
    )
    # Independent instance reads from disk.
    store_b = L6Store(library["path"])
    matches = store_b.find_entity("Persisted", access_level="team")
    assert len(matches) == 1
    assert matches[0].agents == ["claude-desktop"]


def test_commit_l5_role_narrative_overwrites_on_subsequent_write(library):
    store = L6Store(library["path"])
    store.commit_l5(
        "agent-x",
        agent_type="code-assistant",
        role_narrative="initial role description",
        entities=[{"name": "X"}],
    )
    store.commit_l5(
        "agent-x",
        role_narrative="updated role description",
    )
    manifest = store.get_agent_manifest("agent-x", access_level="team")
    assert manifest["agent"]["role_narrative"] == "updated role description"


# -- build_recognition_manifest unified dedupe (Finding #1 + #3) -------------


def test_build_recognition_manifest_dedupes_by_name_across_types(library):
    """Finding #1: same name with different types collapses to ONE row
    with `types` listing both. Previously this would have been two rows
    keyed by (name, type)."""
    library["write"](
        "agent-a",
        _manifest(
            "agent-a",
            entities=[
                {"name": "Bourdon", "type": "project", "summary": "from A"}
            ],
        ),
    )
    library["write"](
        "agent-b",
        _manifest(
            "agent-b",
            entities=[
                {"name": "Bourdon", "type": "concept", "summary": "from B"}
            ],
        ),
    )
    store = L6Store(library["path"])
    rec = store.build_recognition_manifest()
    bourdon_rows = [
        e for e in rec["known_entities"]
        if e.get("name", "").lower() == "bourdon"
    ]
    assert len(bourdon_rows) == 1, (
        f"name-only dedupe should produce one row, got {len(bourdon_rows)}"
    )
    row = bourdon_rows[0]
    assert set(row["source_agents"]) == {"agent-a", "agent-b"}
    assert set(row["types"]) == {"project", "concept"}
    # `type` (singular) stays for backward compat: first-seen wins.
    assert row["type"] in {"project", "concept"}


def test_build_recognition_manifest_types_field_is_unique(library):
    """`types` list shouldn't accumulate duplicates when many agents
    share the same type for the same name."""
    library["write"](
        "agent-a",
        _manifest("agent-a", entities=[{"name": "X", "type": "project"}]),
    )
    library["write"](
        "agent-b",
        _manifest("agent-b", entities=[{"name": "X", "type": "project"}]),
    )
    library["write"](
        "agent-c",
        _manifest("agent-c", entities=[{"name": "X", "type": "topic"}]),
    )
    store = L6Store(library["path"])
    rec = store.build_recognition_manifest()
    row = next(e for e in rec["known_entities"] if e["name"] == "X")
    assert row["types"] == ["project", "topic"] or row["types"] == ["topic", "project"]
    # Both unique, in some order; total len 2 not 3.
    assert len(row["types"]) == 2


def test_find_entity_and_recognition_manifest_agree_on_row_count(library):
    """Finding #3: the two surfaces should agree on row count for the
    same shared entity. Previously find_entity (name-only dedupe)
    returned 1, recognition_manifest (name+type dedupe) could return >1."""
    library["write"](
        "agent-a",
        _manifest("agent-a", entities=[{"name": "Bourdon", "type": "project"}]),
    )
    library["write"](
        "agent-b",
        _manifest("agent-b", entities=[{"name": "Bourdon", "type": "concept"}]),
    )
    library["write"](
        "agent-c",
        _manifest("agent-c", entities=[{"name": "Bourdon", "type": "topic"}]),
    )
    store = L6Store(library["path"])
    find_matches = store.find_entity("Bourdon")
    rec = store.build_recognition_manifest()
    rec_rows = [
        e for e in rec["known_entities"]
        if e.get("name", "").lower() == "bourdon"
    ]
    assert len(find_matches) == len(rec_rows) == 1


# -- Default access_level env var (Finding #2) -------------------------------


def test_resolve_access_level_env_var_flips_default(monkeypatch):
    """BOURDON_DEFAULT_ACCESS_LEVEL=team flips the default for single-user
    federations so participants that tag entities `team` aren't silently
    filtered from naive queries."""
    from core.l6_store import _resolve_access_level

    monkeypatch.delenv("BOURDON_DEFAULT_ACCESS_LEVEL", raising=False)
    assert _resolve_access_level() == "public"

    monkeypatch.setenv("BOURDON_DEFAULT_ACCESS_LEVEL", "team")
    assert _resolve_access_level() == "team"

    monkeypatch.setenv("BOURDON_DEFAULT_ACCESS_LEVEL", "private")
    assert _resolve_access_level() == "private"


def test_resolve_access_level_explicit_argument_beats_env(monkeypatch):
    """An explicit access_level= always wins over the env var."""
    from core.l6_store import _resolve_access_level

    monkeypatch.setenv("BOURDON_DEFAULT_ACCESS_LEVEL", "team")
    assert _resolve_access_level(access_level="public") == "public"
    assert _resolve_access_level(access_level="private") == "private"


def test_resolve_access_level_include_private_beats_env(monkeypatch):
    """The legacy `include_private=True` shim still resolves to `private`
    even when the env var would have said `team`."""
    from core.l6_store import _resolve_access_level

    monkeypatch.setenv("BOURDON_DEFAULT_ACCESS_LEVEL", "team")
    assert _resolve_access_level(include_private=True) == "private"


def test_resolve_access_level_bad_env_value_falls_back_to_public(
    monkeypatch, caplog
):
    """A bogus env value logs a warning and falls back to `public`."""
    from core.l6_store import _resolve_access_level

    monkeypatch.setenv("BOURDON_DEFAULT_ACCESS_LEVEL", "ULTRA_SECRET")
    with caplog.at_level("WARNING"):
        assert _resolve_access_level() == "public"
    assert any(
        "BOURDON_DEFAULT_ACCESS_LEVEL" in r.message and "public" in r.message
        for r in caplog.records
    )


def test_env_default_access_level_affects_find_entity(library, monkeypatch):
    """End-to-end: with the env var set to `team`, find_entity() without
    an explicit access_level surfaces team-tagged entities."""
    library["write"](
        "agent-a",
        _manifest(
            "agent-a",
            entities=[
                {"name": "TeamOnly", "type": "topic", "visibility": "team"},
            ],
        ),
    )
    store = L6Store(library["path"])

    monkeypatch.delenv("BOURDON_DEFAULT_ACCESS_LEVEL", raising=False)
    assert store.find_entity("TeamOnly") == []

    monkeypatch.setenv("BOURDON_DEFAULT_ACCESS_LEVEL", "team")
    matches = store.find_entity("TeamOnly")
    assert len(matches) == 1
    assert matches[0].agents == ["agent-a"]


def test_commit_l5_sessions_sorted_newest_first_after_write(library):
    store = L6Store(library["path"])
    store.commit_l5(
        "agent-x",
        agent_type="other",
        sessions=[
            {"date": "2026-04-01", "cwd": "/a"},
            {"date": "2026-05-11", "cwd": "/b"},
            {"date": "2026-04-15", "cwd": "/c"},
        ],
    )
    manifest = store.get_agent_manifest("agent-x", access_level="team")
    dates = [s["date"] for s in manifest["recent_sessions"]]
    assert dates == sorted(dates, reverse=True)
