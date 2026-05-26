"""Tests for adapters.codex -- Codex CLI external adapter."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from adapters import codex as codex_module
from adapters.base import (
    SPEC_VERSION,
    AdapterDiscoveryError,
    BourdonAdapter,
    Entity,
    HealthStatus,
    L5Manifest,
    Visibility,
)
from adapters.codex import (
    CodexAdapter,
    _collect_session_records,
    _extract_project_candidates,
    _find_rollout_file,
    _inspect_codex_fallback_recall,
    _inspect_codex_state_db,
    _is_junk_entity,
    _normalize_local_path,
    _parse_memory_text,
    _parse_session_index,
    _project_key_from_cwd,
    _read_session_meta,
    _render_codex_native_memory_text,
    _resolve_codex_home,
    _timestamp_to_iso_date,
)

# -- Fixture -------------------------------------------------------------------


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    """Redirect Path.home() at a tmp dir so adapter resolvers don't see the
    host machine's real ~/.codex/."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.setattr(Path, "home", lambda: fake_home)

    def create_codex_home():
        (fake_home / ".codex").mkdir()
        (fake_home / ".codex" / "sessions").mkdir()
        return fake_home / ".codex"

    def create_memories():
        memories = fake_home / ".codex" / "memories"
        (memories / "rollout_summaries").mkdir(parents=True, exist_ok=True)
        return memories

    def add_index_entry(codex_home, session_id: str, thread_name: str, updated_at: str):
        idx = codex_home / "session_index.jsonl"
        with open(idx, "a", encoding="utf-8") as f:
            f.write(
                json.dumps(
                    {
                        "id": session_id,
                        "thread_name": thread_name,
                        "updated_at": updated_at,
                    }
                )
                + "\n"
            )

    def add_rollout(
        codex_home,
        date_parts: tuple[int, int, int],
        session_id: str,
        cwd: str,
        timestamp: str,
        extra_records: list[dict] | None = None,
        meta_extra: dict | None = None,
    ):
        y, m, d = date_parts
        date_dir = codex_home / "sessions" / f"{y:04d}" / f"{m:02d}" / f"{d:02d}"
        date_dir.mkdir(parents=True, exist_ok=True)
        rollout_name = (
            f"rollout-{timestamp.replace(':', '-').replace('.', '-')}-{session_id}.jsonl"
        )
        rollout = date_dir / rollout_name
        payload = {"id": session_id, "timestamp": timestamp, "cwd": cwd}
        if meta_extra:
            payload.update(meta_extra)
        with open(rollout, "w", encoding="utf-8") as f:
            f.write(
                json.dumps({"timestamp": timestamp, "type": "session_meta", "payload": payload})
                + "\n"
            )
            f.write(
                json.dumps(
                    {
                        "timestamp": timestamp,
                        "type": "user_input",
                        "payload": {"text": "hi"},
                    }
                )
                + "\n"
            )
            for record in extra_records or []:
                f.write(json.dumps(record) + "\n")
        return rollout

    def create_codex_brain():
        cb = fake_home / "codex-brain"
        cb.mkdir()
        (cb / "CURRENT.md.txt").write_text("# Current focus\n", encoding="utf-8")
        return cb

    def write_memory_file(relative_path: str, content: str):
        memories = create_memories()
        target = memories / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return target

    def write_codex_brain_file(relative_path: str, content: str):
        cb = fake_home / "codex-brain"
        cb.mkdir(exist_ok=True)
        target = cb / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return target

    return {
        "home": fake_home,
        "create_codex_home": create_codex_home,
        "create_memories": create_memories,
        "add_index_entry": add_index_entry,
        "add_rollout": add_rollout,
        "create_codex_brain": create_codex_brain,
        "write_memory_file": write_memory_file,
        "write_codex_brain_file": write_codex_brain_file,
    }


def _write_state_thread(
    codex_home: Path,
    *,
    session_id: str,
    title: str,
    updated_at: str = "2026-05-13T12:00:00Z",
    first_user_message: str = "",
    cwd: str = "/workspace/bourdon",
    rollout_path: str | None = None,
) -> None:
    db_path = codex_home / "state_5.sqlite"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS threads ("
            "id TEXT PRIMARY KEY, "
            "title TEXT, "
            "first_user_message TEXT, "
            "cwd TEXT, "
            "rollout_path TEXT, "
            "model_provider TEXT, "
            "cli_version TEXT, "
            "source TEXT, "
            "memory_mode TEXT, "
            "archived INTEGER, "
            "updated_at TEXT, "
            "created_at TEXT)"
        )
        conn.execute(
            "INSERT INTO threads "
            "(id, title, first_user_message, cwd, rollout_path, model_provider, "
            "cli_version, source, memory_mode, archived, updated_at, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                session_id,
                title,
                first_user_message,
                cwd,
                rollout_path,
                "openai",
                "0.130.0-alpha.5",
                "vscode",
                "enabled",
                0,
                updated_at,
                updated_at,
            ),
        )


# -- _is_junk_entity (issue #78) -----------------------------------------------


def _make_entity(
    name: str,
    *,
    type: str | None = "topic",
    summary: str | None = None,
    aliases: list[str] | None = None,
    tags: list[str] | None = None,
) -> Entity:
    return Entity(
        name=name,
        type=type,
        aliases=aliases or [],
        summary=summary,
        tags=tags or [],
        visibility=Visibility.TEAM,
    )


def test_is_junk_entity_drops_placeholder_name_patterns():
    """Issue #78: 'Project 2', 'New Project', 'Project Name', 'untitled' are noise."""
    for placeholder in (
        "Project 2",
        "Project2",
        "New Project",
        "New Project 3",
        "Project Name",
        "untitled",
        "Brief description of the project.",
    ):
        assert _is_junk_entity(_make_entity(placeholder)), placeholder


def test_is_junk_entity_drops_single_word_noise_names():
    """Single common words ('memories', 'notes', 'temp') carry no recall signal."""
    for noise in ("memories", "memory", "notes", "Note", "TEMP", "tests", "todo"):
        assert _is_junk_entity(_make_entity(noise)), noise


