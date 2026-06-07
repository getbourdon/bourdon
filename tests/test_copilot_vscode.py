"""Tests for participants.copilot_vscode — VS Code Copilot Chat participant."""

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

from participants.copilot_vscode import (
    CopilotVscodeParticipant,
    _parse_transcript,
    _parse_memory_files,
    _bounded,
    _find_copilot_chat_dirs,
    _session_from_transcript,
    _entities_from_memories,
)
from participants.base import ParticipantDiscoveryError


@pytest.fixture
def fake_workspace_storage(tmp_path):
    """Create a fake VS Code workspaceStorage with Copilot Chat data."""
    # Workspace 1
    ws1 = tmp_path / "abc123hash" / "GitHub.copilot-chat"
    transcripts_dir = ws1 / "transcripts"
    transcripts_dir.mkdir(parents=True)
    memory_dir = ws1 / "memory-tool" / "memories" / "repo"
    memory_dir.mkdir(parents=True)

    # Write a transcript JSONL
    transcript_events = [
        {
            "type": "session.start",
            "data": {
                "sessionId": "sess-uuid-001",
                "version": 1,
                "producer": "copilot-agent",
                "copilotVersion": "0.40.1",
                "vscodeVersion": "1.112.0",
                "startTime": "2026-06-01T10:00:00.000Z",
            },
            "id": "ev-001",
            "timestamp": "2026-06-01T10:00:00.000Z",
            "parentId": None,
        },
        {
            "type": "user.message",
            "data": {"content": "/fix the auth middleware bug", "attachments": []},
            "id": "ev-002",
            "timestamp": "2026-06-01T10:00:05.000Z",
            "parentId": "ev-001",
        },
        {
            "type": "assistant.turn_start",
            "data": {"turnId": "turn-001"},
            "id": "ev-003",
            "timestamp": "2026-06-01T10:00:06.000Z",
            "parentId": "ev-002",
        },
        {
            "type": "assistant.message",
            "data": {
                "messageId": "msg-001",
                "content": "I found the issue in the middleware...",
                "toolRequests": [],
                "reasoningText": None,
            },
            "id": "ev-004",
            "timestamp": "2026-06-01T10:00:10.000Z",
            "parentId": "ev-003",
        },
        {
            "type": "assistant.turn_end",
            "data": {"turnId": "turn-001"},
            "id": "ev-005",
            "timestamp": "2026-06-01T10:00:12.000Z",
            "parentId": "ev-004",
        },
        {
            "type": "user.message",
            "data": {"content": "Now add tests for it", "attachments": []},
            "id": "ev-006",
            "timestamp": "2026-06-01T10:01:00.000Z",
            "parentId": "ev-005",
        },
        {
            "type": "assistant.turn_start",
            "data": {"turnId": "turn-002"},
            "id": "ev-007",
            "timestamp": "2026-06-01T10:01:01.000Z",
            "parentId": "ev-006",
        },
        {
            "type": "assistant.turn_end",
            "data": {"turnId": "turn-002"},
            "id": "ev-008",
            "timestamp": "2026-06-01T10:01:30.000Z",
            "parentId": "ev-007",
        },
    ]
    transcript_path = transcripts_dir / "sess-uuid-001.jsonl"
    transcript_path.write_text(
        "\n".join(json.dumps(e) for e in transcript_events),
        encoding="utf-8",
    )

    # Write memory-tool file
    memory_file = memory_dir / "my-project.md"
    memory_file.write_text(
        "- Project uses TypeScript with strict mode.\n"
        "- Auth is JWT-based with RS256 signing.\n"
        "- Tests use vitest with coverage thresholds.\n",
        encoding="utf-8",
    )

    return tmp_path


@pytest.fixture
def fake_workspace_storage_empty(tmp_path):
    """Workspace storage exists but no Copilot Chat data."""
    (tmp_path / "somehash").mkdir()
    return tmp_path


