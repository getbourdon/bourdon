"""Top-level `bourdon` CLI."""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import subprocess
import sys
import tempfile
import time as _time
from collections import Counter
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Any

import yaml

from participants import discover_participants
from participants.base import ParticipantDiscoveryError
from participants.cascade import (
    CascadeParticipant,
    _inspect_cascade_memory,
    default_cascade_memory_path,
)
from participants.cascade import (
    init_memory_file as cascade_init_memory_file,
)
from participants.claude_code import ClaudeCodeParticipant
from participants.claude_code_automations import (
    ClaudeCodeAutomationsParticipant,
    default_claude_code_automations_dir,
    merge_automation_tree,
)
from participants.claude_desktop_code import ClaudeDesktopCodeParticipant
from participants.claude_desktop_cowork import ClaudeDesktopCoworkParticipant
from participants.codex import (
    CodexParticipant,
    _build_codex_native_memory_payload,
    _default_codex_memory_md_path,
    _default_codex_native_memory_path,
    _inspect_codex_fallback_recall,
    _inspect_codex_state_db,
    _merge_bourdon_memory_md_section,
    _safe_native_memory_text,
)
from participants.codex_automations import CodexAutomationsParticipant
from participants.copilot import (
    CopilotParticipant,
    _inspect_copilot_memory,
    default_copilot_memory_path,
    init_memory_file,
)
from participants.cursor import CursorParticipant
from participants.cursor_automations import (
    CursorAutomationsParticipant,
    default_cursor_automations_dir,
    init_automations_dir as cursor_init_automations_dir,
    merge_automation_tree as cursor_merge_automation_tree,
)
from core.agents_export import (
    export_local_agents,
    resolve_local_name,
)
from core.codex_context import (
    filter_manifest_for_access,
    write_codex_context_artifacts,
)
from core.codex_fixtures import create_sample_codex_sources
from core.codex_turn_compiler import compile_codex_turn
from core.l2 import query_l2
from core.l5_io import write_l5_dict
from core.l6_server import prepare_recognition_context_from_store
from core.l6_store import DEFAULT_LIBRARY_PATH, L6Store
from core.recognition_runtime import recognition_first


def _default_claude_code_l5_path() -> Path:
    """Resolve ~/agent-library/agents/claude-code.l5.yaml at call time.

    Computed at call time (not import time) so tests can monkeypatch
    ``Path.home`` and have the resolution honor the override.
    """
    return Path.home() / "agent-library" / "agents" / "claude-code.l5.yaml"


def _default_codex_l5_path() -> Path:
    return Path.home() / "agent-library" / "agents" / "codex.l5.yaml"


def _default_claude_code_automations_l5_path() -> Path:
    return Path.home() / "agent-library" / "agents" / "claude-code-automations.l5.yaml"


def _default_codex_automations_l5_path() -> Path:
    return Path.home() / "agent-library" / "agents" / "codex-automations.l5.yaml"


def _default_claude_desktop_cowork_l5_path() -> Path:
    return Path.home() / "agent-library" / "agents" / "claude-desktop-cowork.l5.yaml"


def _default_claude_desktop_code_l5_path() -> Path:
    return Path.home() / "agent-library" / "agents" / "claude-desktop-code.l5.yaml"


def _default_cursor_l5_path() -> Path:
    return Path.home() / "agent-library" / "agents" / "cursor.l5.yaml"


def _default_cursor_automations_l5_path() -> Path:
    return Path.home() / "agent-library" / "agents" / "cursor-automations.l5.yaml"


def _default_copilot_l5_path() -> Path:
    return Path.home() / "agent-library" / "agents" / "copilot.l5.yaml"


def _default_cascade_l5_path() -> Path:
    return Path.home() / "agent-library" / "agents" / "cascade.l5.yaml"


def _default_agents_dir() -> Path:
    """Resolve ~/agent-library/agents at call time (test-monkeypatch friendly)."""
    return Path.home() / "agent-library" / "agents"


_DEFAULT_PEERS_CONFIG = Path.home() / ".bourdon" / "peers.yaml"