def test_is_junk_entity_drops_observed_across_placeholder_summary():
    """An entity whose only signal is 'Observed across N session(s)' is junk."""
    e = _make_entity(
        "some-cwd-name",
        type="project",
        summary="Observed across 3 Codex session(s).",
        tags=["codex-project"],
    )
    assert _is_junk_entity(e)


def test_is_junk_entity_keeps_observed_across_with_descriptive_tag():
    """If the entity carries non-canonical tags or aliases, keep it."""
    e = _make_entity(
        "some-cwd-name",
        type="project",
        summary="Observed across 3 Codex session(s).",
        tags=["codex-project", "rust-port"],
    )
    assert not _is_junk_entity(e)


def test_is_junk_entity_keeps_codex_thread_with_meaningful_name():
    """'Codex thread: X' is fine when X is meaningful."""
    e = _make_entity(
        "Align workflow with Codex",
        type="topic",
        summary="Codex thread: Align workflow with Codex",
        tags=["codex-thread"],
    )
    assert not _is_junk_entity(e)


def test_is_junk_entity_drops_codex_thread_with_noise_name():
    """'Codex thread: Project 2' is junk because the name is noise."""
    e = _make_entity(
        "Project 2",
        type="topic",
        summary="Codex thread: Project 2",
        tags=["codex-thread"],
    )
    assert _is_junk_entity(e)


def test_is_junk_entity_keeps_real_project_entity():
    e = _make_entity(
        "Bourdon",
        type="project",
        summary="Cross-agent memory federation protocol",
        tags=["codex-project"],
    )
    assert not _is_junk_entity(e)


def test_is_junk_entity_drops_very_short_names_except_known_acronyms():
    assert _is_junk_entity(_make_entity("ab"))
    assert _is_junk_entity(_make_entity("x"))
    # Common 3-letter acronyms are kept.
    assert not _is_junk_entity(_make_entity("NAS"))
    assert not _is_junk_entity(_make_entity("iOS"))
    assert not _is_junk_entity(_make_entity("git"))


def test_is_junk_entity_drops_empty_name():
    assert _is_junk_entity(_make_entity(""))
    assert _is_junk_entity(_make_entity("   "))


# -- Protocol + constants ------------------------------------------------------


def test_adapter_satisfies_protocol(isolated_home):
    assert isinstance(CodexAdapter(), BourdonAdapter)


def test_adapter_constants():
    assert codex_module.AGENT_ID == "codex"
    assert codex_module.AGENT_TYPE == "code-assistant"
    # role_narrative differentiates Codex from sibling code-assistants
    assert isinstance(codex_module.ROLE_NARRATIVE, str)
    assert len(codex_module.ROLE_NARRATIVE) <= 500
    assert "lead" in codex_module.ROLE_NARRATIVE.lower()


# -- Path resolution -----------------------------------------------------------


def test_normalize_local_path_converts_windows_path_on_wsl():
    path = _normalize_local_path(r"C:\Users\cumul\.codex", os_name="posix")

    assert str(path).replace("\\", "/") == "/mnt/c/Users/cumul/.codex"


def test_normalize_local_path_converts_wsl_path_for_windows_python():
    path = _normalize_local_path("/mnt/c/Users/cumul/.codex", os_name="nt")

    assert str(path).replace("/", "\\") == r"C:\Users\cumul\.codex"


def test_resolve_codex_home_prefers_codex_home_env(isolated_home, monkeypatch):
    env_codex_home = isolated_home["home"] / "env-codex"
    env_codex_home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(env_codex_home))

    assert _resolve_codex_home() == env_codex_home


# -- discover + health_check ---------------------------------------------------


def test_discover_raises_when_no_codex_home(isolated_home):
    adapter = CodexAdapter()
    with pytest.raises(AdapterDiscoveryError):
        adapter.discover()


def test_discover_ok_with_index_and_sessions(isolated_home):
    codex_home = isolated_home["create_codex_home"]()
    isolated_home["add_index_entry"](
        codex_home, "id1", "Thread 1", "2026-04-15T12:00:00Z"
    )
    adapter = CodexAdapter()
    store = adapter.discover()
    assert store.metadata["sources"]["codex_home"] is not None
    assert store.metadata["sources"]["session_index"] is not None
    assert store.metadata["sources"]["sessions_dir"] is not None


def test_health_ok_with_all_present(isolated_home):
    codex_home = isolated_home["create_codex_home"]()
    isolated_home["add_index_entry"](
        codex_home, "id1", "Thread", "2026-04-15T12:00:00Z"
    )
    assert CodexAdapter().health_check().status == "ok"


def test_health_degraded_missing_session_index(isolated_home):
    isolated_home["create_codex_home"]()
    # No session_index.jsonl written
    health = CodexAdapter().health_check()
    assert health.status == "degraded"
    assert "session_index" in health.reason
    assert "state_db_records" not in health.reason


def test_health_blocked_missing_codex_home(isolated_home):
    health = CodexAdapter().health_check()
    assert health.status == "blocked"


def test_health_check_never_raises(isolated_home):
    assert isinstance(CodexAdapter().health_check(), HealthStatus)


# -- Parsing helpers -----------------------------------------------------------


def test_parse_session_index_newest_first(tmp_path):
    idx = tmp_path / "session_index.jsonl"
    with open(idx, "w", encoding="utf-8") as f:
        f.write(
            json.dumps({"id": "a", "thread_name": "Old", "updated_at": "2026-04-10T00:00:00Z"})
            + "\n"
        )
        f.write(
            json.dumps({"id": "b", "thread_name": "New", "updated_at": "2026-04-15T00:00:00Z"})
            + "\n"
        )
    entries = _parse_session_index(idx)
    assert [e["id"] for e in entries] == ["b", "a"]


