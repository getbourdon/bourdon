"""Bourdon participant for GitHub Copilot in VS Code (GUI chat surface).

VS Code's Copilot Chat extension stores per-workspace data at:

    <workspaceStorage>/<hash>/GitHub.copilot-chat/
        transcripts/<session-uuid>.jsonl   — event stream per chat session
        memory-tool/memories/repo/*.md     — per-repo memory (markdown bullets)
        chat-session-resources/<uuid>/     — resource attachments

Transcript JSONL event types:
    session.start       — sessionId, version, producer, copilotVersion, vscodeVersion, startTime
    user.message        — content, attachments
    assistant.turn_start — turnId
    assistant.message   — messageId, content, toolRequests, reasoningText
    assistant.turn_end  — turnId

This participant reads those files read-only, extracts sessions and memory
entities, and emits a Bourdon L5 manifest. It aggregates across all workspace
storage hashes found.

Distinct from:
- ``participants.copilot`` (convention-file at ``~/.copilot-bourdon/memory.md``)
- ``participants.copilot_cli`` (terminal agent, ``~/.copilot/session-store.db``)

Usage::

    from participants.copilot_vscode import CopilotVscodeParticipant

    participant = CopilotVscodeParticipant()
    store = participant.discover()
    manifest = participant.export_l5()
"""

from __future__ import annotations

import json
import logging
import os
import platform
import re
import socket
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from participants.base import (
    SPEC_VERSION,
    AgentInfo,
    AgentStore,
    Entity,
    HealthStatus,
    L5Manifest,
    ParticipantDiscoveryError,
    Session,
    Visibility,
    VisibilityPolicy,
    filter_for_federation,
)
from participants.codex import _safe_native_memory_text

logger = logging.getLogger(__name__)

AGENT_ID = "copilot-vscode"
AGENT_TYPE = "code-assistant"
DISPLAY_NAME = "GitHub Copilot · VS Code"
ROLE_NARRATIVE = (
    "VS Code integrated Copilot Chat — the GUI surface for inline completion, "
    "chat, plan mode, ask mode, and explore mode. Persists per-workspace "
    "transcripts and repo-scoped memory locally. The most widely-used Copilot "
    "surface by session count."
)

DEFAULT_POLICY = VisibilityPolicy(
    default=Visibility.TEAM,
    private_tags=["personal", "financial", "credential", "health", "family", "legal"],
    team_tags=["copilot-vscode", "copilot", "vscode", "workspace"],
)

_COPILOT_CHAT_EXT = "GitHub.copilot-chat"
_TRANSCRIPTS_DIR = "transcripts"
_MEMORY_TOOL_DIR = "memory-tool"
_MAX_KEY_ACTIONS = 6
_MAX_KEY_ACTION_CHARS = 280
_MAX_SUMMARY_CHARS = 260
_MAX_TRANSCRIPT_EVENTS = 500  # cap per file to avoid loading multi-MB transcripts fully


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


def default_vscode_workspace_storage_dir() -> Optional[Path]:
    """Return the VS Code workspaceStorage path for this platform.

    Respects ``COPILOT_VSCODE_STORAGE`` environment variable override.
    """
    env = os.environ.get("COPILOT_VSCODE_STORAGE")
    if env:
        return Path(env)

    system = platform.system()
    if system == "Windows":
        appdata = os.environ.get("APPDATA")
        if appdata:
            return Path(appdata) / "Code" / "User" / "workspaceStorage"
    elif system == "Darwin":
        return Path.home() / "Library" / "Application Support" / "Code" / "User" / "workspaceStorage"
    else:
        # Linux / WSL — check Windows path under /mnt/c if WSL, else native
        wsl_path = Path("/mnt/c/Users")
        if wsl_path.is_dir():
            # WSL: try to find the Windows user
            try:
                for candidate in wsl_path.iterdir():
                    try:
                        ws = candidate / "AppData" / "Roaming" / "Code" / "User" / "workspaceStorage"
                        if ws.is_dir():
                            return ws
                    except OSError:
                        continue
            except OSError:
                pass
        # Native Linux
        config = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
        return Path(config) / "Code" / "User" / "workspaceStorage"
    return None


