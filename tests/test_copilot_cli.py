"""Tests for participants.copilot_cli — Copilot CLI SQLite participant."""

import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

from participants.copilot_cli import (
    CopilotCliParticipant,
    default_copilot_cli_dir,
    default_copilot_cli_db_path,
    _bounded,
    _extract_sessions,
    _extract_dynamic_context,
    _safe_copy_db,
    _cleanup_tmp,
)
from participants.base import ParticipantDiscoveryError


@pytest.fixture
def fake_copilot_dir(tmp_path):
    """Create a fake ~/.copilot with a populated session-store.db."""
    db_path = tmp_path / "session-store.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript("""
        CREATE TABLE schema_version (version INTEGER NOT NULL);
        INSERT INTO schema_version VALUES (4);

        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            cwd TEXT,
            repository TEXT,
            host_type TEXT,
            branch TEXT,
            summary TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE turns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL REFERENCES sessions(id),
            turn_index INTEGER NOT NULL,
            user_message TEXT,
            assistant_response TEXT,
            timestamp TEXT DEFAULT (datetime('now')),
            UNIQUE(session_id, turn_index)
        );
        CREATE TABLE checkpoints (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL REFERENCES sessions(id),
            checkpoint_number INTEGER NOT NULL,
            title TEXT,
            overview TEXT,
            history TEXT,
            work_done TEXT,
            technical_details TEXT,
            important_files TEXT,
            next_steps TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            UNIQUE(session_id, checkpoint_number)
        );
        CREATE TABLE session_files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL REFERENCES sessions(id),
            file_path TEXT NOT NULL,
            tool_name TEXT,
            turn_index INTEGER,
            first_seen_at TEXT DEFAULT (datetime('now')),
            UNIQUE(session_id, file_path)
        );
        CREATE TABLE session_refs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL REFERENCES sessions(id),
            ref_type TEXT NOT NULL,
            ref_value TEXT NOT NULL,
            turn_index INTEGER,
            created_at TEXT DEFAULT (datetime('now')),
            UNIQUE(session_id, ref_type, ref_value)
        );
        CREATE TABLE dynamic_context_items (
            repository TEXT NOT NULL,
            branch TEXT NOT NULL,
            src TEXT NOT NULL,
            name TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            content TEXT NOT NULL DEFAULT '',
            read_count INTEGER NOT NULL DEFAULT 0,
            count INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (repository, branch, src, name)
        );

        INSERT INTO sessions (id, cwd, repository, branch, summary, created_at, updated_at)
        VALUES
            ('sess-001', '/home/user/project', 'user/my-repo', 'main',
             'Implementing auth module', '2026-06-01T10:00:00Z', '2026-06-01T11:00:00Z'),
            ('sess-002', '/home/user/other', 'user/other-repo', 'feat/x',
             'Debugging CI pipeline', '2026-06-03T14:00:00Z', '2026-06-03T15:30:00Z');

        INSERT INTO turns (session_id, turn_index, user_message, assistant_response, timestamp)
        VALUES
            ('sess-001', 0, 'Help me add JWT auth', 'Sure, let me implement that.', '2026-06-01T10:01:00Z'),
            ('sess-001', 1, 'Now add refresh tokens', 'Added refresh token logic.', '2026-06-01T10:05:00Z'),
            ('sess-002', 0, 'Why is CI failing?', 'The test runner config is wrong.', '2026-06-03T14:01:00Z');

        INSERT INTO checkpoints (session_id, checkpoint_number, title, overview)
        VALUES
            ('sess-001', 1, 'Auth scaffold complete', 'JWT middleware + route guards done.');

        INSERT INTO session_files (session_id, file_path, tool_name, turn_index)
        VALUES
            ('sess-001', 'src/auth/jwt.ts', 'edit', 0),
            ('sess-001', 'src/auth/refresh.ts', 'create', 1);

        INSERT INTO session_refs (session_id, ref_type, ref_value)
        VALUES
            ('sess-001', 'commit', 'abc123'),
            ('sess-002', 'pr', '#42');

        INSERT INTO dynamic_context_items (repository, branch, src, name, description, content, read_count, count)
        VALUES
            ('user/my-repo', 'main', 'copilot', 'auth-patterns',
             'JWT auth patterns used in this project', 'Use RS256 with refresh tokens.', 5, 12);
    """)
    conn.close()
    return tmp_path


class TestCopilotCliParticipant:
    def test_discover_success(self, fake_copilot_dir):
        participant = CopilotCliParticipant(copilot_dir=fake_copilot_dir)
        store = participant.discover()
        assert store.path == str(fake_copilot_dir)
        assert store.version == "schema-v4"
        assert "db_path" in store.metadata

    def test_discover_missing_dir(self, tmp_path):
        participant = CopilotCliParticipant(copilot_dir=tmp_path / "nonexistent")
        with pytest.raises(ParticipantDiscoveryError):
            participant.discover()

    def test_export_sessions(self, fake_copilot_dir):
        participant = CopilotCliParticipant(copilot_dir=fake_copilot_dir)
        since = datetime(2026, 5, 1, tzinfo=timezone.utc)
        sessions = participant.export_sessions(since=since)
        assert len(sessions) == 2
        # Most recent first
        assert sessions[0].date == "2026-06-03"
        assert any("Debugging CI pipeline" in a for a in sessions[0].key_actions)

    def test_export_sessions_since_filter(self, fake_copilot_dir):
        participant = CopilotCliParticipant(copilot_dir=fake_copilot_dir)
        since = datetime(2026, 6, 2, tzinfo=timezone.utc)
        sessions = participant.export_sessions(since=since)
        assert len(sessions) == 1
        assert sessions[0].date == "2026-06-03"

    def test_export_l5_manifest(self, fake_copilot_dir):
        participant = CopilotCliParticipant(copilot_dir=fake_copilot_dir)
        manifest = participant.export_l5()
        assert manifest.agent.id == "copilot-cli"
        assert manifest.agent.type == "code-assistant"
        assert manifest.agent.role_narrative is not None
        assert len(manifest.recent_sessions) == 2
        assert len(manifest.known_entities) >= 1  # at least the repos
        assert "terminal-agent" in manifest.capabilities

    def test_export_l5_includes_dynamic_context_entities(self, fake_copilot_dir):
        participant = CopilotCliParticipant(copilot_dir=fake_copilot_dir)
        manifest = participant.export_l5()
        entity_names = [e.name for e in manifest.known_entities]
        assert "auth-patterns" in entity_names

    def test_export_l5_includes_repos_as_entities(self, fake_copilot_dir):
        participant = CopilotCliParticipant(copilot_dir=fake_copilot_dir)
        manifest = participant.export_l5()
        entity_names = [e.name for e in manifest.known_entities]
        assert "user/my-repo" in entity_names
        assert "user/other-repo" in entity_names

    def test_session_includes_files(self, fake_copilot_dir):
        participant = CopilotCliParticipant(copilot_dir=fake_copilot_dir)
        sessions = participant.export_sessions(since=datetime(2026, 5, 1, tzinfo=timezone.utc))
        # sess-001 should have files
        auth_session = [s for s in sessions if "auth" in " ".join(s.key_actions).lower()]
        assert auth_session
        assert "src/auth/jwt.ts" in auth_session[0].files_touched

    def test_session_includes_refs(self, fake_copilot_dir):
        participant = CopilotCliParticipant(copilot_dir=fake_copilot_dir)
        sessions = participant.export_sessions(since=datetime(2026, 5, 1, tzinfo=timezone.utc))
        # sess-001 has commit ref
        auth_session = [s for s in sessions if "auth" in " ".join(s.key_actions).lower()]
        assert any("commit" in a for a in auth_session[0].key_actions)

    def test_health_check_ok(self, fake_copilot_dir):
        participant = CopilotCliParticipant(copilot_dir=fake_copilot_dir)
        health = participant.health_check()
        assert health.status == "ok"
        assert health.details["session_count"] == 2

    def test_health_check_blocked(self, tmp_path):
        participant = CopilotCliParticipant(copilot_dir=tmp_path / "nonexistent")
        health = participant.health_check()
        assert health.status == "blocked"
        assert health.proposed_fix is not None

    def test_health_check_blocked_no_db(self, tmp_path):
        (tmp_path / ".copilot").mkdir()
        participant = CopilotCliParticipant(copilot_dir=tmp_path / ".copilot")
        health = participant.health_check()
        assert health.status == "blocked"


class TestBounded:
    def test_short_string_unchanged(self):
        assert _bounded("hello", 10) == "hello"

    def test_long_string_truncated(self):
        result = _bounded("a" * 100, 50)
        assert len(result) <= 50
        assert result.endswith("…")

    def test_whitespace_normalized(self):
        assert _bounded("hello   world", 20) == "hello world"


class TestSafeCopyDb:
    def test_returns_none_for_missing(self, tmp_path):
        assert _safe_copy_db(tmp_path / "nope.db") is None

    def test_copies_existing_db(self, fake_copilot_dir):
        db_path = fake_copilot_dir / "session-store.db"
        tmp = _safe_copy_db(db_path)
        assert tmp is not None
        assert tmp.is_file()
        _cleanup_tmp(tmp)