def test_parse_session_index_skips_malformed_lines(tmp_path, caplog):
    idx = tmp_path / "session_index.jsonl"
    with open(idx, "w", encoding="utf-8") as f:
        f.write(
            json.dumps(
                {
                    "id": "a",
                    "thread_name": "Good",
                    "updated_at": "2026-04-15T00:00:00Z",
                }
            )
            + "\n"
        )
        f.write("not json at all\n")
        f.write(
            json.dumps(
                {
                    "id": "b",
                    "thread_name": "Also good",
                    "updated_at": "2026-04-14T00:00:00Z",
                }
            )
            + "\n"
        )
    with caplog.at_level("WARNING"):
        entries = _parse_session_index(idx)
    assert {e["id"] for e in entries} == {"a", "b"}


def test_parse_session_index_empty_on_missing_file(tmp_path):
    assert _parse_session_index(tmp_path / "nothing.jsonl") == []


def test_find_rollout_file_matches_by_id(isolated_home):
    codex_home = isolated_home["create_codex_home"]()
    isolated_home["add_rollout"](
        codex_home,
        (2026, 4, 15),
        "abc123",
        "/tmp",
        "2026-04-15T12:00:00Z",
    )
    found = _find_rollout_file(codex_home, "abc123")
    assert found is not None
    assert "abc123" in found.name


def test_find_rollout_file_matches_archived_session_by_id(isolated_home):
    codex_home = isolated_home["create_codex_home"]()
    archived_dir = codex_home / "archived_sessions"
    archived_dir.mkdir()
    archived_rollout = archived_dir / "rollout-2026-04-15T12-00-00Z-archived123.jsonl"
    archived_rollout.write_text(
        json.dumps(
            {
                "timestamp": "2026-04-15T12:00:00Z",
                "type": "session_meta",
                "payload": {
                    "id": "archived123",
                    "timestamp": "2026-04-15T12:00:00Z",
                    "cwd": "/workspace/archived",
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    found = _find_rollout_file(codex_home, "archived123")

    assert found == archived_rollout


def test_find_rollout_file_returns_none_when_missing(isolated_home):
    codex_home = isolated_home["create_codex_home"]()
    assert _find_rollout_file(codex_home, "nope") is None


def test_read_session_meta_returns_payload(isolated_home):
    codex_home = isolated_home["create_codex_home"]()
    rollout = isolated_home["add_rollout"](
        codex_home, (2026, 4, 15), "abc", "/home/user", "2026-04-15T12:00:00Z"
    )
    meta = _read_session_meta(rollout)
    assert meta is not None
    assert meta["cwd"] == "/home/user"


def test_read_session_meta_returns_none_for_non_meta_first_line(tmp_path):
    rollout = tmp_path / "weird.jsonl"
    rollout.write_text(
        json.dumps({"type": "user_input", "payload": {"text": "hi"}}) + "\n",
        encoding="utf-8",
    )
    assert _read_session_meta(rollout) is None


def test_timestamp_parsing_accepts_z_suffix():
    assert _timestamp_to_iso_date("2026-04-15T12:00:00Z") == "2026-04-15"


def test_timestamp_parsing_accepts_microseconds_and_tz():
    assert _timestamp_to_iso_date("2026-04-15T12:00:00.123456+00:00") == "2026-04-15"


def test_timestamp_parsing_falls_back_to_prefix_for_unusual_format():
    # Not valid ISO, but starts with a date-shaped prefix
    assert _timestamp_to_iso_date("2026-04-15 weird stuff") == "2026-04-15"


def test_timestamp_parsing_returns_none_for_garbage():
    assert _timestamp_to_iso_date("totally broken") is None
    assert _timestamp_to_iso_date("") is None


def test_project_key_filters_generic_new_project_workspace():
    assert _project_key_from_cwd("/Users/radman/Documents/New project") is None
    assert _project_key_from_cwd("/workspace/bourdon") == "bourdon"


def test_parse_memory_text_stops_preference_capture_at_new_sections():
    parsed = _parse_memory_text(
        """## Preference signals

- prefer backend-first delivery

Key steps:
- implemented the API

Failures and how to do differently:
- do less guessing

References:
- docs/playbook.md
"""
    )

    assert parsed["preferences"] == ["prefer backend-first delivery"]
    assert "implemented the API" not in parsed["preferences"]
    assert "do less guessing" not in parsed["preferences"]
    assert "docs/playbook.md" not in parsed["preferences"]


def test_inspect_codex_state_db_reports_stage1_memory_failures(isolated_home):
    codex_home = isolated_home["create_codex_home"]()
    db_path = codex_home / "state_5.sqlite"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE threads ("
            "id TEXT PRIMARY KEY, "
            "memory_mode TEXT NOT NULL, "
            "archived INTEGER NOT NULL)"
        )
        conn.execute(
            "CREATE TABLE stage1_outputs ("
            "thread_id TEXT PRIMARY KEY, "
            "raw_memory TEXT NOT NULL, "
            "rollout_summary TEXT NOT NULL)"
        )
        conn.execute(
            "CREATE TABLE jobs ("
            "kind TEXT NOT NULL, "
            "job_key TEXT NOT NULL, "
            "status TEXT NOT NULL, "
            "retry_remaining INTEGER NOT NULL, "
            "last_error TEXT)"
        )
        conn.execute(
            "INSERT INTO threads (id, memory_mode, archived) VALUES ('thread-1', 'enabled', 0)"
        )
        conn.execute(
            "INSERT INTO jobs "
            "(kind, job_key, status, retry_remaining, last_error) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                "memory_stage1",
                "thread-1",
                "error",
                2,
                "You've hit your usage limit.",
            ),
        )

    report = _inspect_codex_state_db(codex_home)

    assert report["present"] is True
    assert report["readable"] is True
    assert report["stage1_outputs"]["total"] == 0
    assert report["threads"]["memory_enabled"] == 1
    assert report["threads"]["active"] == 1
    assert report["memory_stage1_jobs"]["by_status"] == {"error": 1}
    assert report["memory_stage1_jobs"]["errors"][0]["retry_remaining"] == 2
    assert "usage limit" in report["memory_stage1_jobs"]["errors"][0]["last_error"]


def test_inspect_codex_fallback_recall_reports_session_rollout_coverage(isolated_home):
    codex_home = isolated_home["create_codex_home"]()
    isolated_home["add_index_entry"](
        codex_home,
        "bourdon1",
        "Diagnose Bourdon runtime recognition memory",
        "2026-05-07T12:00:00Z",
    )
    isolated_home["add_rollout"](
        codex_home,
        (2026, 5, 7),
        "bourdon1",
        "/workspace/bourdon",
        "2026-05-07T12:00:00Z",
        extra_records=[
            {
                "timestamp": "2026-05-07T12:01:00Z",
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "name": "apply_patch",
                    "arguments": (
                        "*** Begin Patch\n"
                        "*** Update File: adapters/codex.py\n"
                        "@@\n"
                        "-old\n"
                        "+new\n"
                        "*** End Patch\n"
                    ),
                },
            }
        ],
    )
    isolated_home["write_memory_file"](
        "raw_memories.md",
        "# Raw Memories\n\nNo raw memories yet.\n",
    )

    report = _inspect_codex_fallback_recall(codex_home, codex_brain=None)

    assert report["status"] == "available"
    assert report["active"] is True
    assert report["reason"] == "codex_distilled_memory_empty"
    assert report["session_records"] == 1
    assert report["rollout_records"] == 1
    assert report["file_evidence_sessions"] == 1
    assert report["fallback_memory_items"] >= 1
    assert report["project_candidates"] == ["Bourdon"]


def test_inspect_codex_fallback_recall_ignores_bourdon_owned_memory_md_section(
    isolated_home,
):
    codex_home = isolated_home["create_codex_home"]()
    isolated_home["add_index_entry"](
        codex_home,
        "fallback1",
        "Build Bourdon memory doctor",
        "2026-05-07T12:00:00Z",
    )
    isolated_home["add_rollout"](
        codex_home,
        (2026, 5, 7),
        "fallback1",
        "/workspace/bourdon",
        "2026-05-07T12:00:00Z",
    )
    isolated_home["write_memory_file"](
        "MEMORY.md",
        (
            "<!-- BEGIN BOURDON FALLBACK MEMORY -->\n"
            "# Bourdon Fallback Memory\n"
            "\n"
            "Generated by Bourdon from Codex session and rollout metadata.\n"
            "<!-- END BOURDON FALLBACK MEMORY -->\n"
        ),
    )

    report = _inspect_codex_fallback_recall(codex_home, codex_brain=None)

    assert report["distilled_memory_items"] == 0
    assert report["active"] is True
    assert report["reason"] == "codex_distilled_memory_empty"


def test_render_codex_native_memory_redacts_credential_like_thread_titles(isolated_home):
    codex_home = isolated_home["create_codex_home"]()
    isolated_home["add_index_entry"](
        codex_home,
        "secret1",
        "Debug API_KEY=super-secret for Bourdon",
        "2026-05-07T12:00:00Z",
    )
    isolated_home["add_rollout"](
        codex_home,
        (2026, 5, 7),
        "secret1",
        "/workspace/bourdon",
        "2026-05-07T12:00:00Z",
    )

    text = _render_codex_native_memory_text(codex_home, codex_brain=None)

    assert "API_KEY" not in text
    assert "super-secret" not in text
    assert "[redacted credential-like text]" in text
    assert "Bourdon" in text


def test_render_codex_native_memory_extracts_bourdon_thesis_from_user_prompt(
    isolated_home,
):
    codex_home = isolated_home["create_codex_home"]()
    isolated_home["add_index_entry"](
        codex_home,
        "bourdon-thesis",
        "Review Bourdon memory integration",
        "2026-05-07T12:00:00Z",
    )
    isolated_home["add_rollout"](
        codex_home,
        (2026, 5, 7),
        "bourdon-thesis",
        "/Users/radman/shipstable",
        "2026-05-07T12:00:00Z",
        extra_records=[
            {
                "timestamp": "2026-05-07T12:01:00Z",
                "type": "user_input",
                "payload": {
                    "text": (
                        "We renamed Continuo to Bourdon. The project is about "
                        "runtime recognition and a recognition timing layer "
                        "for natural AI communication."
                    ),
                },
            }
        ],
    )

    text = _render_codex_native_memory_text(codex_home, codex_brain=None)

    assert "Bourdon" in text
    assert "Continuo" in text
    assert "runtime recognition" in text
    assert "recognition timing layer" in text


def test_render_codex_native_memory_extracts_concepts_from_response_item_user_message(
    isolated_home,
):
    codex_home = isolated_home["create_codex_home"]()
    isolated_home["add_index_entry"](
        codex_home,
        "response-item-user-message",
        "Review Bourdon memory integration",
        "2026-05-07T12:00:00Z",
    )
    isolated_home["add_rollout"](
        codex_home,
        (2026, 5, 7),
        "response-item-user-message",
        "/Users/radman/shipstable",
        "2026-05-07T12:00:00Z",
        extra_records=[
            {
                "timestamp": "2026-05-07T12:01:00Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": (
                                "Continuo became Bourdon and needs runtime "
                                "recognition as a recognition timing layer."
                            ),
                        }
                    ],
                },
            }
        ],
    )

    text = _render_codex_native_memory_text(codex_home, codex_brain=None)

    assert "Bourdon" in text
    assert "Continuo" in text
    assert "runtime recognition" in text
    assert "recognition timing layer" in text