def _find_copilot_chat_dirs(workspace_storage: Path) -> list[Path]:
    """Find all workspace hashes that contain Copilot Chat data."""
    if not workspace_storage.is_dir():
        return []
    dirs: list[Path] = []
    try:
        for ws_hash in workspace_storage.iterdir():
            chat_dir = ws_hash / _COPILOT_CHAT_EXT
            if chat_dir.is_dir():
                dirs.append(chat_dir)
    except OSError as exc:
        logger.warning("CopilotVscodeParticipant: cannot scan workspace storage: %s", exc)
    return dirs


# ---------------------------------------------------------------------------
# Transcript parsing
# ---------------------------------------------------------------------------


def _parse_transcript(path: Path) -> Optional[dict[str, Any]]:
    """Parse a JSONL transcript file into a session summary.

    Returns None on any parse error (logged at DEBUG). Never raises.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        logger.debug("CopilotVscodeParticipant: cannot read transcript %s: %s", path, exc)
        return None

    lines = text.strip().splitlines()
    if not lines:
        return None

    session_info: dict[str, Any] = {
        "transcript_path": str(path),
        "session_id": path.stem,
        "start_time": None,
        "producer": None,
        "copilot_version": None,
        "vscode_version": None,
        "user_messages": [],
        "turn_count": 0,
    }

    turn_count = 0
    user_msgs: list[str] = []

    for line in lines[:_MAX_TRANSCRIPT_EVENTS]:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue

        event_type = event.get("type")
        data = event.get("data") or {}

        if event_type == "session.start":
            session_info["start_time"] = data.get("startTime")
            session_info["producer"] = data.get("producer")
            session_info["copilot_version"] = data.get("copilotVersion")
            session_info["vscode_version"] = data.get("vscodeVersion")
        elif event_type == "user.message":
            content = data.get("content") or ""
            if content:
                # Truncate long messages but keep enough for entity extraction
                user_msgs.append(content[:200])
        elif event_type == "assistant.turn_end":
            turn_count += 1

    session_info["user_messages"] = user_msgs[:20]
    session_info["turn_count"] = turn_count
    return session_info


# ---------------------------------------------------------------------------
# Memory-tool parsing
# ---------------------------------------------------------------------------


def _parse_memory_files(chat_dir: Path) -> list[dict[str, Any]]:
    """Parse memory-tool markdown files from a Copilot Chat workspace.

    Each file is a bulleted list of learned facts about the repo/project.
    Returns a list of {name, content, path} dicts.
    """
    memory_dir = chat_dir / _MEMORY_TOOL_DIR / "memories"
    if not memory_dir.is_dir():
        return []

    memories: list[dict[str, Any]] = []
    try:
        for md_file in memory_dir.rglob("*.md"):
            try:
                text = md_file.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if not text.strip():
                continue
            # Name from relative path (e.g. "repo/mobile-scaffold")
            rel = md_file.relative_to(memory_dir)
            name = str(rel.with_suffix("")).replace("\\", "/")
            memories.append({
                "name": name,
                "content": text,
                "path": str(md_file),
                "bullet_count": text.count("\n- ") + (1 if text.startswith("- ") else 0),
            })
    except OSError as exc:
        logger.warning("CopilotVscodeParticipant: cannot scan memory dir: %s", exc)

    return memories


# ---------------------------------------------------------------------------
# Conversion to Bourdon types
# ---------------------------------------------------------------------------


def _bounded(value: str, limit: int) -> str:
    normalized = " ".join(value.split())
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 1].rstrip() + "…"


def _session_from_transcript(info: dict[str, Any]) -> Session:
    """Convert a parsed transcript into a Bourdon Session."""
    start = info.get("start_time") or ""
    date_str = start[:10] if start else ""

    key_actions: list[str] = []
    # First user message as the session topic
    msgs = info.get("user_messages") or []
    if msgs:
        first_msg = _bounded(_safe_native_memory_text(msgs[0]), _MAX_KEY_ACTION_CHARS)
        key_actions.append(first_msg)

    if info.get("turn_count"):
        key_actions.append(f"turns: {info['turn_count']}")
    if info.get("copilot_version"):
        key_actions.append(f"copilot: {info['copilot_version']}")
    if info.get("producer"):
        key_actions.append(f"mode: {info['producer']}")

    return Session(
        date=date_str,
        cwd=None,
        project_focus=[],
        key_actions=key_actions[:_MAX_KEY_ACTIONS],
        files_touched=[],
        visibility=Visibility.TEAM,
    )


def _entities_from_memories(memories: list[dict[str, Any]]) -> list[Entity]:
    """Build entities from the memory-tool files."""
    entities: list[Entity] = []
    for mem in memories:
        name = mem["name"]
        content = mem["content"]
        # Extract first 2-3 bullets as summary
        bullets = [
            line.strip().removeprefix("- ").strip()
            for line in content.splitlines()
            if line.strip().startswith("- ")
        ][:3]
        summary = "; ".join(bullets) if bullets else None
        if summary:
            summary = _bounded(_safe_native_memory_text(summary), _MAX_SUMMARY_CHARS)

        entities.append(Entity(
            name=name,
            type="vscode-memory",
            summary=summary,
            last_touched=None,
            tags=["copilot-vscode", "memory-tool", "repo-memory"],
            visibility=Visibility.TEAM,
        ))
    return entities


def _entities_from_transcripts(transcripts: list[dict[str, Any]]) -> list[Entity]:
    """Infer project/topic entities from transcript user messages."""
    # Simple frequency-based entity extraction from first messages
    topic_counts: dict[str, int] = {}
    for info in transcripts:
        msgs = info.get("user_messages") or []
        if msgs:
            # Use the first few words as a topic hint
            first = msgs[0][:100]
            # Look for slash-command patterns (/create-skill, /fix, etc.)
            slash_match = re.match(r"^/(\S+)", first)
            if slash_match:
                topic = f"copilot-command:{slash_match.group(1)}"
                topic_counts[topic] = topic_counts.get(topic, 0) + 1

    entities: list[Entity] = []
    for topic, count in sorted(topic_counts.items(), key=lambda x: -x[1])[:10]:
        entities.append(Entity(
            name=topic,
            type="vscode-capability",
            summary=f"VS Code Copilot command used {count} time(s).",
            tags=["copilot-vscode", "capability"],
            visibility=Visibility.TEAM,
        ))
    return entities


# ---------------------------------------------------------------------------
# Participant
# ---------------------------------------------------------------------------


class CopilotVscodeParticipant:
    """External participant for GitHub Copilot in VS Code.

    Reads transcripts and memory-tool files from VS Code's workspaceStorage.
    Implements the :class:`~participants.base.BourdonParticipant` Protocol.
    """

    agent_id = AGENT_ID
    agent_type = AGENT_TYPE
    display_name = DISPLAY_NAME

    @classmethod
    def default_native_path(cls, home: Path | None = None) -> Path:
        """The workspaceStorage dir the setup wizard probes for presence.

        When ``home`` is provided (e.g. by the test harness), returns a
        deterministic path under that home rather than scanning the real
        filesystem.
        """
        if home is not None:
            return home / ".config" / "Code" / "User" / "workspaceStorage"
        try:
            ws = default_vscode_workspace_storage_dir()
        except OSError:
            ws = None
        if ws is not None:
            return ws
        return Path.home() / ".config" / "Code" / "User" / "workspaceStorage"

    def __init__(self, workspace_storage: Optional[Path] = None) -> None:
        self._workspace_storage = workspace_storage

    @property
    def native_path(self) -> str:
        return str(self._workspace_storage or default_vscode_workspace_storage_dir() or "")

    # -- Protocol surface -----------------------------------------------------

    def discover(self) -> AgentStore:
        """Locate VS Code Copilot Chat workspace storage."""
        ws = self._workspace_storage or default_vscode_workspace_storage_dir()
        if ws is None or not ws.is_dir():
            raise ParticipantDiscoveryError(
                f"VS Code workspaceStorage not found at {ws}. "
                "Install VS Code with the GitHub Copilot Chat extension."
            )
        chat_dirs = _find_copilot_chat_dirs(ws)
        if not chat_dirs:
            raise ParticipantDiscoveryError(
                f"No GitHub.copilot-chat data found under {ws}. "
                "Open VS Code and use Copilot Chat at least once."
            )
        return AgentStore(
            path=str(ws),
            version="transcript-v1",
            metadata={
                "workspace_count": len(chat_dirs),
                "workspace_storage_path": str(ws),
            },
        )

    def export_sessions(self, since: datetime, limit: int = 100) -> list[Session]:
        """Return recent VS Code Copilot sessions newer than ``since``."""
        since_iso = since.astimezone(timezone.utc).date().isoformat()
        transcripts = self._all_transcripts()
        sessions: list[Session] = []
        for info in transcripts:
            session = _session_from_transcript(info)
            if session.date and session.date < since_iso:
                continue
            sessions.append(session)
            if len(sessions) >= limit:
                break
        return sessions

    def export_l5(self, since: Optional[datetime] = None) -> L5Manifest:
        """Build the L5 manifest from VS Code Copilot Chat storage."""
        transcripts = self._all_transcripts()
        memories = self._all_memories()

        since_iso = since.astimezone(timezone.utc).date().isoformat() if since else None
        sessions: list[Session] = []
        for info in transcripts:
            session = _session_from_transcript(info)
            if since_iso and session.date and session.date < since_iso:
                continue
            sessions.append(session)

        entities = _entities_from_memories(memories) + _entities_from_transcripts(transcripts)
        visible_entities = filter_for_federation(entities, DEFAULT_POLICY)

        return L5Manifest(
            spec_version=SPEC_VERSION,
            agent=AgentInfo(
                id=AGENT_ID,
                type=AGENT_TYPE,
                instance=socket.gethostname() or "unknown",
                spec_version_compat=f">={SPEC_VERSION}",
                role_narrative=ROLE_NARRATIVE,
            ),
            last_updated=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            capabilities=[
                "inline-completion",
                "chat",
                "plan-mode",
                "ask-mode",
                "explore-mode",
                "memory-tool",
                "workspace-scoped",
            ],
            recent_sessions=sessions,
            known_entities=visible_entities,
            visibility_policy=DEFAULT_POLICY,
        )

    def health_check(self) -> HealthStatus:
        """Report ok / degraded / blocked. Never raises."""
        ws = self._workspace_storage or default_vscode_workspace_storage_dir()
        if ws is None or not ws.is_dir():
            return HealthStatus(
                status="blocked",
                reason=f"VS Code workspaceStorage not found at {ws}.",
                details={"expected_path": str(ws)},
                proposed_fix=(
                    "Install VS Code and the GitHub Copilot Chat extension. "
                    "Set COPILOT_VSCODE_STORAGE if VS Code stores data elsewhere."
                ),
            )
        chat_dirs = _find_copilot_chat_dirs(ws)
        if not chat_dirs:
            return HealthStatus(
                status="degraded",
                reason="No Copilot Chat data found in any workspace.",
                details={"workspace_storage": str(ws)},
                proposed_fix=(
                    "Open VS Code and start a Copilot Chat conversation, "
                    "then re-run `bourdon copilot-vscode export`."
                ),
            )
        transcripts = self._all_transcripts()
        memories = self._all_memories()
        return HealthStatus(
            status="ok",
            reason=None,
            details={
                "workspace_storage": str(ws),
                "workspace_count": len(chat_dirs),
                "transcript_count": len(transcripts),
                "memory_file_count": len(memories),
                "total_turns": sum(t.get("turn_count", 0) for t in transcripts),
            },
        )

    # -- Internal -------------------------------------------------------------

    def _all_transcripts(self) -> list[dict[str, Any]]:
        """Collect and parse all transcripts across all workspaces."""
        ws = self._workspace_storage or default_vscode_workspace_storage_dir()
        if ws is None:
            return []
        chat_dirs = _find_copilot_chat_dirs(ws)
        transcripts: list[dict[str, Any]] = []
        for chat_dir in chat_dirs:
            transcript_dir = chat_dir / _TRANSCRIPTS_DIR
            if not transcript_dir.is_dir():
                continue
            try:
                for jsonl_file in transcript_dir.glob("*.jsonl"):
                    info = _parse_transcript(jsonl_file)
                    if info is not None:
                        transcripts.append(info)
            except OSError:
                continue
        # Sort by start_time descending
        transcripts.sort(key=lambda t: t.get("start_time") or "", reverse=True)
        return transcripts

    def _all_memories(self) -> list[dict[str, Any]]:
        """Collect all memory-tool files across all workspaces."""
        ws = self._workspace_storage or default_vscode_workspace_storage_dir()
        if ws is None:
            return []
        chat_dirs = _find_copilot_chat_dirs(ws)
        memories: list[dict[str, Any]] = []
        for chat_dir in chat_dirs:
            memories.extend(_parse_memory_files(chat_dir))
        return memories