def _parse_since(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        parsed = date.fromisoformat(value)
        return datetime.combine(parsed, time.min)


def _write_yaml_if_requested(data: dict[str, Any], path: str | None) -> None:
    if not path:
        return
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def _print_yaml(data: dict[str, Any]) -> None:
    print(yaml.safe_dump(data, sort_keys=False), end="")


def _write_text_atomic(text: str, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = target.with_name(f".{target.name}.tmp")
    tmp_path.write_text(text, encoding="utf-8")
    tmp_path.replace(target)


def _build_participant(args: argparse.Namespace) -> CodexParticipant:
    codex_home = Path(args.codex_home) if getattr(args, "codex_home", None) else None
    codex_brain = (
        Path(args.codex_brain) if getattr(args, "codex_brain", None) else None
    )
    return CodexParticipant(codex_home=codex_home, codex_brain=codex_brain)


def _manifest_for_access(
    participant: CodexParticipant, since: datetime | None, access_level: str
) -> dict[str, Any]:
    manifest = participant.export_l5(since=since)
    return filter_manifest_for_access(manifest, access_level=access_level)


def _handle_codex_export(args: argparse.Namespace) -> int:
    participant = _build_participant(args)
    data = _manifest_for_access(
        participant,
        since=_parse_since(args.since),
        access_level=args.access_level,
    )
    _write_yaml_if_requested(data, args.out)
    _print_yaml(data)
    return 0


def _handle_prepare_turn(args: argparse.Namespace) -> int:
    store = L6Store(Path(args.library))
    report = prepare_recognition_context_from_store(
        store,
        args.prompt,
        access_level=args.access_level,
    )
    _write_yaml_if_requested(report, args.report_out)
    _print_yaml(report)
    return 0


async def _build_deeper_context_report(
    prompt: str,
    access_level: str,
) -> dict[str, Any]:
    try:
        context = await query_l2(prompt)
    except Exception:
        context = ""
    return {
        "prompt": prompt,
        "access_level": access_level,
        "context": context,
        "context_chars": len(context),
    }


def _handle_deeper_context(args: argparse.Namespace) -> int:
    report = asyncio.run(
        _build_deeper_context_report(
            args.prompt,
            args.access_level,
        )
    )
    _write_yaml_if_requested(report, args.report_out)
    _print_yaml(report)
    return 0


def _handle_cursor_export(args: argparse.Namespace) -> int:
    """Hook-safe: silent on success, returns 0 in all failure modes."""
    verbose = getattr(args, "verbose", False)
    try:
        cursor_dir = Path(args.cursor_dir) if args.cursor_dir else None
        participant = CursorParticipant(cursor_dir=cursor_dir)
    except Exception as exc:  # noqa: BLE001 -- hook contract
        if verbose:
            print(f"bourdon cursor export: init failed: {exc}", file=sys.stderr)
        return 0
    try:
        manifest = participant.export_l5(since=_parse_since(args.since))
    except ParticipantDiscoveryError as exc:
        if verbose:
            print(f"bourdon cursor export: no data ({exc}), skipping", file=sys.stderr)
        return 0
    except Exception as exc:  # noqa: BLE001 -- hook contract
        if verbose:
            print(f"bourdon cursor export: export failed: {exc}", file=sys.stderr)
        return 0
    data = filter_manifest_for_access(manifest, access_level=args.access_level)
    out_path = Path(args.out) if args.out else _default_cursor_l5_path()
    try:
        write_l5_dict(data, out_path)
    except Exception as exc:  # noqa: BLE001 -- hook contract
        if verbose:
            print(f"bourdon cursor export: write failed: {exc}", file=sys.stderr)
        return 0
    if getattr(args, "print_manifest", False):
        _print_yaml(data)
    return 0


def _handle_cursor_doctor(args: argparse.Namespace) -> int:
    cursor_dir = Path(args.cursor_dir) if getattr(args, "cursor_dir", None) else None
    participant = CursorParticipant(cursor_dir=cursor_dir)
    health = participant.health_check()
    report: dict[str, Any] = {
        "health": {
            "status": health.status, "reason": health.reason,
            "details": health.details,
        },
        "cursor_dir": participant.native_path,
    }
    if health.proposed_fix:
        report["health"]["proposed_fix"] = health.proposed_fix
    _write_yaml_if_requested(report, getattr(args, "report_out", None))
    _print_yaml(report)
    return 0


def _handle_cursor_compile_turn(args: argparse.Namespace) -> int:
    from core.cursor_turn_compiler import compile_cursor_turn
    brief = compile_cursor_turn(
        args.prompt, cwd=getattr(args, "cwd", None),
        access_level=getattr(args, "access_level", "team"),
        library_path=(
            Path(args.library_path)
            if getattr(args, "library_path", None) else None
        ),
        max_items=getattr(args, "max_items", 6),
    )
    report: dict[str, Any] = {
        "schema_version": brief.schema_version, "strategy": brief.strategy,
        "cwd_project": brief.cwd_project, "prompt_tokens": brief.prompt_tokens,
        "matched_entities": brief.matched_entities, "routing": brief.routing,
        "compile_latency_us": brief.compile_latency_us,
        "text": brief.to_text(),
    }
    _print_yaml(report)
    return 0


def _handle_cursor_sync_native(args: argparse.Namespace) -> int:
    from core.l6_store import DEFAULT_LIBRARY_PATH, L6Store
    library_path = (
        Path(args.library_path) if getattr(args, "library_path", None)
        else DEFAULT_LIBRARY_PATH
    )
    access_level = getattr(args, "access_level", "team")
    max_entities = getattr(args, "max_entities", 100)
    max_sessions = getattr(args, "max_sessions", 20)
    store = L6Store(library_path)
    agents = store.list_agents()
    all_entities: list[tuple[str, dict]] = []
    all_sessions: list[tuple[str, dict]] = []
    for agent_id in agents:
        manifest = store.get_agent_manifest(agent_id, access_level=access_level)
        if not manifest:
            continue
        for entity in manifest.get("known_entities") or []:
            all_entities.append((agent_id, entity))
        for session in manifest.get("recent_sessions") or []:
            all_sessions.append((agent_id, session))
    all_sessions.sort(key=lambda p: p[1].get("date", ""), reverse=True)
    lines = [
        "# Bourdon Federation Context", "",
        f"_Auto-generated by `bourdon cursor sync-native`. "
        f"{len(agents)} agents federated._", "",
    ]
    if all_entities:
        lines.append("## Known Entities")
        lines.append("")
        for agent_id, entity in all_entities[:max_entities]:
            name = entity.get("name", "?")
            etype = entity.get("type", "topic")
            summary = entity.get("summary", "")
            line = f"- **{name}** ({etype}, via {agent_id})"
            if summary:
                line += f": {summary[:200]}"
            lines.append(line)
        lines.append("")
    if all_sessions:
        lines.append("## Recent Sessions")
        lines.append("")
        for agent_id, session in all_sessions[:max_sessions]:
            sdate = session.get("date", "?")
            cwd = session.get("cwd", "")
            actions = session.get("key_actions", [])
            action_text = (
                "; ".join(str(a)[:120] for a in actions[:3]) if actions else ""
            )
            line = f"- **{sdate}** ({agent_id})"
            if cwd:
                line += f" in `{cwd}`"
            if action_text:
                line += f": {action_text}"
            lines.append(line)
        lines.append("")
    text = "\n".join(lines) + "\n"
    cursor_home = (
        Path(args.cursor_dir) if getattr(args, "cursor_dir", None) else None
    )
    target = (
        Path(args.out) if getattr(args, "out", None)
        else (cursor_home or Path.home() / ".cursor") / "memory" / "bourdon_context.md"
    )
    if args.write:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    report = {
        "mode": "write" if args.write else "dry-run",
        "target": str(target), "agents_federated": len(agents),
        "entities": len(all_entities), "sessions": len(all_sessions),
        "bytes": len(text.encode("utf-8")), "written": bool(args.write),
    }
    if not args.write:
        report["preview"] = text
    _print_yaml(report)
    return 0


def _handle_cursor_init(args: argparse.Namespace) -> int:
    automations_dir = (
        Path(args.automations_dir)
        if getattr(args, "automations_dir", None) else None
    )
    automation_id = getattr(args, "automation_id", None) or "cursor-cloud-agent"
    force = getattr(args, "force", False)
    try:
        path = cursor_init_automations_dir(
            automations_dir=automations_dir,
            automation_id=automation_id, force=force,
        )
        print(f"Created {path}")
        print(
            f"Edit {path / 'memory.md'} to add run entries, then run "
            "`bourdon cursor-automations export`."
        )
    except FileExistsError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


def _handle_cursor_automations_export(args: argparse.Namespace) -> int:
    """Hook-safe: silent on success, returns 0 in all failure modes."""
    verbose = getattr(args, "verbose", False)
    try:
        automations_dir = (
            Path(args.automations_dir)
            if getattr(args, "automations_dir", None) else None
        )
        participant = CursorAutomationsParticipant(automations_dir=automations_dir)
    except Exception as exc:  # noqa: BLE001 -- hook contract
        if verbose:
            print(f"bourdon cursor-automations export: init failed: {exc}", file=sys.stderr)
        return 0
    try:
        manifest = participant.export_l5(since=_parse_since(args.since))
    except ParticipantDiscoveryError as exc:
        if verbose:
            print(
                f"bourdon cursor-automations export: no data ({exc}), skipping",
                file=sys.stderr,
            )
        return 0
    except Exception as exc:  # noqa: BLE001 -- hook contract
        if verbose:
            print(f"bourdon cursor-automations export: failed: {exc}", file=sys.stderr)
        return 0
    data = filter_manifest_for_access(manifest, access_level=args.access_level)
    out_path = (
        Path(args.out) if args.out
        else _default_cursor_automations_l5_path()
    )
    try:
        write_l5_dict(data, out_path)
    except Exception as exc:  # noqa: BLE001 -- hook contract
        if verbose:
            print(f"bourdon cursor-automations export: write failed: {exc}", file=sys.stderr)
        return 0
    if getattr(args, "print_manifest", False):
        _print_yaml(data)
    return 0


def _find_cursor_automations_root(extracted_dir: Path) -> Path | None:
    if extracted_dir.name == "automations" and extracted_dir.is_dir():
        return extracted_dir
    candidate = extracted_dir / "automations"
    if candidate.is_dir():
        return candidate
    for child in extracted_dir.iterdir():
        if child.is_dir() and (child / "automations").is_dir():
            return child / "automations"
    if any(
        (extracted_dir / sub / "automation.toml").is_file()
        for sub in (p.name for p in extracted_dir.iterdir() if p.is_dir())
    ):
        return extracted_dir
    return None


def _handle_cursor_automations_ingest(args: argparse.Namespace) -> int:
    source: Path | None = None
    cleanup_dir: tempfile.TemporaryDirectory | None = None
    try:
        if args.source:
            source = Path(args.source)
        elif args.artifact_zip:
            tmp = tempfile.TemporaryDirectory(prefix="bourdon-cursor-ingest-")
            cleanup_dir = tmp
            zip_path = Path(args.artifact_zip)
            if not zip_path.is_file():
                print(f"cursor-automations ingest: zip not found: {zip_path}", file=sys.stderr)
                return 2
            shutil.unpack_archive(str(zip_path), tmp.name)
            source = _find_cursor_automations_root(Path(tmp.name))
        else:
            print(
                "cursor-automations ingest: specify --source or --artifact-zip.",
                file=sys.stderr,
            )
            return 2
        if source is None or not source.is_dir():
            print(
                "cursor-automations ingest: could not locate an "
                "'automations/' directory inside the source.",
                file=sys.stderr,
            )
            return 2
        dest_dir = (
            Path(args.dest) if getattr(args, "dest", None)
            else default_cursor_automations_dir()
        )
        result = cursor_merge_automation_tree(
            source, dest_dir, default_kind=args.default_kind,
        )
        report = {
            "source": str(source), "dest": str(dest_dir),
            "automations_seen": result.automations_seen,
            "automations_created": result.automations_created,
            "bullets_added": result.bullets_added,
            "sections_created": result.sections_created,
            "skipped_invalid_id": list(result.skipped),
        }
        print(json.dumps(report, indent=2))
        return 0
    finally:
        if cleanup_dir is not None:
            cleanup_dir.cleanup()


def _handle_cursor_automations_doctor(args: argparse.Namespace) -> int:
    automations_dir = (
        Path(args.automations_dir)
        if getattr(args, "automations_dir", None) else None
    )
    participant = CursorAutomationsParticipant(automations_dir=automations_dir)
    health = participant.health_check()
    report = {
        "health": {
            "status": health.status, "reason": health.reason,
            "details": health.details,
        },
        "automations_dir": participant.native_path,
    }
    if health.proposed_fix:
        report["health"]["proposed_fix"] = health.proposed_fix
    _write_yaml_if_requested(report, getattr(args, "report_out", None))
    _print_yaml(report)
    return 0


def _handle_copilot_export(args: argparse.Namespace) -> int:
    copilot_dir = Path(args.copilot_dir) if getattr(args, "copilot_dir", None) else None
    participant = CopilotParticipant(copilot_dir=copilot_dir)
    manifest = participant.export_l5(since=_parse_since(args.since))
    data = filter_manifest_for_access(manifest, access_level=args.access_level)
    out_path = Path(args.out) if args.out else _default_copilot_l5_path()
    write_l5_dict(data, out_path)
    if args.print_manifest:
        _print_yaml(data)
    return 0


def _handle_codex_automations_export(args: argparse.Namespace) -> int:
    automations_dir = (
        Path(args.automations_dir)
        if getattr(args, "automations_dir", None)
        else None
    )
    participant = CodexAutomationsParticipant(automations_dir=automations_dir)
    manifest = participant.export_l5(since=_parse_since(args.since))
    data = filter_manifest_for_access(manifest, access_level=args.access_level)
    out_path = Path(args.out) if args.out else _default_codex_automations_l5_path()
    write_l5_dict(data, out_path)
    if args.print_manifest:
        _print_yaml(data)
    return 0


def _handle_codex_automations_doctor(args: argparse.Namespace) -> int:
    automations_dir = (
        Path(args.automations_dir)
        if getattr(args, "automations_dir", None)
        else None
    )
    participant = CodexAutomationsParticipant(automations_dir=automations_dir)
    health = participant.health_check()
    report = {
        "health": {
            "status": health.status,
            "reason": health.reason,
            "details": health.details,
        },
        "automations_dir": participant.native_path,
    }
    if health.proposed_fix:
        report["health"]["proposed_fix"] = health.proposed_fix
    _write_yaml_if_requested(report, getattr(args, "report_out", None))
    _print_yaml(report)
    return 0


def _handle_copilot_doctor(args: argparse.Namespace) -> int:
    copilot_dir = Path(args.copilot_dir) if getattr(args, "copilot_dir", None) else None
    participant = CopilotParticipant(copilot_dir=copilot_dir)
    health = participant.health_check()
    mem_report = _inspect_copilot_memory(copilot_dir)
    report = {
        "health": {
            "status": health.status,
            "reason": health.reason,
            "details": health.details,
        },
        "memory_file": mem_report,
        "memory_path": str(default_copilot_memory_path(copilot_dir)),
    }
    _write_yaml_if_requested(report, getattr(args, "report_out", None))
    _print_yaml(report)
    return 0


def _handle_copilot_init(args: argparse.Namespace) -> int:
    copilot_dir = Path(args.copilot_dir) if getattr(args, "copilot_dir", None) else None
    force = getattr(args, "force", False)
    try:
        path = init_memory_file(copilot_dir=copilot_dir, force=force)
        print(f"Created {path}")
        print("Edit it to add entities and sessions, then run `bourdon copilot export`.")
    except FileExistsError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


# -- Cascade handlers ---------------------------------------------------------


def _handle_cascade_export(args: argparse.Namespace) -> int:
    cascade_dir = Path(args.cascade_dir) if getattr(args, "cascade_dir", None) else None
    participant = CascadeParticipant(cascade_dir=cascade_dir)
    manifest = participant.export_l5(since=_parse_since(args.since))
    data = filter_manifest_for_access(manifest, access_level=args.access_level)
    out_path = Path(args.out) if args.out else _default_cascade_l5_path()
    write_l5_dict(data, out_path)
    if args.print_manifest:
        _print_yaml(data)
    return 0


def _handle_cascade_doctor(args: argparse.Namespace) -> int:
    cascade_dir = Path(args.cascade_dir) if getattr(args, "cascade_dir", None) else None
    participant = CascadeParticipant(cascade_dir=cascade_dir)
    health = participant.health_check()
    mem_report = _inspect_cascade_memory(cascade_dir or participant._dir)
    report = {
        "health": {
            "status": health.status,
            "reason": health.reason,
            "details": health.details,
        },
        "memory_file": mem_report,
        "memory_path": str(default_cascade_memory_path()),
    }
    _write_yaml_if_requested(report, getattr(args, "report_out", None))
    _print_yaml(report)
    return 0


def _handle_cascade_init(args: argparse.Namespace) -> int:
    cascade_dir = Path(args.cascade_dir) if getattr(args, "cascade_dir", None) else None
    force = getattr(args, "force", False)
    try:
        path = cascade_init_memory_file(cascade_dir=cascade_dir, force=force)
        print(f"Created {path}")
        print("Edit it to add entities and sessions, then run `bourdon cascade export`.")
    except FileExistsError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


# -- Top-level doctor / export-all --------------------------------------------


def _handle_doctor(args: argparse.Namespace) -> int:
    """Run health checks across all known participants."""
    results: list[dict[str, Any]] = []
    for agent_id, participant_cls in discover_participants():
        try:
            participant = participant_cls()
            health = participant.health_check()
            row: dict[str, Any] = {
                "agent": agent_id,
                "status": health.status,
                "reason": health.reason,
                "details": health.details,
            }
            if health.proposed_fix:
                row["proposed_fix"] = health.proposed_fix
            results.append(row)
        except Exception as exc:  # noqa: BLE001
            results.append({
                "agent": agent_id,
                "status": "error",
                "reason": str(exc),
                "details": {},
                "proposed_fix": (
                    "Participant raised during health_check. Run "
                    "`bourdon doctor --report-out doctor.yaml` and file an issue with "
                    "the traceback."
                ),
            })

    report = {"participants": results}
    _write_yaml_if_requested(report, getattr(args, "report_out", None))
    _print_yaml(report)
    return 0


def _handle_agents(args: argparse.Namespace) -> int:
    """Enumerate L5 manifests as a stable, redacted, source-attributed JSON object.

    Read foundation for the Phase 0 desktop tray: keeps redaction and
    access-level handling server-side so the tray never reads raw YAML. The
    summarization is the single shared implementation in
    :mod:`core.agents_export`, so the local and federated paths can never drift.

    Without ``--federated`` this enumerates only THIS machine's local agents,
    each tagged ``source=<local_name>`` / ``source_kind="local"``. Exits nonzero
    only if the agents dir itself is missing/unreadable; per-manifest parse
    errors are represented inline (``parse_error``) and still exit 0 so the tray
    can distinguish "no data" from "broken".

    With ``--federated`` it additionally fans out to configured peers via the
    L6 store, merging each peer's own ``export_agents`` output re-tagged
    ``source=<peer-name>`` / ``source_kind="peer"``. A peer that is unreachable
    (or runs a build without the ``export_agents`` tool) contributes nothing and
    is marked ``reachable: false`` rather than crashing the export.
    """
    agents_dir = (
        Path(args.agents_dir)
        if getattr(args, "agents_dir", None)
        else _default_agents_dir()
    )
    if not agents_dir.is_dir():
        print(
            f"agents: agent-library directory not found: {agents_dir}",
            file=sys.stderr,
        )
        return 2

    local_name = resolve_local_name()

    if getattr(args, "federated", False):
        from core.l6_server import load_peers
        from core.l6_store import L6Store

        peers_config = (
            Path(args.peers_config)
            if getattr(args, "peers_config", None)
            else _DEFAULT_PEERS_CONFIG
        )
        peers = load_peers(peers_config, [])
        # The store's library is the PARENT of the agents dir (it appends
        # ``agents/`` itself). For the default dir this is ~/agent-library.
        store = L6Store(agents_dir.parent, peers=peers)
        report = asyncio.run(store.export_agents_federated(local_name=local_name))
        print(json.dumps(report, indent=2, sort_keys=False))
        return 0

    report = export_local_agents(agents_dir, local_name)
    print(json.dumps(report, indent=2, sort_keys=False))
    return 0


def _handle_dogfood(args: argparse.Namespace) -> int:
    """Run an end-to-end federation smoke test on the local machine."""
    from cli.dogfood import format_matrix, run_dogfood

    report = run_dogfood(
        keep_marker=getattr(args, "keep_marker", False),
        access_level=getattr(args, "access_level", "team"),
    )
    print(format_matrix(report))
    _write_yaml_if_requested(report.to_dict(), getattr(args, "report_out", None))
    return 0 if report.passed else 1


def _handle_serve(args: argparse.Namespace) -> int:
    """Launch the L6 federation MCP server.

    Wrapper around ``python -m core.l6_server`` that prints an onboarding
    banner (library path, agents loaded, transport, peers, paste-ready MCP
    config snippet) before handing off to the shared :func:`run_l6_server`.
    Peer federation (``--peer`` / ``--peers-config``) and HTTP Bearer auth
    (``--allow-unauthenticated``) are resolved identically to the module entry
    point via :func:`load_peers` + :func:`run_l6_server`, so both serve paths
    behave the same. Stdio transport blocks until the connecting MCP client
    disconnects; HTTP transport blocks until interrupted. Returns the server's
    exit code (0 on clean shutdown / KeyboardInterrupt).
    """
    from core.l6_server import (  # type: ignore[attr-defined]
        DEFAULT_PEERS_CONFIG,
        L6Store,
        create_l6_server,
        load_peers,
        run_l6_server,
    )

    library_path = Path(args.library) if getattr(args, "library", None) else None
    if library_path is None:
        from core.l6_store import DEFAULT_LIBRARY_PATH
        library_path = DEFAULT_LIBRARY_PATH

    transport = getattr(args, "transport", "stdio")
    port = getattr(args, "port", 7500)
    host = getattr(args, "host", "0.0.0.0")
    allow_unauthenticated = getattr(args, "allow_unauthenticated", False)

    # Cross-machine peer federation (Phase 1.6+): merge --peer URLs with the
    # optional peers.yaml so `bourdon serve` matches `python -m core.l6_server`.
    peers_config = getattr(args, "peers_config", None) or DEFAULT_PEERS_CONFIG
    peer_urls = list(getattr(args, "peer", []) or [])
    peers = load_peers(Path(peers_config), peer_urls)

    store = L6Store(library_path, peers=peers)
    agents = store.list_agents()

    if getattr(args, "quiet", False) is False:
        # Banner goes to stderr so stdio transport (which uses stdout for the
        # MCP protocol) stays clean.
        print("Bourdon L6 server", file=sys.stderr)
        print(f"  library:   {library_path}", file=sys.stderr)
        agent_names = ", ".join(agents) if agents else "none"
        print(f"  agents:    {len(agents)} loaded ({agent_names})", file=sys.stderr)
        print(f"  transport: {transport}", file=sys.stderr)
        if transport == "http":
            print(f"  bind:      {host}:{port}", file=sys.stderr)
            auth_state = (
                "disabled (--allow-unauthenticated)"
                if allow_unauthenticated
                else "Bearer (set BOURDON_PEER_TOKEN_SERVER)"
            )
            print(f"  auth:      {auth_state}", file=sys.stderr)
        if peers:
            peer_desc = ", ".join(f"{p.name} -> {p.url}" for p in peers)
            print(f"  peers:     {len(peers)} ({peer_desc})", file=sys.stderr)
        print("", file=sys.stderr)
        if transport == "stdio":
            print("MCP client config (stdio):", file=sys.stderr)
            print('  {"command": "bourdon", "args": ["serve"]}', file=sys.stderr)
        else:
            print(f"MCP client endpoint (http): http://127.0.0.1:{port}/mcp", file=sys.stderr)
        print("", file=sys.stderr)

    server = create_l6_server(store)
    try:
        run_l6_server(
            server,
            transport=transport,
            port=port,
            host=host,
            allow_unauthenticated=allow_unauthenticated,
        )
    except KeyboardInterrupt:
        return 0
    return 0


def _handle_export_all(args: argparse.Namespace) -> int:
    """Export L5 manifests for all healthy participants."""
    access_level = args.access_level
    since = _parse_since(getattr(args, "since", None))
    results: list[dict[str, Any]] = []

    for agent_id, participant_cls in discover_participants():
        try:
            participant = participant_cls()
            manifest = participant.export_l5(since=since)
            data = filter_manifest_for_access(manifest, access_level=access_level)
            out_path = (
                Path(args.library) / "agents" / f"{agent_id}.l5.yaml"
            )
            write_l5_dict(data, out_path)
            entity_count = len(data.get("known_entities") or [])
            session_count = len(data.get("recent_sessions") or [])
            results.append({
                "agent": agent_id,
                "status": "ok",
                "path": str(out_path),
                "entities": entity_count,
                "sessions": session_count,
            })
        except Exception as exc:  # noqa: BLE001
            results.append({
                "agent": agent_id,
                "status": "error",
                "reason": str(exc),
            })

    report = {"exports": results}
    _write_yaml_if_requested(report, getattr(args, "report_out", None))
    _print_yaml(report)
    return 0


def _handle_sync_push(args: argparse.Namespace) -> int:
    """Stage a visibility-filtered library copy and rsync it to ``dest``."""
    from core.sync import (
        DEFAULT_PUSH_ACCESS_LEVEL,
        RsyncMissingError,
        SyncError,
        sync_push,
        visible_counts,
    )

    library_path = (
        Path(args.library_path) if getattr(args, "library_path", None) else None
    )
    library = library_path or DEFAULT_LIBRARY_PATH
    access_level = getattr(args, "access_level", DEFAULT_PUSH_ACCESS_LEVEL)

    # Surface the visibility-filter outcome before the network leg.
    try:
        counts = visible_counts(library, access_level)
    except SyncError as exc:
        print(f"sync push: {exc}", file=sys.stderr)
        return 2

    print(
        f"sync push: library={library} access_level={access_level} dest={args.dest}",
        file=sys.stderr,
    )
    for agent, c in sorted(counts.items()):
        print(
            f"  {agent}: {c['entities']} entities, {c['sessions']} sessions",
            file=sys.stderr,
        )

    try:
        result = sync_push(
            args.dest,
            access_level=access_level,
            library_path=library_path,
            dry_run=bool(getattr(args, "dry_run", False)),
            delete=bool(getattr(args, "delete", False)),
            verbose=bool(getattr(args, "verbose", False)),
        )
    except RsyncMissingError as exc:
        print(f"sync push: {exc}", file=sys.stderr)
        return 127
    except SyncError as exc:
        print(f"sync push: {exc}", file=sys.stderr)
        return 2

    return int(result.returncode)


def _handle_sync_pull(args: argparse.Namespace) -> int:
    """rsync a remote library into the local one."""
    from core.sync import RsyncMissingError, SyncError, sync_pull

    library_path = (
        Path(args.library_path) if getattr(args, "library_path", None) else None
    )
    library = library_path or DEFAULT_LIBRARY_PATH

    print(
        f"sync pull: library={library} src={args.src}",
        file=sys.stderr,
    )

    try:
        result = sync_pull(
            args.src,
            library_path=library_path,
            dry_run=bool(getattr(args, "dry_run", False)),
            delete=bool(getattr(args, "delete", False)),
            verbose=bool(getattr(args, "verbose", False)),
        )
    except RsyncMissingError as exc:
        print(f"sync pull: {exc}", file=sys.stderr)
        return 127
    except SyncError as exc:
        print(f"sync pull: {exc}", file=sys.stderr)
        return 2

    return int(result.returncode)


def _handle_codex_build_context(args: argparse.Namespace) -> int:
    participant = _build_participant(args)
    manifest = _manifest_for_access(participant, since=_parse_since(args.since), access_level="team")
    report = write_codex_context_artifacts(manifest, Path(args.out_dir), access_level="team")
    _print_yaml(report)
    return 0


def _inspect_l5_quality(manifest: dict[str, Any]) -> dict[str, Any]:
    oversized_actions: list[dict[str, Any]] = []
    for session_index, session in enumerate(manifest.get("recent_sessions") or []):
        for action_index, action in enumerate(session.get("key_actions") or []):
            action_text = str(action)
            if len(action_text) <= 500:
                continue
            oversized_actions.append(
                {
                    "session_index": session_index,
                    "action_index": action_index,
                    "bytes": len(action_text.encode("utf-8")),
                }
            )

    duplicated_entities: list[dict[str, Any]] = []
    for entity_index, entity in enumerate(manifest.get("known_entities") or []):
        name = str(entity.get("name") or "").strip()
        summary = str(entity.get("summary") or "").strip()
        if not name or name != summary:
            continue
        duplicated_entities.append(
            {
                "entity_index": entity_index,
                "name": _safe_native_memory_text(name, limit=120),
            }
        )

    warnings: list[str] = []
    if oversized_actions:
        warnings.append("L5 contains oversized session key_actions.")
    if duplicated_entities:
        warnings.append("L5 contains entities with duplicated name and summary.")

    return {
        "status": "warn" if warnings else "ok",
        "oversized_key_actions": len(oversized_actions),
        "duplicated_name_summary_entities": len(duplicated_entities),
        "warnings": warnings,
        "samples": {
            "oversized_key_actions": oversized_actions[:5],
            "duplicated_name_summary_entities": duplicated_entities[:5],
        },
    }


def _handle_codex_doctor(args: argparse.Namespace) -> int:
    participant = _build_participant(args)
    try:
        manifest = participant.export_l5().to_dict()
        l5_quality = _inspect_l5_quality(manifest)
    except Exception as exc:  # noqa: BLE001
        l5_quality = {
            "status": "unavailable",
            "reason": str(exc),
        }
    report = {
        "source_coverage": _source_coverage(participant),
        "codex_state_db": _inspect_codex_state_db(participant._codex_home),
        "fallback_recall": _inspect_codex_fallback_recall(
            participant._codex_home,
            participant._codex_brain,
        ),
        "l5_quality": l5_quality,
    }
    _write_yaml_if_requested(report, args.report_out)
    _print_yaml(report)
    return 0


def _handle_codex_sync_native(args: argparse.Namespace) -> int:
    participant = _build_participant(args)
    library_path = (
        Path(args.library_path) if getattr(args, "library_path", None) else None
    )
    payload = _build_codex_native_memory_payload(
        participant._codex_home,
        participant._codex_brain,
        max_sessions=args.max_sessions,
        from_library=bool(getattr(args, "from_library", False)),
        include_local=bool(getattr(args, "include_local", False)),
        library_path=library_path,
        access_level=getattr(args, "access_level", "team"),
    )
    target_kind = "memory_md" if args.memory_md else "bourdon_file"
    target = (
        Path(args.out)
        if getattr(args, "out", None)
        else (
            _default_codex_memory_md_path(participant._codex_home)
            if args.memory_md
            else _default_codex_native_memory_path(participant._codex_home)
        )
    )
    mode = "write" if args.write else "dry-run"
    text = str(payload["text"])
    if args.memory_md:
        existing_text = target.read_text(encoding="utf-8") if target.is_file() else ""
        text = _merge_bourdon_memory_md_section(existing_text, text)
    written = False
    if args.write:
        _write_text_atomic(text, target)
        written = True

    report = {
        "mode": mode,
        "target": str(target),
        "target_kind": target_kind,
        "would_write": bool(text.strip()),
        "written": written,
        "bytes": payload["bytes"],
        "fallback_recall": payload["fallback_recall"],
    }
    if not args.write:
        report["preview"] = text
    _print_yaml(report)
    return 0


def _build_recognition_prompt_context(result: Any) -> str:
    if not result.recognition:
        return ""

    lines = [
        "Bourdon recognition context",
        f"Immediate recognition: {_safe_native_memory_text(result.recognition)}",
    ]
    if result.matched_entities:
        lines.append("Matched entities:")
    for entity in result.matched_entities:
        name = _safe_native_memory_text(str(entity.get("name") or ""))
        entity_type = _safe_native_memory_text(str(entity.get("type") or "topic"))
        summary = str(entity.get("summary") or "").strip()
        line = f"- {name} ({entity_type})"
        if summary:
            line += f": {_safe_native_memory_text(summary, limit=240)}"
        lines.append(line)
    lines.append("Use this as timing-layer context, not as a final answer.")
    return "\n".join(lines)


def _handle_codex_recognize(args: argparse.Namespace) -> int:
    participant = _build_participant(args)
    manifest = _manifest_for_access(
        participant,
        since=_parse_since(args.since),
        access_level=args.access_level,
    )
    t0 = _time.perf_counter()
    result = recognition_first(
        args.prompt,
        manifest,
        access_level=args.access_level,
    )
    recognition_us = (_time.perf_counter() - t0) * 1_000_000
    hydration = result.hydration
    hydration_scheduled = hydration is not None
    if hydration is not None:
        hydration.close()

    report = {
        "mode": "live",
        "access_level": args.access_level,
        "prompt": args.prompt,
        "recognition": result.recognition,
        "matched_entities": [
            {
                "name": str(entity.get("name") or ""),
                "type": str(entity.get("type") or "topic"),
            }
            for entity in result.matched_entities
        ],
        "recognition_latency_us": round(recognition_us, 1),
        "hydration_scheduled": hydration_scheduled,
    }
    if args.prompt_context:
        report["prompt_context"] = _build_recognition_prompt_context(result)
    _write_yaml_if_requested(report, args.report_out)
    _print_yaml(report)
    return 0


def _handle_codex_prepare_turn(args: argparse.Namespace) -> int:
    participant = _build_participant(args)
    access_level = args.access_level
    since = _parse_since(args.since)
    mode = "write" if args.write else "dry-run"
    strategy = getattr(args, "strategy", "legacy")

    native_payload = _build_codex_native_memory_payload(
        participant._codex_home,
        participant._codex_brain,
        max_sessions=args.max_sessions,
    )
    native_target_kind = "memory_md" if args.memory_md else "bourdon_file"
    native_target = (
        Path(args.native_out)
        if getattr(args, "native_out", None)
        else (
            _default_codex_memory_md_path(participant._codex_home)
            if args.memory_md
            else _default_codex_native_memory_path(participant._codex_home)
        )
    )
    native_text = str(native_payload["text"])
    if args.memory_md:
        existing_text = (
            native_target.read_text(encoding="utf-8")
            if native_target.is_file()
            else ""
        )
        native_text = _merge_bourdon_memory_md_section(existing_text, native_text)

    manifest = _manifest_for_access(
        participant,
        since=since,
        access_level=access_level,
    )
    l5_target = Path(args.l5_out) if args.l5_out else _default_codex_l5_path()

    t0 = _time.perf_counter()
    result = recognition_first(
        args.prompt,
        manifest,
        access_level=access_level,
    )
    recognition_us = (_time.perf_counter() - t0) * 1_000_000
    hydration = result.hydration
    hydration_scheduled = hydration is not None
    if hydration is not None:
        hydration.close()

    native_written = False
    l5_written = False
    if args.write:
        _write_text_atomic(native_text, native_target)
        write_l5_dict(manifest, l5_target)
        native_written = True
        l5_written = True

    recognition_report = {
        "prompt": args.prompt,
        "recognition": result.recognition,
        "matched_entities": [
            {
                "name": str(entity.get("name") or ""),
                "type": str(entity.get("type") or "topic"),
            }
            for entity in result.matched_entities
        ],
        "recognition_latency_us": round(recognition_us, 1),
        "hydration_scheduled": hydration_scheduled,
    }
    prompt_context = _build_recognition_prompt_context(result)
    compiled_turn: dict[str, Any] | None = None
    if strategy == "turn-compiled":
        try:
            compiled = compile_codex_turn(
                args.prompt,
                cwd=getattr(args, "cwd", None),
                codex_home=participant._codex_home,
                library_path=getattr(args, "library_path", None),
                access_level=access_level,
                max_items=getattr(args, "max_items", 6),
                max_chars=getattr(args, "max_chars", 1800),
                delivery="all",
            )
        except ValueError as exc:
            print(f"prepare-turn: {exc}", file=sys.stderr)
            return 2
        compiled_turn = compiled.to_dict()
        prompt_context = str(compiled_turn["delivery"]["explicit_text"])

    report = {
        "mode": mode,
        "strategy": strategy,
        "access_level": access_level,
        "recognition": recognition_report,
        "prompt_context": prompt_context,
        "fallback_recall": native_payload["fallback_recall"],
        "writes": {
            "native_memory": {
                "target": str(native_target),
                "target_kind": native_target_kind,
                "would_write": bool(native_text.strip()),
                "written": native_written,
                "bytes": len(native_text.encode("utf-8")),
            },
            "l5": {
                "target": str(l5_target),
                "would_write": True,
                "written": l5_written,
                "entity_count": len(manifest.get("known_entities") or []),
                "session_count": len(manifest.get("recent_sessions") or []),
            },
        },
    }
    if compiled_turn is not None:
        report["compiled_turn"] = compiled_turn
    _write_yaml_if_requested(report, args.report_out)
    _print_yaml(report)
    return 0


def _handle_codex_compile_turn(args: argparse.Namespace) -> int:
    try:
        brief = compile_codex_turn(
            args.prompt,
            cwd=getattr(args, "cwd", None),
            codex_home=getattr(args, "codex_home", None),
            library_path=getattr(args, "library_path", None),
            access_level=args.access_level,
            max_items=args.max_items,
            max_chars=args.max_chars,
            delivery=args.delivery,
        )
    except ValueError as exc:
        print(f"compile-turn: {exc}", file=sys.stderr)
        return 2

    data = brief.to_dict()
    _write_yaml_if_requested(data, args.report_out)
    if args.format == "json":
        print(json.dumps(data, indent=2, sort_keys=False))
    else:
        _print_yaml(data)
    return 0


def _fixture_participant() -> CodexParticipant:
    tmpdir = tempfile.TemporaryDirectory()
    sources = create_sample_codex_sources(Path(tmpdir.name) / "home")
    participant = CodexParticipant(
        codex_home=sources["codex_home"],
        codex_brain=sources["codex_brain"],
    )
    participant._fixture_tmpdir = tmpdir  # type: ignore[attr-defined]
    return participant


def _source_coverage(participant: CodexParticipant) -> dict[str, Any]:
    health = participant.health_check()
    details = health.details or {}
    return {
        "status": health.status,
        "state_db": details.get("state_db") != "missing",
        "session_index": details.get("session_index") != "missing",
        "sessions_dir": details.get("sessions_dir") != "missing",
        "memory_md": details.get("memory_md") != "missing",
        "raw_memories": details.get("raw_memories") != "missing",
        "rollout_summaries_dir": details.get("rollout_summaries_dir") != "missing",
        "codex_brain": details.get("codex_brain") != "missing",
    }


CANONICAL_RECOGNITION_PROMPTS = [
    "Tell me about Coolculator",
    "What is Fastify?",
    "Anything new on Mac handoff?",
    "Remind me what the rollout was about",
    "What's the weather like?",  # negative control -- should not match
]
"""Canonical prompts for the recognition harness.

Mixed by design: the first four are fixture-friendly (the bundled codex
fixtures include Coolculator + Fastify entities, plus 'Mac handoff' as a
known keyword) so the test suite gets deterministic positive hits. The
fifth is a negative control that should never match -- it guards against
over-eager substring matching in detect_entities.

When run against live data (`--live`), the positive hits depend on what's
actually in the user's manifest. The first four prompts work as-is on a
typical developer machine (Coolculator + Fastify are common topic names
in shipping code) but a more representative live evaluation would replace
them with prompts based on the user's own recent threads."""


def _recognition_eval(
    manifest: Any, prompts: list[str] = CANONICAL_RECOGNITION_PROMPTS
) -> dict[str, Any]:
    """
    Run :func:`recognition_first` against a list of prompts and return an
    aggregated report.

    Reports per-prompt: the recognition string, matched entity names,
    recognition latency (microseconds), hydration latency (milliseconds).
    Reports aggregate: hit rate, average latencies. Hydration runs through
    asyncio.run so this helper can be called from a synchronous handler.
    """

    async def _run_one(prompt: str) -> dict[str, Any]:
        t0 = _time.perf_counter()
        result = recognition_first(prompt, manifest)
        recognition_us = (_time.perf_counter() - t0) * 1_000_000

        hydration_ms = 0.0
        hydration_chars = 0
        if result.hydration is not None:
            t1 = _time.perf_counter()
            try:
                hydration = await result.hydration
            except Exception:  # noqa: BLE001 -- harness must not crash
                hydration = ""
            hydration_ms = (_time.perf_counter() - t1) * 1_000
            hydration_chars = len(hydration)

        return {
            "prompt": prompt,
            "recognition": result.recognition,
            "matched_entities": [
                str(e.get("name") or "") for e in result.matched_entities
            ],
            "recognition_latency_us": round(recognition_us, 1),
            "hydration_latency_ms": round(hydration_ms, 1),
            "hydration_chars": hydration_chars,
        }

    async def _run_all() -> list[dict[str, Any]]:
        return [await _run_one(p) for p in prompts]

    results = asyncio.run(_run_all())

    n = len(results)
    hits = sum(1 for r in results if r["recognition"])
    avg_recog_us = (
        sum(r["recognition_latency_us"] for r in results) / n if n else 0.0
    )
    avg_hyd_ms = (
        sum(r["hydration_latency_ms"] for r in results) / n if n else 0.0
    )

    return {
        "prompts_tested": n,
        "recognition_hits": hits,
        "recognition_hit_rate": round(hits / n, 2) if n else 0.0,
        "avg_recognition_latency_us": round(avg_recog_us, 1),
        "avg_hydration_latency_ms": round(avg_hyd_ms, 1),
        "results": results,
    }


def _turn_compiler_eval(
    *,
    prompts: list[str],
    codex_home: Path,
    library_path: Path,
    cwd: str | None,
    access_level: str,
    max_items: int,
    max_chars: int,
) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    latencies_us: list[float] = []
    primary_surfaces: Counter[str] = Counter()
    confidence_counts: Counter[str] = Counter()

    for prompt in prompts:
        t0 = _time.perf_counter()
        brief = compile_codex_turn(
            prompt,
            cwd=cwd,
            codex_home=codex_home,
            library_path=library_path,
            access_level=access_level,
            max_items=max_items,
            max_chars=max_chars,
            delivery="all",
        )
        latency_us = (_time.perf_counter() - t0) * 1_000_000
        latencies_us.append(latency_us)

        data = brief.to_dict()
        items = data["items"]
        routing = data["routing"]
        primary_surface = str(routing["primary_surface"])
        confidence = str(routing["confidence"])
        primary_surfaces[primary_surface] += 1
        confidence_counts[confidence] += 1
        top_item = items[0] if items else None
        results.append(
            {
                "prompt": prompt,
                "native_stage1": data["health"]["native_stage1"],
                "item_count": len(items),
                "top_item": top_item["name"] if top_item else None,
                "top_score": top_item["score"] if top_item else 0.0,
                "primary_surface": primary_surface,
                "confidence": confidence,
                "latency_us": round(latency_us, 1),
            }
        )

    n = len(results)
    avg_latency_us = sum(latencies_us) / n if n else 0.0
    hits = sum(1 for result in results if result["item_count"] > 0)

    return {
        "prompts_tested": n,
        "compiled_hits": hits,
        "compiled_hit_rate": round(hits / n, 2) if n else 0.0,
        "avg_latency_us": round(avg_latency_us, 1),
        "primary_surfaces": dict(primary_surfaces),
        "confidence_counts": dict(confidence_counts),
        "results": results,
    }


def _handle_codex_eval(args: argparse.Namespace) -> int:
    participant = _fixture_participant() if args.fixtures else _build_participant(args)
    manifest = _manifest_for_access(
        participant,
        since=_parse_since(args.since),
        access_level=args.access_level,
    )
    with tempfile.TemporaryDirectory() as tmpdir:
        context_report = write_codex_context_artifacts(
            manifest,
            Path(tmpdir) / "context",
            access_level=args.access_level,
        )

    entities = manifest.get("known_entities") or []
    sessions = manifest.get("recent_sessions") or []
    entity_counts = Counter(entity.get("type") or "topic" for entity in entities)
    visibility_counts = Counter(entity.get("visibility") or "public" for entity in entities)
    project_hits = [
        entity["name"]
        for entity in entities
        if entity.get("type") == "project"
    ][:5]
    preference_hits = [
        entity["name"]
        for entity in entities
        if entity.get("type") == "preference"
    ][:5]

    report = {
        "mode": "fixtures" if args.fixtures else "live",
        "access_level": args.access_level,
        "source_coverage": _source_coverage(participant),
        "session_count": len(sessions),
        "entity_counts": {
            "total": len(entities),
            "by_type": dict(entity_counts),
        },
        "visibility_counts": dict(visibility_counts),
        "context_generation": context_report,
        "recognition_spot_checks": {
            "projects": project_hits,
            "preferences": preference_hits,
        },
    }

    # --recognition flag: also run recognition_runtime against canonical
    # prompts and attach a behavior-layer eval to the report. This is the
    # measurable counterpart to the data-layer counts above; together they
    # let us track both `does the manifest contain the right entities?`
    # and `does recognition fire on them in microseconds without retrieval?`
    if getattr(args, "recognition", False):
        report["recognition"] = _recognition_eval(manifest)

    if getattr(args, "turn_compiler", False):
        with tempfile.TemporaryDirectory() as tmpdir:
            if args.fixtures or not getattr(args, "library_path", None):
                compiler_library = Path(tmpdir) / "agent-library"
                write_l5_dict(manifest, compiler_library / "agents" / "codex.l5.yaml")
            else:
                compiler_library = Path(args.library_path)
            report["turn_compiler"] = _turn_compiler_eval(
                prompts=CANONICAL_RECOGNITION_PROMPTS,
                codex_home=participant._codex_home,
                library_path=compiler_library,
                cwd=getattr(args, "cwd", None),
                access_level=args.access_level,
                max_items=getattr(args, "max_items", 6),
                max_chars=getattr(args, "max_chars", 1800),
            )

    _write_yaml_if_requested(report, args.report_out)
    _print_yaml(report)
    return 0


def _handle_claude_code_export(args: argparse.Namespace) -> int:
    """
    Build a Claude Code L5 manifest and write it to ``~/agent-library/agents/
    claude-code.l5.yaml`` (or ``--out`` if specified). Designed for use as a
    SessionEnd hook in Claude Code:

      Add to ~/.claude/settings.json:
        "hooks": {
          "SessionEnd": [
            { "command": "bourdon claude-code export" }
          ]
        }

    Operates silently on success and **never raises** -- a session-end hook
    that crashes is worse than a session-end hook that does nothing. Returns
    0 in all observable failure modes; use --verbose to surface diagnostics
    to stderr.
    """
    try:
        participant = ClaudeCodeParticipant()
    except Exception as exc:  # noqa: BLE001 -- hook contract: never raises
        if args.verbose:
            print(
                f"bourdon claude-code export: participant init failed: {exc}",
                file=sys.stderr,
            )
        return 0

    try:
        manifest = participant.export_l5(since=_parse_since(args.since))
    except ParticipantDiscoveryError as exc:
        if args.verbose:
            print(
                "bourdon claude-code export: no Claude Code memory sources "
                f"found ({exc}), skipping",
                file=sys.stderr,
            )
        return 0
    except Exception as exc:  # noqa: BLE001 -- hook contract
        if args.verbose:
            print(
                f"bourdon claude-code export: export failed: {exc}",
                file=sys.stderr,
            )
        return 0

    data = filter_manifest_for_access(manifest, access_level=args.access_level)

    out_path = Path(args.out) if args.out else _default_claude_code_l5_path()
    try:
        write_l5_dict(data, out_path)
    except Exception as exc:  # noqa: BLE001 -- hook contract
        if args.verbose:
            print(
                f"bourdon claude-code export: write to {out_path} failed: {exc}",
                file=sys.stderr,
            )
        return 0

    if getattr(args, "print_manifest", False):
        _print_yaml(data)
    elif args.verbose:
        print(
            f"bourdon claude-code export: wrote {out_path}",
            file=sys.stderr,
        )
    return 0


def _handle_claude_code_automations_export(args: argparse.Namespace) -> int:
    """
    Build a Claude Code automations L5 manifest from
    ``~/.claude/automations/<id>/{automation.toml, memory.md}`` and write it
    to ``~/agent-library/agents/claude-code-automations.l5.yaml`` (or
    ``--out`` if specified).

    Designed for use both as a SessionEnd companion to ``claude-code export``
    and as a cron-friendly publisher for automations that have no associated
    interactive session. Silent on success; never raises -- matches the hook
    contract of ``_handle_claude_code_export``.
    """
    automations_dir = (
        Path(args.automations_dir)
        if getattr(args, "automations_dir", None)
        else None
    )
    try:
        participant = ClaudeCodeAutomationsParticipant(automations_dir=automations_dir)
    except Exception as exc:  # noqa: BLE001 -- hook contract: never raises
        if getattr(args, "verbose", False):
            print(
                f"bourdon claude-code-automations export: participant init failed: {exc}",
                file=sys.stderr,
            )
        return 0

    try:
        manifest = participant.export_l5(since=_parse_since(args.since))
    except ParticipantDiscoveryError as exc:
        if getattr(args, "verbose", False):
            print(
                "bourdon claude-code-automations export: no automations "
                f"directory found ({exc}), skipping",
                file=sys.stderr,
            )
        return 0
    except Exception as exc:  # noqa: BLE001 -- hook contract
        if getattr(args, "verbose", False):
            print(
                f"bourdon claude-code-automations export: export failed: {exc}",
                file=sys.stderr,
            )
        return 0

    data = filter_manifest_for_access(manifest, access_level=args.access_level)

    out_path = (
        Path(args.out) if args.out else _default_claude_code_automations_l5_path()
    )
    try:
        write_l5_dict(data, out_path)
    except Exception as exc:  # noqa: BLE001 -- hook contract
        if getattr(args, "verbose", False):
            print(
                f"bourdon claude-code-automations export: write to {out_path} "
                f"failed: {exc}",
                file=sys.stderr,
            )
        return 0

    if getattr(args, "print_manifest", False):
        _print_yaml(data)
    elif getattr(args, "verbose", False):
        print(
            f"bourdon claude-code-automations export: wrote {out_path}",
            file=sys.stderr,
        )
    return 0


def _handle_claude_desktop_cowork_export(args: argparse.Namespace) -> int:
    """Build a Claude Desktop Co-Work L5 manifest and write it to
    ``~/agent-library/agents/claude-desktop-cowork.l5.yaml`` (or ``--out``).

    Emits recognition metadata only -- never conversation content. Silent on
    success; never raises -- matches the SessionEnd hook contract of
    ``_handle_claude_code_export``.
    """
    store_dir = Path(args.store_dir) if getattr(args, "store_dir", None) else None
    try:
        participant = ClaudeDesktopCoworkParticipant(store_dir=store_dir)
    except Exception as exc:  # noqa: BLE001 -- hook contract: never raises
        if getattr(args, "verbose", False):
            print(
                f"bourdon claude-desktop-cowork export: participant init failed: {exc}",
                file=sys.stderr,
            )
        return 0

    try:
        manifest = participant.export_l5(since=_parse_since(args.since))
    except ParticipantDiscoveryError as exc:
        if getattr(args, "verbose", False):
            print(
                "bourdon claude-desktop-cowork export: no Co-Work store "
                f"found ({exc}), skipping",
                file=sys.stderr,
            )
        return 0
    except Exception as exc:  # noqa: BLE001 -- hook contract
        if getattr(args, "verbose", False):
            print(
                f"bourdon claude-desktop-cowork export: export failed: {exc}",
                file=sys.stderr,
            )
        return 0

    data = filter_manifest_for_access(manifest, access_level=args.access_level)

    out_path = Path(args.out) if args.out else _default_claude_desktop_cowork_l5_path()
    try:
        write_l5_dict(data, out_path)
    except Exception as exc:  # noqa: BLE001 -- hook contract
        if getattr(args, "verbose", False):
            print(
                f"bourdon claude-desktop-cowork export: write to {out_path} failed: {exc}",
                file=sys.stderr,
            )
        return 0

    if getattr(args, "print_manifest", False):
        _print_yaml(data)
    elif getattr(args, "verbose", False):
        print(
            f"bourdon claude-desktop-cowork export: wrote {out_path}",
            file=sys.stderr,
        )
    return 0


def _handle_claude_desktop_code_export(args: argparse.Namespace) -> int:
    """Build a Claude Desktop Code (GUI) L5 manifest and write it to
    ``~/agent-library/agents/claude-desktop-code.l5.yaml`` (or ``--out``).

    Emits recognition metadata only -- never conversation content. Silent on
    success; never raises -- matches the SessionEnd hook contract of
    ``_handle_claude_code_export``.
    """
    store_dir = Path(args.store_dir) if getattr(args, "store_dir", None) else None
    try:
        participant = ClaudeDesktopCodeParticipant(store_dir=store_dir)
    except Exception as exc:  # noqa: BLE001 -- hook contract: never raises
        if getattr(args, "verbose", False):
            print(
                f"bourdon claude-desktop-code export: participant init failed: {exc}",
                file=sys.stderr,
            )
        return 0

    try:
        manifest = participant.export_l5(since=_parse_since(args.since))
    except ParticipantDiscoveryError as exc:
        if getattr(args, "verbose", False):
            print(
                "bourdon claude-desktop-code export: no Code store "
                f"found ({exc}), skipping",
                file=sys.stderr,
            )
        return 0
    except Exception as exc:  # noqa: BLE001 -- hook contract
        if getattr(args, "verbose", False):
            print(
                f"bourdon claude-desktop-code export: export failed: {exc}",
                file=sys.stderr,
            )
        return 0

    data = filter_manifest_for_access(manifest, access_level=args.access_level)

    out_path = Path(args.out) if args.out else _default_claude_desktop_code_l5_path()
    try:
        write_l5_dict(data, out_path)
    except Exception as exc:  # noqa: BLE001 -- hook contract
        if getattr(args, "verbose", False):
            print(
                f"bourdon claude-desktop-code export: write to {out_path} failed: {exc}",
                file=sys.stderr,
            )
        return 0

    if getattr(args, "print_manifest", False):
        _print_yaml(data)
    elif getattr(args, "verbose", False):
        print(
            f"bourdon claude-desktop-code export: wrote {out_path}",
            file=sys.stderr,
        )
    return 0


def _handle_claude_code_automations_ingest(args: argparse.Namespace) -> int:
    """Ingest an ``automations/`` tree into the local one.

    Four modes (mutually exclusive, mode is auto-detected from flags):

      1. ``--source <local-dir>`` -- merge from an already-downloaded tree
         (also serves the claude-brain-relay case: ``--source
         ~/claude-brain/automations``).
      2. ``--artifact-zip <path>`` -- unzip a workflow artifact zip first.
      3. ``--repo owner/name --run <run-id>`` -- shell out to ``gh run
         download`` to fetch the named artifact, then merge.
      4. ``--gh-issue owner/name#N --automation-id <id>`` -- shell out to
         ``gh issue view`` to read the issue body + comments, treat each as
         a memory.md run entry for the given automation_id. This is the
         routine-self-report relay (Path C of the federation plan).

    On success prints a JSON summary. Non-zero exit only on config error.
    """
    source: Path | None = None
    cleanup_dir: tempfile.TemporaryDirectory | None = None

    try:
        if args.source:
            source = Path(args.source)
        elif args.gh_issue:
            if shutil.which("gh") is None:
                print(
                    "claude-code-automations ingest: 'gh' CLI not on PATH "
                    "-- install GitHub CLI to use --gh-issue.",
                    file=sys.stderr,
                )
                return 127
            if not args.automation_id:
                print(
                    "claude-code-automations ingest: --gh-issue requires "
                    "--automation-id <id>.",
                    file=sys.stderr,
                )
                return 2
            try:
                repo_part, issue_num = args.gh_issue.rsplit("#", 1)
            except ValueError:
                print(
                    "claude-code-automations ingest: --gh-issue must be "
                    "'owner/repo#N'.",
                    file=sys.stderr,
                )
                return 2
            tmp = tempfile.TemporaryDirectory(prefix="bourdon-cca-issue-")
            cleanup_dir = tmp
            try:
                source = _build_source_from_gh_issue(
                    Path(tmp.name),
                    repo=repo_part,
                    issue_number=issue_num,
                    automation_id=args.automation_id,
                    gh_runner=subprocess.run,
                )
            except _GhIssueIngestError as exc:
                print(
                    f"claude-code-automations ingest: {exc}",
                    file=sys.stderr,
                )
                return 2
        elif args.artifact_zip:
            tmp = tempfile.TemporaryDirectory(prefix="bourdon-cca-ingest-")
            cleanup_dir = tmp
            zip_path = Path(args.artifact_zip)
            if not zip_path.is_file():
                print(
                    f"claude-code-automations ingest: artifact zip not found: {zip_path}",
                    file=sys.stderr,
                )
                return 2
            shutil.unpack_archive(str(zip_path), tmp.name)
            source = _find_automations_root(Path(tmp.name))
        elif args.repo and args.run:
            if shutil.which("gh") is None:
                print(
                    "claude-code-automations ingest: 'gh' CLI not on PATH "
                    "-- install GitHub CLI or use --source / --artifact-zip.",
                    file=sys.stderr,
                )
                return 127
            tmp = tempfile.TemporaryDirectory(prefix="bourdon-cca-ingest-")
            cleanup_dir = tmp
            cmd = [
                "gh", "run", "download", str(args.run),
                "--repo", args.repo,
                "--name", args.artifact_name,
                "--dir", tmp.name,
            ]
            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode != 0:
                print(
                    f"claude-code-automations ingest: gh download failed: {result.stderr.strip()}",
                    file=sys.stderr,
                )
                return result.returncode
            source = _find_automations_root(Path(tmp.name))
        else:
            print(
                "claude-code-automations ingest: must specify one of "
                "--source / --artifact-zip / (--repo + --run) / --gh-issue.",
                file=sys.stderr,
            )
            return 2

        if source is None or not source.is_dir():
            print(
                "claude-code-automations ingest: could not locate an "
                "'automations/' directory inside the source.",
                file=sys.stderr,
            )
            return 2

        dest_dir = (
            Path(args.dest)
            if args.dest
            else default_claude_code_automations_dir()
        )
        result = merge_automation_tree(source, dest_dir, default_kind=args.default_kind)

        report = {
            "source": str(source),
            "dest": str(dest_dir),
            "automations_seen": result.automations_seen,
            "automations_created": result.automations_created,
            "bullets_added": result.bullets_added,
            "sections_created": result.sections_created,
            "skipped_invalid_id": list(result.skipped),
        }
        # JSON for easy piping; YAML would be ambiguous with the doctor command.
        print(json.dumps(report, indent=2))
        return 0
    finally:
        if cleanup_dir is not None:
            cleanup_dir.cleanup()


class _GhIssueIngestError(Exception):
    """Raised when gh issue view fails or returns unparsable data."""


def _build_source_from_gh_issue(
    tmpdir: Path,
    repo: str,
    issue_number: str,
    automation_id: str,
    gh_runner,
) -> Path:
    """Materialize a synthetic automations/ tree from a GitHub issue.

    Calls ``gh issue view <N> --repo <repo> --json body,comments,title``
    and treats:
      - the issue body as one run entry (dated by issue createdAt fallback today)
      - each comment body as one run entry dated by the comment's createdAt

    All run entries land in ``automations/<automation_id>/memory.md`` so the
    standard merge_automation_tree pipeline handles deduplication.

    ``gh_runner`` is injected so tests can stub the subprocess call.
    """
    import json as _json
    import re as _re

    cmd = [
        "gh", "issue", "view", str(issue_number),
        "--repo", repo,
        "--json", "title,body,comments,createdAt",
    ]
    result = gh_runner(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise _GhIssueIngestError(
            f"gh issue view failed: {result.stderr.strip() or 'no stderr'}"
        )
    try:
        payload = _json.loads(result.stdout)
    except _json.JSONDecodeError as exc:
        raise _GhIssueIngestError(f"gh returned non-JSON: {exc}") from exc

    automations_dir = tmpdir / "automations" / automation_id
    automations_dir.mkdir(parents=True)
    (automations_dir / "automation.toml").write_text(
        f'version = 1\n'
        f'id = "{automation_id}"\n'
        f'name = "{automation_id}"\n'
        f'status = "ACTIVE"\n'
        f'kind = "routine-gh-issue"\n'
        f'rrule = ""\n'
        f'cwds = []\n',
        encoding="utf-8",
    )

    def _bullets_from_body(body: str) -> list[str]:
        """Pull bullets out of an issue/comment body.

        Routine prompts that follow the convention emit dashed bullets;
        non-bulleted bodies become one bullet (the whole body, normalized).
        """
        bullets: list[str] = []
        for line in body.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            m = _re.match(r"^[-*]\s+(.*)$", stripped)
            bullets.append(m.group(1) if m else stripped)
        return bullets

    def _date_from_iso(s: str) -> str:
        """Extract YYYY-MM-DD from an ISO timestamp, with today as fallback."""
        m = _re.match(r"^(\d{4}-\d{2}-\d{2})", s or "")
        return m.group(1) if m else datetime.now(timezone.utc).date().isoformat()

    sections: list[tuple[str, list[str]]] = []
    body = str(payload.get("body") or "").strip()
    if body:
        sections.append((_date_from_iso(payload.get("createdAt", "")), _bullets_from_body(body)))
    for comment in payload.get("comments") or []:
        c_body = str(comment.get("body") or "").strip()
        if not c_body:
            continue
        sections.append((_date_from_iso(comment.get("createdAt", "")), _bullets_from_body(c_body)))

    # Group bullets by date so multiple comments on the same day collapse.
    by_date: dict[str, list[str]] = {}
    for date_str, bullets in sections:
        by_date.setdefault(date_str, []).extend(bullets)

    lines: list[str] = []
    for date_str in sorted(by_date.keys()):
        if lines:
            lines.append("")
        lines.append(date_str)
        for bullet in by_date[date_str]:
            lines.append(f"- {bullet}")
    (automations_dir / "memory.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    return tmpdir / "automations"


def _find_automations_root(extracted_dir: Path) -> Path | None:
    """Locate the 'automations/' subtree inside an extracted artifact.

    Workflows can upload either the parent `~/.claude/` or just the
    `automations/` dir; tolerate both. Returns the dir containing
    `<id>/automation.toml` children.
    """
    if extracted_dir.name == "automations" and extracted_dir.is_dir():
        return extracted_dir
    candidate = extracted_dir / "automations"
    if candidate.is_dir():
        return candidate
    # Fallback: walk one level deep
    for child in extracted_dir.iterdir():
        if child.is_dir() and (child / "automations").is_dir():
            return child / "automations"
    # Bottom case: the extracted dir IS the automations root (no wrapping)
    if any(
        (extracted_dir / sub / "automation.toml").is_file()
        for sub in (p.name for p in extracted_dir.iterdir() if p.is_dir())
    ):
        return extracted_dir
    return None


def _handle_claude_code_automations_doctor(args: argparse.Namespace) -> int:
    automations_dir = (
        Path(args.automations_dir)
        if getattr(args, "automations_dir", None)
        else None
    )
    participant = ClaudeCodeAutomationsParticipant(automations_dir=automations_dir)
    health = participant.health_check()
    report = {
        "health": {
            "status": health.status,
            "reason": health.reason,
            "details": health.details,
        },
        "automations_dir": participant.native_path,
    }
    if health.proposed_fix:
        report["health"]["proposed_fix"] = health.proposed_fix
    _write_yaml_if_requested(report, getattr(args, "report_out", None))
    _print_yaml(report)
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bourdon",
        description="Bourdon CLI",
    )
    subparsers = parser.add_subparsers(dest="command")

    prepare_turn_cmd = subparsers.add_parser(
        "prepare-turn",
        help="Return L6 recognition context for a prompt",
    )
    prepare_turn_cmd.add_argument("prompt")
    prepare_turn_cmd.add_argument(
        "--library",
        type=Path,
        default=DEFAULT_LIBRARY_PATH,
        help=f"Path to agent-library (default: {DEFAULT_LIBRARY_PATH})",
    )
    prepare_turn_cmd.add_argument(
        "--access-level",
        choices=("public", "team", "private"),
        default="team",
    )
    prepare_turn_cmd.add_argument("--report-out")
    prepare_turn_cmd.set_defaults(func=_handle_prepare_turn)

    deeper_context_cmd = subparsers.add_parser(
        "deeper-context",
        help="Return post-recognition L2 context for a prompt",
    )
    deeper_context_cmd.add_argument("prompt")
    deeper_context_cmd.add_argument(
        "--access-level",
        choices=("public", "team", "private"),
        default="team",
    )
    deeper_context_cmd.add_argument("--report-out")
    deeper_context_cmd.set_defaults(func=_handle_deeper_context)

    cursor = subparsers.add_parser("cursor", help="Cursor-specific commands")
    cursor_subparsers = cursor.add_subparsers(dest="cursor_command")

    cursor_export_cmd = cursor_subparsers.add_parser(
        "export",
        help="Build a Cursor L5 manifest from native SQLite state",
    )
    cursor_export_cmd.add_argument("--cursor-dir")
    cursor_export_cmd.add_argument("--out")
    cursor_export_cmd.add_argument("--since")
    cursor_export_cmd.add_argument(
        "--access-level",
        choices=("public", "team", "private"),
        default="team",
    )
    cursor_export_cmd.add_argument(
        "--print",
        dest="print_manifest",
        action="store_true",
        help="Print the exported manifest after writing it.",
    )
    cursor_export_cmd.add_argument(
        "--verbose", action="store_true",
        help="Print diagnostics to stderr on failure (normally silent).",
    )
    cursor_export_cmd.set_defaults(func=_handle_cursor_export)

    cursor_doctor_cmd = cursor_subparsers.add_parser(
        "doctor", help="Diagnose Cursor memory sources",
    )
    cursor_doctor_cmd.add_argument("--cursor-dir", help=argparse.SUPPRESS)
    cursor_doctor_cmd.add_argument("--report-out")
    cursor_doctor_cmd.set_defaults(func=_handle_cursor_doctor)

    cursor_compile_turn_cmd = cursor_subparsers.add_parser(
        "compile-turn", help="Compile a turn-scoped Cursor recognition brief",
    )
    cursor_compile_turn_cmd.add_argument(
        "prompt", help="The user prompt to compile recognition for.",
    )
    cursor_compile_turn_cmd.add_argument(
        "--cwd", help="Current working directory for project context.",
    )
    cursor_compile_turn_cmd.add_argument(
        "--access-level", choices=("public", "team", "private"), default="team",
    )
    cursor_compile_turn_cmd.add_argument("--library-path")
    cursor_compile_turn_cmd.add_argument("--max-items", type=int, default=6)
    cursor_compile_turn_cmd.set_defaults(func=_handle_cursor_compile_turn)

    cursor_sync_native_cmd = cursor_subparsers.add_parser(
        "sync-native",
        help="Render federation content into a Cursor-readable markdown file",
    )
    cursor_sync_mode = cursor_sync_native_cmd.add_mutually_exclusive_group()
    cursor_sync_mode.add_argument(
        "--dry-run", action="store_true", default=True,
    )
    cursor_sync_mode.add_argument(
        "--write", action="store_true", default=False,
        help="Write ~/.cursor/memory/bourdon_context.md.",
    )
    cursor_sync_native_cmd.add_argument("--out")
    cursor_sync_native_cmd.add_argument("--cursor-dir", help=argparse.SUPPRESS)
    cursor_sync_native_cmd.add_argument("--max-entities", type=int, default=100)
    cursor_sync_native_cmd.add_argument("--max-sessions", type=int, default=20)
    cursor_sync_native_cmd.add_argument(
        "--access-level", choices=("public", "team", "private"), default="team",
    )
    cursor_sync_native_cmd.add_argument("--library-path")
    cursor_sync_native_cmd.set_defaults(func=_handle_cursor_sync_native)

    cursor_init_cmd = cursor_subparsers.add_parser(
        "init",
        help="Create a starter ~/.cursor/automations/ directory",
    )
    cursor_init_cmd.add_argument("--automations-dir", help=argparse.SUPPRESS)
    cursor_init_cmd.add_argument(
        "--automation-id", default="cursor-cloud-agent",
    )
    cursor_init_cmd.add_argument("--force", action="store_true")
    cursor_init_cmd.set_defaults(func=_handle_cursor_init)

    # ---- cursor automation subcommands -------------------------------------
    cursor_automations = subparsers.add_parser(
        "cursor-automations",
        help="Cursor Cloud Agent automation memory commands",
    )
    cursor_automation_subparsers = cursor_automations.add_subparsers(
        dest="cursor_automations_command",
    )
    cursor_automation_export_cmd = cursor_automation_subparsers.add_parser(
        "export",
        help="Build a Cursor automations L5 manifest",
    )
    cursor_automation_export_cmd.add_argument(
        "--automations-dir", help=argparse.SUPPRESS,
    )
    cursor_automation_export_cmd.add_argument("--out")
    cursor_automation_export_cmd.add_argument("--since")
    cursor_automation_export_cmd.add_argument(
        "--access-level", choices=("public", "team", "private"), default="team",
    )
    cursor_automation_export_cmd.add_argument(
        "--print", dest="print_manifest", action="store_true",
    )
    cursor_automation_export_cmd.add_argument(
        "--verbose", action="store_true",
    )
    cursor_automation_export_cmd.set_defaults(
        func=_handle_cursor_automations_export,
    )
    cursor_automation_doctor_cmd = cursor_automation_subparsers.add_parser(
        "doctor", help="Diagnose local Cursor automation memory coverage",
    )
    cursor_automation_doctor_cmd.add_argument(
        "--automations-dir", help=argparse.SUPPRESS,
    )
    cursor_automation_doctor_cmd.add_argument("--report-out")
    cursor_automation_doctor_cmd.set_defaults(
        func=_handle_cursor_automations_doctor,
    )
    cursor_automation_ingest_cmd = cursor_automation_subparsers.add_parser(
        "ingest",
        help="Ingest an automations/ tree into the local Cursor automations",
    )
    cursor_automation_ingest_cmd.add_argument("--source")
    cursor_automation_ingest_cmd.add_argument("--artifact-zip")
    cursor_automation_ingest_cmd.add_argument("--dest")
    cursor_automation_ingest_cmd.add_argument(
        "--default-kind", default="cursor-cloud-agent",
    )
    cursor_automation_ingest_cmd.set_defaults(
        func=_handle_cursor_automations_ingest,
    )

    # ---- codex automation subcommands --------------------------------------
    codex_automations = subparsers.add_parser(
        "codex-automations",
        help="Codex automation memory commands",
    )
    codex_automation_subparsers = codex_automations.add_subparsers(
        dest="codex_automations_command"
    )

    codex_automation_export_cmd = codex_automation_subparsers.add_parser(
        "export",
        help="Build a Codex automations L5 manifest from local automation memory",
    )
    codex_automation_export_cmd.add_argument("--automations-dir", help=argparse.SUPPRESS)
    codex_automation_export_cmd.add_argument("--out")
    codex_automation_export_cmd.add_argument("--since")
    codex_automation_export_cmd.add_argument(
        "--access-level",
        choices=("public", "team", "private"),
        default="team",
    )
    codex_automation_export_cmd.add_argument(
        "--print",
        dest="print_manifest",
        action="store_true",
        help="Print the exported manifest after writing it.",
    )
    codex_automation_export_cmd.set_defaults(func=_handle_codex_automations_export)

    codex_automation_doctor_cmd = codex_automation_subparsers.add_parser(
        "doctor",
        help="Diagnose local Codex automation memory coverage",
    )
    codex_automation_doctor_cmd.add_argument("--automations-dir", help=argparse.SUPPRESS)
    codex_automation_doctor_cmd.add_argument("--report-out")
    codex_automation_doctor_cmd.set_defaults(func=_handle_codex_automations_doctor)

    # ---- copilot subcommands ------------------------------------------------
    copilot = subparsers.add_parser("copilot", help="GitHub Copilot-specific commands")
    copilot_subparsers = copilot.add_subparsers(dest="copilot_command")

    copilot_export_cmd = copilot_subparsers.add_parser(
        "export",
        help="Build a Copilot L5 manifest from ~/.copilot-bourdon/memory.md",
    )
    copilot_export_cmd.add_argument("--copilot-dir", help=argparse.SUPPRESS)
    copilot_export_cmd.add_argument("--out")
    copilot_export_cmd.add_argument("--since")
    copilot_export_cmd.add_argument(
        "--access-level",
        choices=("public", "team", "private"),
        default="team",
    )
    copilot_export_cmd.add_argument(
        "--print",
        dest="print_manifest",
        action="store_true",
        help="Print the exported manifest after writing it.",
    )
    copilot_export_cmd.set_defaults(func=_handle_copilot_export)

    copilot_doctor_cmd = copilot_subparsers.add_parser(
        "doctor",
        help="Diagnose the Copilot convention memory file",
    )
    copilot_doctor_cmd.add_argument("--copilot-dir", help=argparse.SUPPRESS)
    copilot_doctor_cmd.add_argument("--report-out")
    copilot_doctor_cmd.set_defaults(func=_handle_copilot_doctor)

    copilot_init_cmd = copilot_subparsers.add_parser(
        "init",
        help="Create ~/.copilot-bourdon/memory.md with a starter template",
    )
    copilot_init_cmd.add_argument("--copilot-dir", help=argparse.SUPPRESS)
    copilot_init_cmd.add_argument(
        "--force",
        action="store_true",
        help="Overwrite an existing memory.md.",
    )
    copilot_init_cmd.set_defaults(func=_handle_copilot_init)

    # ---- cascade subcommands ------------------------------------------------
    cascade = subparsers.add_parser("cascade", help="Cascade (Windsurf)-specific commands")
    cascade_subparsers = cascade.add_subparsers(dest="cascade_command")

    cascade_export_cmd = cascade_subparsers.add_parser(
        "export",
        help="Build a Cascade L5 manifest from ~/.cascade-bourdon/memory.md",
    )
    cascade_export_cmd.add_argument("--cascade-dir", help=argparse.SUPPRESS)
    cascade_export_cmd.add_argument("--out")
    cascade_export_cmd.add_argument("--since")
    cascade_export_cmd.add_argument(
        "--access-level",
        choices=("public", "team", "private"),
        default="team",
    )
    cascade_export_cmd.add_argument(
        "--print",
        dest="print_manifest",
        action="store_true",
        help="Print the exported manifest after writing it.",
    )
    cascade_export_cmd.set_defaults(func=_handle_cascade_export)

    cascade_doctor_cmd = cascade_subparsers.add_parser(
        "doctor",
        help="Diagnose the Cascade convention memory file",
    )
    cascade_doctor_cmd.add_argument("--cascade-dir", help=argparse.SUPPRESS)
    cascade_doctor_cmd.add_argument("--report-out")
    cascade_doctor_cmd.set_defaults(func=_handle_cascade_doctor)

    cascade_init_cmd = cascade_subparsers.add_parser(
        "init",
        help="Create ~/.cascade-bourdon/memory.md with a starter template",
    )
    cascade_init_cmd.add_argument("--cascade-dir", help=argparse.SUPPRESS)
    cascade_init_cmd.add_argument(
        "--force",
        action="store_true",
        help="Overwrite an existing memory.md.",
    )
    cascade_init_cmd.set_defaults(func=_handle_cascade_init)

    # ---- top-level doctor / export-all --------------------------------------
    doctor_cmd = subparsers.add_parser(
        "doctor",
        help="Run health checks across all installed participants",
    )
    doctor_cmd.add_argument("--report-out")
    doctor_cmd.set_defaults(func=_handle_doctor)

    export_all_cmd = subparsers.add_parser(
        "export-all",
        help="Export L5 manifests for all healthy participants",
    )
    export_all_cmd.add_argument("--since")
    export_all_cmd.add_argument(
        "--access-level",
        choices=("public", "team", "private"),
        default="team",
    )
    export_all_cmd.add_argument(
        "--library",
        type=Path,
        default=DEFAULT_LIBRARY_PATH,
        help=f"Path to agent-library (default: {DEFAULT_LIBRARY_PATH})",
    )
    export_all_cmd.add_argument("--report-out")
    export_all_cmd.set_defaults(func=_handle_export_all)

    agents_cmd = subparsers.add_parser(
        "agents",
        help=(
            "Enumerate L5 manifests as a redacted, source-attributed JSON "
            "object (read foundation for the desktop tray). With --federated, "
            "merge this machine's agents with configured peers'."
        ),
    )
    agents_cmd.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON (the stable tray contract; currently the default).",
    )
    agents_cmd.add_argument(
        "--federated",
        action="store_true",
        help=(
            "Also fan out to configured L6 peers and merge their agents in, "
            "each re-tagged with the peer's machine name."
        ),
    )
    agents_cmd.add_argument("--agents-dir", help=argparse.SUPPRESS)
    agents_cmd.add_argument("--peers-config", help=argparse.SUPPRESS)
    agents_cmd.set_defaults(func=_handle_agents)

    dogfood_cmd = subparsers.add_parser(
        "dogfood",
        help=(
            "End-to-end federation smoke test: plant marker in convention-file "
            "participants, export all, query L6, verify round-trip"
        ),
    )
    dogfood_cmd.add_argument(
        "--keep-marker",
        action="store_true",
        help="Leave the planted marker entity in place after the run (debug aid)",
    )
    dogfood_cmd.add_argument(
        "--access-level",
        choices=("public", "team", "private"),
        default="team",
        help="L6 query access level (default team)",
    )
    dogfood_cmd.add_argument(
        "--report-out",
        help="Write the machine-readable YAML report to this path as well as stdout",
    )
    dogfood_cmd.set_defaults(func=_handle_dogfood)

    serve_cmd = subparsers.add_parser(
        "serve",
        help="Launch the L6 federation MCP server (stdio by default)",
    )
    serve_cmd.add_argument(
        "--library",
        type=Path,
        default=None,
        help=f"Path to agent-library (default: {DEFAULT_LIBRARY_PATH})",
    )
    serve_cmd.add_argument(
        "--transport",
        choices=("stdio", "http"),
        default="stdio",
        help="MCP transport (default: stdio -- the MCP default)",
    )
    serve_cmd.add_argument(
        "--port",
        type=int,
        default=7500,
        help="Port for HTTP transport (ignored for stdio, default: 7500)",
    )
    serve_cmd.add_argument(
        "--host",
        default="0.0.0.0",
        help=(
            "Bind host for HTTP transport (default: 0.0.0.0 — all interfaces, "
            "required for cross-host / Tailnet federation; use 127.0.0.1 for "
            "localhost only)."
        ),
    )
    serve_cmd.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress the onboarding banner",
    )
    serve_cmd.add_argument(
        "--peer",
        action="append",
        default=[],
        metavar="URL",
        help=(
            "Peer L6 server URL to federate with (e.g. http://pc.tailnet:7500). "
            "Repeatable. Merged with peers from --peers-config. Cross-machine "
            "federation requires the bourdon[federation] extra."
        ),
    )
    serve_cmd.add_argument(
        "--peers-config",
        type=Path,
        default=None,
        help=(
            "Path to a YAML file listing peer L6 servers (entries: name, url, "
            "token_env). Loaded if present; defaults to ~/.bourdon/peers.yaml. "
            "See config/peers.example.yaml."
        ),
    )
    serve_cmd.add_argument(
        "--allow-unauthenticated",
        action="store_true",
        help=(
            "Serve HTTP transport without Bearer-token auth. Off by default "
            "(authenticated HTTP requires BOURDON_PEER_TOKEN_SERVER). Only safe "
            "on localhost / a closed Tailnet."
        ),
    )
    serve_cmd.set_defaults(func=_handle_serve)

    codex = subparsers.add_parser("codex", help="Codex-specific commands")
    codex_subparsers = codex.add_subparsers(dest="codex_command")

    export_cmd = codex_subparsers.add_parser(
        "export", help="Build a Codex L5 manifest"
    )
    export_cmd.add_argument("--since")
    export_cmd.add_argument("--out")
    export_cmd.add_argument(
        "--access-level",
        choices=("public", "team", "private"),
        default="team",
    )
    export_cmd.add_argument("--codex-home", help=argparse.SUPPRESS)
    export_cmd.add_argument("--codex-brain", help=argparse.SUPPRESS)
    export_cmd.set_defaults(func=_handle_codex_export)

    build_context_cmd = codex_subparsers.add_parser(
        "build-context", help="Generate Codex L0/L1 artifacts"
    )
    build_context_cmd.add_argument("--out-dir", required=True)
    build_context_cmd.add_argument("--since")
    build_context_cmd.add_argument("--codex-home", help=argparse.SUPPRESS)
    build_context_cmd.add_argument("--codex-brain", help=argparse.SUPPRESS)
    build_context_cmd.set_defaults(func=_handle_codex_build_context)

    doctor_cmd = codex_subparsers.add_parser(
        "doctor", help="Diagnose Codex memory sources"
    )
    doctor_cmd.add_argument("--report-out")
    doctor_cmd.add_argument("--codex-home", help=argparse.SUPPRESS)
    doctor_cmd.add_argument("--codex-brain", help=argparse.SUPPRESS)
    doctor_cmd.set_defaults(func=_handle_codex_doctor)

    sync_native_cmd = codex_subparsers.add_parser(
        "sync-native",
        help="Render Bourdon fallback recall into a Codex-native memory file",
    )
    sync_mode = sync_native_cmd.add_mutually_exclusive_group()
    sync_mode.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview the native memory file without writing. This is the default.",
    )
    sync_mode.add_argument(
        "--write",
        action="store_true",
        help="Write ~/.codex/memories/bourdon_fallback.md.",
    )
    sync_native_cmd.add_argument("--out")
    sync_native_cmd.add_argument("--max-sessions", type=int, default=20)
    sync_native_cmd.add_argument(
        "--memory-md",
        action="store_true",
        help=(
            "Update a bounded Bourdon section in ~/.codex/memories/MEMORY.md "
            "instead of writing the standalone Bourdon file."
        ),
    )
    sync_native_cmd.add_argument(
        "--from-library",
        action="store_true",
        help=(
            "Source content from the federation library "
            "(~/agent-library/agents/*.l5.yaml) instead of local Codex history. "
            "Required to render anchors on a fresh machine where Codex has no "
            "local sessions yet."
        ),
    )
    sync_native_cmd.add_argument(
        "--include-local",
        action="store_true",
        help=(
            "When combined with --from-library, append local Codex history "
            "as a trailing section. Ignored without --from-library."
        ),
    )
    sync_native_cmd.add_argument(
        "--access-level",
        choices=("public", "team", "private"),
        default="team",
        help="Visibility filter applied to federation entities (default: team).",
    )
    sync_native_cmd.add_argument(
        "--library-path",
        help="Override the agent-library root (default: ~/agent-library).",
    )
    sync_native_cmd.add_argument("--codex-home", help=argparse.SUPPRESS)
    sync_native_cmd.add_argument("--codex-brain", help=argparse.SUPPRESS)
    sync_native_cmd.set_defaults(func=_handle_codex_sync_native)

    recognize_cmd = codex_subparsers.add_parser(
        "recognize",
        help="Run the Codex recognition layer for one prompt",
    )
    recognize_cmd.add_argument("prompt")
    recognize_cmd.add_argument("--since")
    recognize_cmd.add_argument(
        "--access-level",
        choices=("public", "team", "private"),
        default="team",
    )
    recognize_cmd.add_argument("--report-out")
    recognize_cmd.add_argument(
        "--prompt-context",
        action="store_true",
        help="Include a bounded prompt fragment built from matched entities.",
    )
    recognize_cmd.add_argument("--codex-home", help=argparse.SUPPRESS)
    recognize_cmd.add_argument("--codex-brain", help=argparse.SUPPRESS)
    recognize_cmd.set_defaults(func=_handle_codex_recognize)

    prepare_turn_cmd = codex_subparsers.add_parser(
        "prepare-turn",
        help="Refresh Codex memory surfaces and return recognition context",
    )
    prepare_turn_cmd.add_argument("prompt")
    prepare_turn_cmd.add_argument(
        "--write",
        action="store_true",
        help="Write the native memory bridge and Codex L5 manifest.",
    )
    prepare_turn_cmd.add_argument(
        "--memory-md",
        action="store_true",
        help="Update a bounded Bourdon section in ~/.codex/memories/MEMORY.md.",
    )
    prepare_turn_cmd.add_argument("--native-out")
    prepare_turn_cmd.add_argument("--l5-out")
    prepare_turn_cmd.add_argument("--max-sessions", type=int, default=20)
    prepare_turn_cmd.add_argument(
        "--strategy",
        choices=("legacy", "turn-compiled"),
        default="legacy",
        help="Choose legacy recognition context or the turn-scoped compiler.",
    )
    prepare_turn_cmd.add_argument(
        "--cwd",
        help="Current working directory used by --strategy turn-compiled.",
    )
    prepare_turn_cmd.add_argument(
        "--library-path",
        help=(
            "Override the agent-library root used by --strategy "
            "turn-compiled (default: ~/agent-library)."
        ),
    )
    prepare_turn_cmd.add_argument(
        "--max-items",
        type=int,
        default=6,
        help="Maximum compiled brief items for --strategy turn-compiled.",
    )
    prepare_turn_cmd.add_argument(
        "--max-chars",
        type=int,
        default=1800,
        help="Maximum explicit brief characters for --strategy turn-compiled.",
    )
    prepare_turn_cmd.add_argument("--since")
    prepare_turn_cmd.add_argument(
        "--access-level",
        choices=("public", "team", "private"),
        default="team",
    )
    prepare_turn_cmd.add_argument("--report-out")
    prepare_turn_cmd.add_argument("--codex-home", help=argparse.SUPPRESS)
    prepare_turn_cmd.add_argument("--codex-brain", help=argparse.SUPPRESS)
    prepare_turn_cmd.set_defaults(func=_handle_codex_prepare_turn)

    compile_turn_cmd = codex_subparsers.add_parser(
        "compile-turn",
        help="Compile a turn-scoped Codex recognition brief",
    )
    compile_turn_cmd.add_argument("prompt")
    compile_turn_cmd.add_argument("--cwd")
    compile_turn_cmd.add_argument(
        "--library-path",
        help="Override the agent-library root (default: ~/agent-library).",
    )
    compile_turn_cmd.add_argument("--codex-home", help=argparse.SUPPRESS)
    compile_turn_cmd.add_argument(
        "--access-level",
        choices=("public", "team", "private"),
        default="team",
    )
    compile_turn_cmd.add_argument("--max-items", type=int, default=6)
    compile_turn_cmd.add_argument("--max-chars", type=int, default=1800)
    compile_turn_cmd.add_argument(
        "--format",
        choices=("yaml", "json"),
        default="yaml",
    )
    compile_turn_cmd.add_argument(
        "--delivery",
        choices=("explicit", "mcp", "memory-md", "fallback", "all"),
        default="all",
    )
    compile_turn_cmd.add_argument("--report-out")
    compile_turn_cmd.set_defaults(func=_handle_codex_compile_turn)

    eval_cmd = codex_subparsers.add_parser("eval", help="Evaluate Codex sources")
    eval_mode = eval_cmd.add_mutually_exclusive_group()
    eval_mode.add_argument("--fixtures", action="store_true")
    eval_mode.add_argument("--live", action="store_true")
    eval_cmd.add_argument("--since")
    eval_cmd.add_argument(
        "--access-level",
        choices=("public", "team", "private"),
        default="team",
    )
    eval_cmd.add_argument("--report-out")
    eval_cmd.add_argument("--codex-home", help=argparse.SUPPRESS)
    eval_cmd.add_argument("--codex-brain", help=argparse.SUPPRESS)
    eval_cmd.add_argument(
        "--recognition",
        action="store_true",
        help=(
            "Also exercise core.recognition_runtime against canonical prompts "
            "and attach a behavior-layer report (recognition latency, "
            "hydration latency, hit rate)."
        ),
    )
    eval_cmd.add_argument(
        "--turn-compiler",
        action="store_true",
        help=(
            "Also exercise the Codex turn-scoped compiler against canonical "
            "prompts and attach routing, confidence, and latency metrics."
        ),
    )
    eval_cmd.add_argument("--cwd")
    eval_cmd.add_argument(
        "--library-path",
        help=(
            "Agent-library root for --turn-compiler live mode. Fixtures and "
            "live runs without this flag use a temp L5 export."
        ),
    )
    eval_cmd.add_argument("--max-items", type=int, default=6)
    eval_cmd.add_argument("--max-chars", type=int, default=1800)
    eval_cmd.set_defaults(func=_handle_codex_eval)

    # ---- claude-code subcommands --------------------------------------------
    cc = subparsers.add_parser(
        "claude-code", help="Claude Code-specific commands"
    )
    cc_subparsers = cc.add_subparsers(dest="cc_command")

    cc_export_cmd = cc_subparsers.add_parser(
        "export",
        help=(
            "Build a Claude Code L5 manifest and write it to ~/agent-library/. "
            "Silent + never raises; designed for SessionEnd hook use."
        ),
    )
    cc_export_cmd.add_argument(
        "--since",
        help="Filter sessions newer than this ISO 8601 date / datetime.",
    )
    cc_export_cmd.add_argument(
        "--out",
        help=(
            "Output YAML path. Default: ~/agent-library/agents/claude-code.l5.yaml"
        ),
    )
    cc_export_cmd.add_argument(
        "--access-level",
        choices=("public", "team", "private"),
        default="team",
    )
    cc_export_cmd.add_argument(
        "--print",
        dest="print_manifest",
        action="store_true",
        help="Also print the filtered manifest to stdout (default: silent).",
    )
    cc_export_cmd.add_argument(
        "--verbose",
        action="store_true",
        help="Log progress + errors to stderr (default: silent).",
    )
    cc_export_cmd.set_defaults(func=_handle_claude_code_export)

    # ---- claude-code-automations subcommands -------------------------------
    cca = subparsers.add_parser(
        "claude-code-automations",
        help="Claude Code automation memory commands",
    )
    cca_subparsers = cca.add_subparsers(dest="cca_command")

    cca_export_cmd = cca_subparsers.add_parser(
        "export",
        help=(
            "Build a Claude Code automations L5 manifest from local "
            "~/.claude/automations/ memory."
        ),
    )
    cca_export_cmd.add_argument("--automations-dir", help=argparse.SUPPRESS)
    cca_export_cmd.add_argument("--out")
    cca_export_cmd.add_argument("--since")
    cca_export_cmd.add_argument(
        "--access-level",
        choices=("public", "team", "private"),
        default="team",
    )
    cca_export_cmd.add_argument(
        "--print",
        dest="print_manifest",
        action="store_true",
        help="Print the exported manifest after writing it.",
    )
    cca_export_cmd.add_argument(
        "--verbose",
        action="store_true",
        help="Log progress + errors to stderr (default: silent).",
    )
    cca_export_cmd.set_defaults(func=_handle_claude_code_automations_export)

    cca_doctor_cmd = cca_subparsers.add_parser(
        "doctor",
        help="Diagnose local Claude Code automation memory coverage",
    )
    cca_doctor_cmd.add_argument("--automations-dir", help=argparse.SUPPRESS)
    cca_doctor_cmd.add_argument("--report-out")
    cca_doctor_cmd.set_defaults(func=_handle_claude_code_automations_doctor)

    cca_ingest_cmd = cca_subparsers.add_parser(
        "ingest-github",
        help=(
            "Ingest an automations/ tree produced by a claude-code-action "
            "GitHub Actions run into the local ~/.claude/automations/."
        ),
    )
    cca_ingest_cmd.add_argument(
        "--source",
        help="Local automations/ directory (already downloaded).",
    )
    cca_ingest_cmd.add_argument(
        "--artifact-zip",
        help="Path to a workflow artifact zip to extract.",
    )
    cca_ingest_cmd.add_argument(
        "--repo",
        help="GitHub repo (owner/name) to pull from via 'gh run download'.",
    )
    cca_ingest_cmd.add_argument(
        "--run",
        help="Workflow run id (used with --repo).",
    )
    cca_ingest_cmd.add_argument(
        "--artifact-name",
        default="claude-code-automations",
        help="Artifact name uploaded by the workflow (default: %(default)s).",
    )
    cca_ingest_cmd.add_argument(
        "--dest",
        help="Destination automations directory (default: ~/.claude/automations).",
    )
    cca_ingest_cmd.add_argument(
        "--default-kind",
        default="github-action",
        help="kind= value for newly created automation.toml stubs (default: %(default)s).",
    )
    cca_ingest_cmd.add_argument(
        "--gh-issue",
        help=(
            "GitHub issue to pull routine self-report comments from "
            "(format: owner/repo#NUMBER). Requires --automation-id."
        ),
    )
    cca_ingest_cmd.add_argument(
        "--automation-id",
        help=(
            "Automation id to attribute the ingested entries to. "
            "Required with --gh-issue."
        ),
    )
    cca_ingest_cmd.set_defaults(func=_handle_claude_code_automations_ingest)

    # ---- claude-desktop-cowork subcommands ---------------------------------
    cdcw = subparsers.add_parser(
        "claude-desktop-cowork",
        help="Claude desktop app Co-Work / local-agent memory commands",
    )
    cdcw_subparsers = cdcw.add_subparsers(dest="cdcw_command")

    cdcw_export_cmd = cdcw_subparsers.add_parser(
        "export",
        help=(
            "Build a Claude Desktop Co-Work L5 manifest from local "
            "local-agent-mode-sessions/ state (metadata only -- no content)."
        ),
    )
    cdcw_export_cmd.add_argument("--store-dir", help=argparse.SUPPRESS)
    cdcw_export_cmd.add_argument("--out")
    cdcw_export_cmd.add_argument("--since")
    cdcw_export_cmd.add_argument(
        "--access-level",
        choices=("public", "team", "private"),
        default="team",
    )
    cdcw_export_cmd.add_argument(
        "--print",
        dest="print_manifest",
        action="store_true",
        help="Print the exported manifest after writing it.",
    )
    cdcw_export_cmd.add_argument(
        "--verbose",
        action="store_true",
        help="Log progress + errors to stderr (default: silent).",
    )
    cdcw_export_cmd.set_defaults(func=_handle_claude_desktop_cowork_export)

    # ---- claude-desktop-code subcommands -----------------------------------
    cdco = subparsers.add_parser(
        "claude-desktop-code",
        help="Claude desktop app GUI Claude Code memory commands",
    )
    cdco_subparsers = cdco.add_subparsers(dest="cdco_command")

    cdco_export_cmd = cdco_subparsers.add_parser(
        "export",
        help=(
            "Build a Claude Desktop Code L5 manifest from local "
            "claude-code-sessions/ state (metadata only -- no content)."
        ),
    )
    cdco_export_cmd.add_argument("--store-dir", help=argparse.SUPPRESS)
    cdco_export_cmd.add_argument("--out")
    cdco_export_cmd.add_argument("--since")
    cdco_export_cmd.add_argument(
        "--access-level",
        choices=("public", "team", "private"),
        default="team",
    )
    cdco_export_cmd.add_argument(
        "--print",
        dest="print_manifest",
        action="store_true",
        help="Print the exported manifest after writing it.",
    )
    cdco_export_cmd.add_argument(
        "--verbose",
        action="store_true",
        help="Log progress + errors to stderr (default: silent).",
    )
    cdco_export_cmd.set_defaults(func=_handle_claude_desktop_code_export)

    # ---- benchmark ---------------------------------------------------------
    benchmark_cmd = subparsers.add_parser(
        "benchmark",
        help="Bourdon benchmarks (Phase 1.5+)",
    )
    benchmark_subparsers = benchmark_cmd.add_subparsers(dest="benchmark_command")
    latency_cmd = benchmark_subparsers.add_parser(
        "latency",
        help=(
            "Run the first-turn recognition latency harness and append a row "
            "to BENCHMARKS/latency_matrix.md. See BENCHMARKS/methodology.md."
        ),
    )
    latency_cmd.add_argument(
        "harness_args",
        nargs=argparse.REMAINDER,
        help="Arguments forwarded to scripts/latency_harness.py.",
    )
    latency_cmd.set_defaults(func=_handle_benchmark_latency)

    # ---- setup wizard ------------------------------------------------------
    from cli.setup import add_setup_parser
    add_setup_parser(subparsers)

    # ---- demo walkthrough --------------------------------------------------
    from cli.demo import add_demo_parser
    add_demo_parser(subparsers)

    # ---- sync (#74) --------------------------------------------------------
    sync_cmd = subparsers.add_parser(
        "sync",
        help="Push/pull the agent-library across machines via rsync.",
    )
    sync_subparsers = sync_cmd.add_subparsers(dest="sync_command")

    sync_push_cmd = sync_subparsers.add_parser(
        "push",
        help=(
            "Push the local agent-library to <dest>, filtered by visibility. "
            "Default access-level is `public` -- pushing team/private manifests "
            "is opt-in."
        ),
    )
    sync_push_cmd.add_argument(
        "dest",
        help=(
            "rsync destination: local path, user@host:path, or rsync:// URL. "
            "Trailing slash semantics follow rsync."
        ),
    )
    sync_push_cmd.add_argument(
        "--access-level",
        choices=("public", "team", "private"),
        default="public",
        help="Maximum visibility to push. Default: public.",
    )
    sync_push_cmd.add_argument(
        "--library-path",
        help=f"Source library (default: {DEFAULT_LIBRARY_PATH}).",
    )
    sync_push_cmd.add_argument(
        "--dry-run",
        action="store_true",
        help="Stage + show rsync plan without writing to dest.",
    )
    sync_push_cmd.add_argument(
        "--delete",
        action="store_true",
        help="Mirror mode -- delete files on dest that don't exist in source.",
    )
    sync_push_cmd.add_argument(
        "--verbose",
        action="store_true",
        help="Pass rsync -v.",
    )
    sync_push_cmd.set_defaults(func=_handle_sync_push)

    sync_pull_cmd = sync_subparsers.add_parser(
        "pull",
        help="Pull a remote agent-library into the local one.",
    )
    sync_pull_cmd.add_argument(
        "src",
        help="rsync source: local path, user@host:path, or rsync:// URL.",
    )
    sync_pull_cmd.add_argument(
        "--library-path",
        help=f"Destination library (default: {DEFAULT_LIBRARY_PATH}).",
    )
    sync_pull_cmd.add_argument(
        "--dry-run",
        action="store_true",
        help="Show rsync plan without writing to local library.",
    )
    sync_pull_cmd.add_argument(
        "--delete",
        action="store_true",
        help="Mirror mode -- delete local files not in source.",
    )
    sync_pull_cmd.add_argument(
        "--verbose",
        action="store_true",
        help="Pass rsync -v.",
    )
    sync_pull_cmd.set_defaults(func=_handle_sync_pull)

    return parser


def _handle_benchmark_latency(args: argparse.Namespace) -> int:
    """Run the Phase 1.5 latency harness in-process.

    Forwards `harness_args` (anything after `bourdon benchmark latency --`) to
    `scripts/latency_harness.py:main`. Lives in cli/main.py only as a convenience
    surface; the harness is also runnable directly as a script.
    """
    import importlib.util
    from pathlib import Path

    harness_path = Path(__file__).resolve().parent.parent / "scripts" / "latency_harness.py"
    if not harness_path.exists():
        print(f"latency harness missing at {harness_path}", file=sys.stderr)
        return 1
    spec = importlib.util.spec_from_file_location("latency_harness", harness_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    # Register before exec so dataclasses' module-resolution sees us.
    sys.modules["latency_harness"] = module
    spec.loader.exec_module(module)
    forwarded = getattr(args, "harness_args", None) or []
    # argparse.REMAINDER on a subcommand sometimes keeps the leading "--"; drop it.
    if forwarded and forwarded[0] == "--":
        forwarded = forwarded[1:]
    return int(module.main(forwarded))


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if not hasattr(args, "func"):
        parser.print_help()
        return 1
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