def test_render_codex_native_memory_extracts_concepts_from_event_user_message(
    isolated_home,
):
    codex_home = isolated_home["create_codex_home"]()
    isolated_home["add_index_entry"](
        codex_home,
        "event-user-message",
        "Review Bourdon memory integration",
        "2026-05-07T12:00:00Z",
    )
    isolated_home["add_rollout"](
        codex_home,
        (2026, 5, 7),
        "event-user-message",
        "/Users/radman/shipstable",
        "2026-05-07T12:00:00Z",
        extra_records=[
            {
                "timestamp": "2026-05-07T12:01:00Z",
                "type": "event_msg",
                "payload": {
                    "type": "user_message",
                    "message": (
                        "Continuo became Bourdon and needs run time "
                        "recognition for natural AI communication."
                    ),
                },
            }
        ],
    )

    text = _render_codex_native_memory_text(codex_home, codex_brain=None)

    assert "Bourdon" in text
    assert "Continuo" in text
    assert "runtime recognition" in text
    assert "natural AI communication" in text


def test_export_l5_promotes_recovered_fallback_concepts_to_topic_entities(
    isolated_home,
):
    codex_home = isolated_home["create_codex_home"]()
    isolated_home["add_index_entry"](
        codex_home,
        "bourdon-concepts",
        "Review Bourdon memory integration",
        "2026-05-07T12:00:00Z",
    )
    isolated_home["add_rollout"](
        codex_home,
        (2026, 5, 7),
        "bourdon-concepts",
        "/Users/radman/shipstable",
        "2026-05-07T12:00:00Z",
        extra_records=[
            {
                "timestamp": "2026-05-07T12:01:00Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": (
                                "Continuo became Bourdon and needs run time "
                                "recognition as a recognition timing layer."
                            ),
                        }
                    ],
                },
            }
        ],
    )

    manifest = CodexAdapter().export_l5()
    topics = {
        entity.name: entity
        for entity in manifest.known_entities
        if entity.type == "topic"
    }

    assert "Bourdon" in topics
    assert "Continuo" in topics
    assert "runtime recognition" in topics
    assert "recognition timing layer" in topics
    assert "codex-fallback-concept" in topics["Bourdon"].tags
    assert "run time recognition" in topics["runtime recognition"].aliases