class TestCopilotVscodeParticipant:
    def test_discover_success(self, fake_workspace_storage):
        participant = CopilotVscodeParticipant(workspace_storage=fake_workspace_storage)
        store = participant.discover()
        assert store.version == "transcript-v1"
        assert store.metadata["workspace_count"] == 1

    def test_discover_missing_storage(self, tmp_path):
        participant = CopilotVscodeParticipant(workspace_storage=tmp_path / "nonexistent")
        with pytest.raises(ParticipantDiscoveryError):
            participant.discover()

    def test_discover_no_copilot_data(self, fake_workspace_storage_empty):
        participant = CopilotVscodeParticipant(workspace_storage=fake_workspace_storage_empty)
        with pytest.raises(ParticipantDiscoveryError):
            participant.discover()

    def test_export_sessions(self, fake_workspace_storage):
        participant = CopilotVscodeParticipant(workspace_storage=fake_workspace_storage)
        since = datetime(2026, 5, 1, tzinfo=timezone.utc)
        sessions = participant.export_sessions(since=since)
        assert len(sessions) == 1
        assert sessions[0].date == "2026-06-01"
        assert any("fix" in a.lower() for a in sessions[0].key_actions)

    def test_export_sessions_since_filter(self, fake_workspace_storage):
        participant = CopilotVscodeParticipant(workspace_storage=fake_workspace_storage)
        since = datetime(2026, 6, 15, tzinfo=timezone.utc)
        sessions = participant.export_sessions(since=since)
        assert len(sessions) == 0

    def test_export_l5_manifest(self, fake_workspace_storage):
        participant = CopilotVscodeParticipant(workspace_storage=fake_workspace_storage)
        manifest = participant.export_l5()
        assert manifest.agent.id == "copilot-vscode"
        assert manifest.agent.type == "code-assistant"
        assert "chat" in manifest.capabilities
        assert "memory-tool" in manifest.capabilities
        assert len(manifest.recent_sessions) == 1
        assert len(manifest.known_entities) >= 1

    def test_export_l5_memory_entities(self, fake_workspace_storage):
        participant = CopilotVscodeParticipant(workspace_storage=fake_workspace_storage)
        manifest = participant.export_l5()
        entity_names = [e.name for e in manifest.known_entities]
        assert "repo/my-project" in entity_names

    def test_export_l5_slash_command_entities(self, fake_workspace_storage):
        participant = CopilotVscodeParticipant(workspace_storage=fake_workspace_storage)
        manifest = participant.export_l5()
        entity_names = [e.name for e in manifest.known_entities]
        assert "copilot-command:fix" in entity_names

    def test_health_check_ok(self, fake_workspace_storage):
        participant = CopilotVscodeParticipant(workspace_storage=fake_workspace_storage)
        health = participant.health_check()
        assert health.status == "ok"
        assert health.details["transcript_count"] == 1
        assert health.details["memory_file_count"] == 1
        assert health.details["total_turns"] == 2

    def test_health_check_blocked(self, tmp_path):
        participant = CopilotVscodeParticipant(workspace_storage=tmp_path / "nope")
        health = participant.health_check()
        assert health.status == "blocked"
        assert health.proposed_fix is not None

    def test_health_check_degraded(self, fake_workspace_storage_empty):
        participant = CopilotVscodeParticipant(workspace_storage=fake_workspace_storage_empty)
        health = participant.health_check()
        assert health.status == "degraded"


class TestParseTranscript:
    def test_valid_transcript(self, fake_workspace_storage):
        path = fake_workspace_storage / "abc123hash" / "GitHub.copilot-chat" / "transcripts" / "sess-uuid-001.jsonl"
        info = _parse_transcript(path)
        assert info is not None
        assert info["session_id"] == "sess-uuid-001"
        assert info["start_time"] == "2026-06-01T10:00:00.000Z"
        assert info["turn_count"] == 2
        assert len(info["user_messages"]) == 2
        assert info["copilot_version"] == "0.40.1"

    def test_missing_file(self, tmp_path):
        assert _parse_transcript(tmp_path / "nope.jsonl") is None

    def test_empty_file(self, tmp_path):
        f = tmp_path / "empty.jsonl"
        f.write_text("", encoding="utf-8")
        assert _parse_transcript(f) is None


class TestParseMemoryFiles:
    def test_parses_memory(self, fake_workspace_storage):
        chat_dir = fake_workspace_storage / "abc123hash" / "GitHub.copilot-chat"
        memories = _parse_memory_files(chat_dir)
        assert len(memories) == 1
        assert memories[0]["name"] == "repo/my-project"
        assert memories[0]["bullet_count"] == 3

    def test_no_memory_dir(self, tmp_path):
        chat_dir = tmp_path / "GitHub.copilot-chat"
        chat_dir.mkdir(parents=True)
        assert _parse_memory_files(chat_dir) == []


class TestEntitiesFromMemories:
    def test_creates_entities(self):
        memories = [
            {"name": "repo/project-a", "content": "- Uses Python.\n- Has REST API.\n", "bullet_count": 2, "path": "/x"},
        ]
        entities = _entities_from_memories(memories)
        assert len(entities) == 1
        assert entities[0].name == "repo/project-a"
        assert entities[0].type == "vscode-memory"
        assert "Python" in (entities[0].summary or "")