def test_extract_project_candidates_filters_glue_words_and_generic_workstreams():
    candidates = _extract_project_candidates(
        {
            "keywords": [
                "Coolculator",
                "ShipStable",
                "Android Studio",
                "Claude",
                "Google Play Console",
                "OneDrive",
                "robocopy",
            ],
            "task_groups": [
                "Claude workflow handoff",
                "Coolculator monorepo bootstrap and Windows-to-Mac handoff",
                "OneDrive restore handoff",
                "robocopy backup handoff",
                "ShipStable reset handoff",
                "Windows fresh-install recovery for drive layout",
            ],
            "task_titles": [
                "Align workflow with Codex",
                "Review Android parity changes",
                "Locate DNS handoff doc",
                "Install Android Studio and restore GitHub access",
            ],
        }
    )

    assert "coolculator" in candidates
    assert "shipstable" in candidates
    assert "and" not in candidates
    assert "align" not in candidates
    assert "android" not in candidates
    assert "claude" not in candidates
    assert "install" not in candidates
    assert "onedrive" not in candidates
    assert "robocopy" not in candidates


# -- export_sessions -----------------------------------------------------------


def test_export_sessions_resolves_cwd_from_rollout(isolated_home):
    codex_home = isolated_home["create_codex_home"]()
    isolated_home["add_index_entry"](
        codex_home, "sess1", "Work thread", "2026-04-15T12:00:00Z"
    )
    isolated_home["add_rollout"](
        codex_home, (2026, 4, 15), "sess1", "/workspace/project", "2026-04-15T12:00:00Z"
    )
    sessions = CodexAdapter().export_sessions(since=datetime(2026, 4, 1, tzinfo=timezone.utc))
    assert len(sessions) == 1
    assert sessions[0].date == "2026-04-15"
    assert sessions[0].cwd == "/workspace/project"
    assert "Work thread" in sessions[0].key_actions[0]


def test_collect_session_records_prefers_live_sqlite_over_stale_session_index(
    isolated_home,
):
    codex_home = isolated_home["create_codex_home"]()
    isolated_home["add_index_entry"](
        codex_home,
        "stale",
        "Old April thread",
        "2026-04-15T12:00:00Z",
    )
    _write_state_thread(
        codex_home,
        session_id="live",
        title="Bourdon recognition first runtime layer",
        updated_at="2026-05-13T12:00:00Z",
        first_user_message=(
            "Continuo became Bourdon and needs runtime recognition."
        ),
    )

    records = _collect_session_records(codex_home)

    assert [record["id"] for record in records] == ["live"]
    assert records[0]["date"] == "2026-05-13"
    assert records[0]["thread_name"] == "Bourdon recognition first runtime layer"
    assert "Bourdon" in records[0]["fallback_concepts"]
    assert "runtime recognition" in records[0]["fallback_concepts"]


def test_collect_session_records_keeps_newer_unindexed_rollouts(
    isolated_home,
):
    codex_home = isolated_home["create_codex_home"]()
    isolated_home["add_index_entry"](
        codex_home,
        "stale",
        "Old April thread",
        "2026-04-15T12:00:00Z",
    )
    _write_state_thread(
        codex_home,
        session_id="state-thread",
        title="Known SQLite thread",
        updated_at="2026-04-20T12:00:00Z",
    )
    isolated_home["add_rollout"](
        codex_home,
        (2026, 5, 13),
        "current-rollout",
        "/workspace/bourdon",
        "2026-05-13T13:09:40Z",
        extra_records=[
            {
                "timestamp": "2026-05-13T13:10:00Z",
                "type": "user_input",
                "payload": {
                    "text": (
                        "Bourdon used to be Continuo. It is a recognition first "
                        "runtime layer."
                    )
                },
            }
        ],
    )

    records = _collect_session_records(codex_home)

    assert [record["id"] for record in records] == [
        "current-rollout",
        "state-thread",
    ]
    assert records[0]["thread_name"] == "Bourdon recognition first runtime layer"
    assert "Continuo" in records[0]["fallback_concepts"]
    assert "recognition first runtime layer" in records[0]["fallback_concepts"]


def test_collect_session_records_keeps_unindexed_rollouts_without_known_concepts(
    isolated_home,
):
    codex_home = isolated_home["create_codex_home"]()
    _write_state_thread(
        codex_home,
        session_id="state-thread",
        title="Known SQLite thread",
        updated_at="2026-04-20T12:00:00Z",
    )
    isolated_home["add_rollout"](
        codex_home,
        (2026, 5, 13),
        "plain-current-rollout",
        "/workspace/plain",
        "2026-05-13T13:09:40Z",
        extra_records=[
            {
                "timestamp": "2026-05-13T13:10:00Z",
                "type": "user_input",
                "payload": {
                    "text": "Please inspect the local logs before the next build."
                },
            }
        ],
    )

    records = _collect_session_records(codex_home)

    assert [record["id"] for record in records] == [
        "plain-current-rollout",
        "state-thread",
    ]
    assert records[0]["thread_name"] == "Codex session 2026-05-13"
    assert records[0]["fallback_concepts"] == []


def test_collect_session_records_keeps_same_day_unindexed_rollouts(
    isolated_home,
):
    """Regression: when an unindexed rollout shares the latest state-DB record's
    calendar date but has a different ID (e.g., a session created later that
    same day, not yet indexed), it must still appear in the merged output.
    excluded_ids already prevents true duplicates; the date filter must use
    < not <=, otherwise active same-day sessions get silently dropped --
    a recognition gap on the exact in-flight work Bourdon should be best at."""
    codex_home = isolated_home["create_codex_home"]()
    _write_state_thread(
        codex_home,
        session_id="state-thread-day13",
        title="Indexed thread",
        updated_at="2026-05-13T10:00:00Z",
    )
    isolated_home["add_rollout"](
        codex_home,
        (2026, 5, 13),
        "later-rollout-day13",
        "/workspace/bourdon",
        "2026-05-13T18:00:00Z",
    )

    records = _collect_session_records(codex_home)
    record_ids = {record["id"] for record in records}

    assert "state-thread-day13" in record_ids
    assert "later-rollout-day13" in record_ids, (
        "same-day unindexed rollout was dropped by overly conservative <= "
        "after_date filter (excluded_ids already prevents true duplicates)"
    )


def test_collect_session_records_pushes_limit_into_state_thread_query(
    isolated_home, monkeypatch
):
    """Regression: limit must be pushed down into _collect_state_thread_records
    so SQLite truncates with LIMIT instead of fetching all rows + doing
    expensive per-row I/O before _limit_session_records truncates in Python."""
    captured: dict[str, object] = {}
    original = codex_module._collect_state_thread_records

    def spy(codex_home, limit=None):
        captured["limit"] = limit
        return original(codex_home, limit=limit)

    monkeypatch.setattr(codex_module, "_collect_state_thread_records", spy)

    codex_home = isolated_home["create_codex_home"]()
    _write_state_thread(
        codex_home,
        session_id="t1",
        title="T1",
        updated_at="2026-04-20T12:00:00Z",
    )

    _collect_session_records(codex_home, limit=5)

    assert captured["limit"] == 5, (
        "limit was not pushed into _collect_state_thread_records; "
        "SQLite query will fetch all rows + do expensive I/O before truncating"
    )


def test_export_sessions_since_filter(isolated_home):
    codex_home = isolated_home["create_codex_home"]()
    isolated_home["add_index_entry"](codex_home, "new", "New", "2026-04-15T00:00:00Z")
    isolated_home["add_index_entry"](codex_home, "old", "Old", "2026-04-01T00:00:00Z")
    cutoff = datetime(2026, 4, 10, tzinfo=timezone.utc)
    sessions = CodexAdapter().export_sessions(since=cutoff)
    assert [s.date for s in sessions] == ["2026-04-15"]


def test_export_sessions_with_missing_rollout_still_emits_session(isolated_home):
    """If the rollout file isn't locatable, cwd falls back to None but session
    still appears. Codex session index is authoritative."""
    codex_home = isolated_home["create_codex_home"]()
    isolated_home["add_index_entry"](codex_home, "orphan", "No rollout", "2026-04-15T00:00:00Z")
    # No add_rollout call
    sessions = CodexAdapter().export_sessions(since=datetime(2026, 4, 1, tzinfo=timezone.utc))
    assert len(sessions) == 1
    assert sessions[0].cwd is None


def test_export_sessions_collects_files_touched_from_structured_apply_patch_only(
    isolated_home,
):
    codex_home = isolated_home["create_codex_home"]()
    isolated_home["add_index_entry"](
        codex_home,
        "sess1",
        "Patch some files",
        "2026-04-15T12:00:00Z",
    )
    isolated_home["add_rollout"](
        codex_home,
        (2026, 4, 15),
        "sess1",
        "/workspace/project",
        "2026-04-15T12:00:00Z",
        extra_records=[
            {
                "timestamp": "2026-04-15T12:01:00Z",
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "name": "apply_patch",
                    "arguments": (
                        "*** Begin Patch\n"
                        "*** Update File: apps/api/app.py\n"
                        "@@\n"
                        "-old\n"
                        "+new\n"
                        "*** End Patch\n"
                    ),
                },
            },
            {
                "timestamp": "2026-04-15T12:02:00Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "content": [
                        {
                            "type": "output_text",
                            "text": (
                                "I also mentioned docs/README.md in chat, but "
                                "that should not count."
                            ),
                        }
                    ],
                },
            },
        ],
    )

    sessions = CodexAdapter().export_sessions(
        since=datetime(2026, 4, 1, tzinfo=timezone.utc)
    )
    assert len(sessions) == 1
    assert sessions[0].files_touched == ["apps/api/app.py"]


def test_export_sessions_empty_when_no_codex_home(isolated_home):
    """Missing ~/.codex/ should not crash -- just return empty."""
    # Don't call create_codex_home
    with pytest.raises(AdapterDiscoveryError):
        CodexAdapter().discover()
    # But export_sessions directly bypasses discover
    sessions = CodexAdapter().export_sessions(since=datetime(2026, 4, 1, tzinfo=timezone.utc))
    assert sessions == []


# -- export_l5 -----------------------------------------------------------------


def test_export_l5_produces_valid_manifest(isolated_home):
    codex_home = isolated_home["create_codex_home"]()
    isolated_home["add_index_entry"](
        codex_home, "s1", "Set up CI", "2026-04-15T00:00:00Z"
    )
    isolated_home["add_index_entry"](
        codex_home, "s2", "Debug auth", "2026-04-14T00:00:00Z"
    )
    manifest = CodexAdapter().export_l5()
    assert isinstance(manifest, L5Manifest)
    assert manifest.agent.id == "codex"
    assert manifest.spec_version == SPEC_VERSION
    assert len(manifest.recent_sessions) == 2
    # Two unique thread_names -> two entities
    names = {e.name for e in manifest.known_entities}
    assert names == {"Set up CI", "Debug auth"}


def test_export_l5_uses_sqlite_thread_entities_for_current_recognition(
    isolated_home,
):
    codex_home = isolated_home["create_codex_home"]()
    _write_state_thread(
        codex_home,
        session_id="live-bourdon",
        title="Bourdon recognition first runtime layer",
        updated_at="2026-05-13T12:00:00Z",
        first_user_message=(
            "Continuo became Bourdon and needs runtime recognition as a "
            "recognition timing layer."
        ),
    )
    memories = isolated_home["create_memories"]()
    (memories / "MEMORY.md").write_text(
        "# Task Group: Existing Codex context\n\n"
        "## Task 1: Prior unrelated memory\n\n"
        "### keywords\n\n"
        "- ShipStable\n",
        encoding="utf-8",
    )
    (memories / "raw_memories.md").write_text(
        "# Raw Memories\n\nNo raw memories yet.\n",
        encoding="utf-8",
    )
    for summary_file in (memories / "rollout_summaries").glob("*.md"):
        summary_file.unlink()

    manifest = CodexAdapter().export_l5()
    topics = {
        entity.name: entity
        for entity in manifest.known_entities
        if entity.type == "topic"
    }

    assert manifest.recent_sessions[0].date == "2026-05-13"
    assert "Bourdon recognition first runtime layer" in topics
    assert "Bourdon" in topics
    assert "Continuo" in topics
    assert "runtime recognition" in topics
    assert "recognition timing layer" in topics


def test_export_l5_bounds_memory_session_actions(isolated_home):
    codex_home = isolated_home["create_codex_home"]()
    memories = isolated_home["create_memories"]()
    prompt_body = (
        "## Memory Writing Agent: Phase 2 (Consolidation) "
        "You are a Memory Writing Agent. "
        + "Do not publish this internal prompt as a session action. " * 120
    )
    rollout = isolated_home["add_rollout"](
        codex_home,
        (2026, 4, 20),
        "memory-thread",
        str(memories),
        "2026-04-20T12:00:00Z",
        extra_records=[
            {
                "timestamp": "2026-04-20T12:01:00Z",
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "name": "apply_patch",
                    "arguments": (
                        "*** Begin Patch\n"
                        "*** Update File: MEMORY.md\n"
                        "@@\n"
                        "-old\n"
                        "+new\n"
                        "*** End Patch\n"
                    ),
                },
            }
        ],
    )
    _write_state_thread(
        codex_home,
        session_id="memory-thread",
        title=prompt_body,
        updated_at="2026-04-20T12:00:00Z",
        cwd=str(memories),
        rollout_path=str(rollout),
    )

    manifest = CodexAdapter().export_l5()
    session = manifest.recent_sessions[0]
    topic_names = {entity.name for entity in manifest.known_entities}

    assert session.key_actions == ["Updated Codex memory artifacts: MEMORY.md"]
    assert all(len(action) <= 500 for action in session.key_actions)
    assert "Memory Writing Agent" not in session.key_actions[0]
    assert "Codex memory artifacts" in topic_names
    assert all("Memory Writing Agent" not in name for name in topic_names)


def test_export_l5_bounds_long_thread_titles(isolated_home):
    codex_home = isolated_home["create_codex_home"]()
    long_title = "vibecode-cli " + ("loads a long setup prompt " * 80)
    isolated_home["add_index_entry"](
        codex_home,
        "long-thread",
        long_title,
        "2026-04-15T00:00:00Z",
    )

    manifest = CodexAdapter().export_l5()
    session = manifest.recent_sessions[0]
    topics = [entity for entity in manifest.known_entities if entity.type == "topic"]

    assert session.key_actions[0].startswith("vibecode-cli")
    assert session.key_actions[0].endswith("...")
    assert len(session.key_actions[0]) <= 500
    assert all(len(topic.name) <= 120 for topic in topics)


def test_export_l5_dedupes_thread_names_case_insensitive(isolated_home):
    codex_home = isolated_home["create_codex_home"]()
    isolated_home["add_index_entry"](codex_home, "a", "Set up CI", "2026-04-15T00:00:00Z")
    isolated_home["add_index_entry"](codex_home, "b", "set up ci", "2026-04-14T00:00:00Z")
    manifest = CodexAdapter().export_l5()
    assert len(manifest.known_entities) == 1
    # Keeps the most-recent last_touched
    assert manifest.known_entities[0].last_touched == "2026-04-15"


def test_export_l5_schema_round_trip(isolated_home):
    import jsonschema

    codex_home = isolated_home["create_codex_home"]()
    isolated_home["add_index_entry"](
        codex_home, "s1", "A thread", "2026-04-15T00:00:00Z"
    )
    manifest = CodexAdapter().export_l5()
    schema_path = Path(__file__).parent.parent / "spec" / "L5_schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    jsonschema.validate(instance=manifest.to_dict(), schema=schema)


def test_export_l5_raises_when_nothing_discovered(isolated_home):
    with pytest.raises(AdapterDiscoveryError):
        CodexAdapter().export_l5()


def test_export_l5_extracts_projects_topics_preferences_and_team_visibility(
    isolated_home,
):
    codex_home = isolated_home["create_codex_home"]()
    isolated_home["add_index_entry"](
        codex_home,
        "cool1",
        "Plan Coolculator Mac handoff",
        "2026-04-15T12:00:00Z",
    )
    isolated_home["add_rollout"](
        codex_home,
        (2026, 4, 15),
        "cool1",
        "/workspace/coolculator",
        "2026-04-15T12:00:00Z",
        extra_records=[
            {
                "timestamp": "2026-04-15T12:01:00Z",
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "name": "apply_patch",
                    "arguments": (
                        "*** Begin Patch\n"
                        "*** Update File: apps/web/src/App.tsx\n"
                        "@@\n"
                        "-old\n"
                        "+new\n"
                        "*** End Patch\n"
                    ),
                },
            }
        ],
        meta_extra={"model_provider": "openai", "cli_version": "0.200.0"},
    )
    isolated_home["write_memory_file"](
        "MEMORY.md",
        """# Task Group: Coolculator monorepo bootstrap and Mac handoff
scope: generic

## Task 1: Build the shared contracts

### keywords

- Coolculator
- Fastify
- Jetpack Compose

## User preferences

- prefer backend-first delivery
""",
    )
    isolated_home["write_memory_file"](
        "raw_memories.md",
        """# Raw Memories

## Thread `cool-thread`
updated_at: 2026-04-15T12:00:00+00:00
cwd: /workspace/coolculator
rollout_path: /tmp/cool.jsonl

---
description: Coolculator monorepo workstream.
task: build-context-export
task_group: coolculator-monorepo
keywords: Coolculator, SwiftUI, Fastify, Mac handoff
---

Preference signals:
- default to backend-first delivery for Coolculator

Reusable knowledge:
- Coolculator is the active product name.
""",
    )
    isolated_home["write_memory_file"](
        "rollout_summaries/2026-04-15-coolculator.md",
        """thread_id: cool-thread
updated_at: 2026-04-15T12:00:00+00:00

# Rebuilt Coolculator into a new monorepo.

## Task 1: Windows-to-Mac handoff
Outcome: success

Preference signals:
- keep a clean Windows-to-Mac checkpoint
""",
    )
    isolated_home["write_codex_brain_file"](
        "LOG/2026-04-15.md",
        "# Coolculator notes\n\nTrack the handoff carefully.\n",
    )

    manifest = CodexAdapter().export_l5()

    projects = [e for e in manifest.known_entities if e.type == "project"]
    topics = [e for e in manifest.known_entities if e.type == "topic"]
    preferences = [e for e in manifest.known_entities if e.type == "preference"]

    assert any(p.name == "Coolculator" for p in projects)
    assert any("mac handoff" in t.name.lower() for t in topics)
    assert any(
        "backend-first" in ((p.summary or p.name).lower()) for p in preferences
    )
    assert manifest.recent_sessions[0].project_focus == ["Coolculator"]
    assert manifest.recent_sessions[0].files_touched == ["apps/web/src/App.tsx"]
    assert all(e.visibility == codex_module.Visibility.TEAM for e in manifest.known_entities)
    assert all(s.visibility == codex_module.Visibility.TEAM for s in manifest.recent_sessions)


def test_export_l5_canonicalizes_preference_entities(isolated_home):
    codex_home = isolated_home["create_codex_home"]()
    isolated_home["add_index_entry"](
        codex_home,
        "prefs",
        "Capture workflow preferences",
        "2026-04-15T12:00:00Z",
    )
    isolated_home["write_memory_file"](
        "MEMORY.md",
        (
            "# Task Group: Bourdon preference cleanup\n\n"
            "## User preferences\n\n"
            "- before any reinstall, the user wants a clear handoff artifact "
            "and offline recovery path, not just verbal reassurance [Task 1]\n"
            "- for cross-machine work, the user gave a clear workflow: "
            '"You can do android on this machine it is PC with android studio '
            'and vs code" and "we will commit push then move to the ios '
            'machine when we need to implement there in xcode" [Task 3]\n'
            "- user accepted the handoff after the push -> future machine "
            "switches should preserve the same branch and checkpoint\n"
        ),
    )

    manifest = CodexAdapter().export_l5()
    preferences = {
        entity.name: entity
        for entity in manifest.known_entities
        if entity.type == "preference"
    }

    assert "reinstall recovery handoff" in preferences
    assert "cross-machine handoff workflow" in preferences
    assert "machine-switch checkpoint workflow" in preferences
    for entity in preferences.values():
        assert entity.name != entity.summary
        assert len(entity.name) <= 80
        assert "Task " not in entity.name
    assert "offline recovery path" in preferences["reinstall recovery handoff"].summary
    assert "android studio" in preferences["cross-machine handoff workflow"].summary


def test_export_l5_prefers_named_project_from_memory_keywords_over_date_path(
    isolated_home,
):
    codex_home = isolated_home["create_codex_home"]()
    isolated_home["add_index_entry"](
        codex_home,
        "s1",
        "Finish Windows-side Android work, then commit/push and hand off",
        "2026-04-15T12:00:00Z",
    )
    isolated_home["add_rollout"](
        codex_home,
        (2026, 4, 15),
        "s1",
        "C:\\Users\\cumul\\Documents\\Codex\\2026-04-11-new-project",
        "2026-04-15T12:00:00Z",
    )
    isolated_home["write_memory_file"](
        "MEMORY.md",
        """# Task Group: Coolculator monorepo bootstrap and Windows-to-Mac handoff
scope: generic

### keywords

- Coolculator
- SwiftUI
- Jetpack Compose
""",
    )
    isolated_home["write_memory_file"](
        "raw_memories.md",
        """# Raw Memories

## Thread `s1`
updated_at: 2026-04-15T12:00:00+00:00

---
task_group: coolculator-monorepo
keywords: Coolculator, Fastify, Mac handoff
---
""",
    )

    manifest = CodexAdapter().export_l5()

    projects = [entity.name for entity in manifest.known_entities if entity.type == "project"]
    assert "Coolculator" in projects
    assert "2026 04 11 New Project" not in projects
    assert manifest.recent_sessions[0].project_focus == ["Coolculator"]


# -- Integration: round-trip through write_l5 + L6Store -----------------------


def test_codex_l5_can_round_trip_through_l6_store(isolated_home, tmp_path):
    """Adapter output writes cleanly to disk and the L6 store picks it up."""
    from core.l5_io import write_l5
    from core.l6_store import L6Store

    codex_home = isolated_home["create_codex_home"]()
    isolated_home["add_index_entry"](
        codex_home, "s1", "Federation test", "2026-04-15T00:00:00Z"
    )
    manifest = CodexAdapter().export_l5()

    library = tmp_path / "agent-library"
    target = library / "agents" / f"{manifest.agent.id}.l5.yaml"
    write_l5(manifest, target)

    store = L6Store(library)
    assert "codex" in store.list_agents()
    matches = store.find_entity("Federation test", access_level="team")
    assert len(matches) == 1
    assert "codex" in matches[0].agents
