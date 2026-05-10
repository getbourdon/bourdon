"""Top-level `bourdon` CLI."""

from __future__ import annotations

import argparse
import asyncio
import sys
import tempfile
import time as _time
from collections import Counter
from datetime import date, datetime, time
from pathlib import Path
from typing import Any

import yaml

from adapters.base import AdapterDiscoveryError
from adapters.claude_code import ClaudeCodeAdapter
from adapters.codex import (
    CodexAdapter,
    _build_codex_native_memory_payload,
    _default_codex_memory_md_path,
    _default_codex_native_memory_path,
    _inspect_codex_fallback_recall,
    _inspect_codex_state_db,
    _merge_bourdon_memory_md_section,
    _safe_native_memory_text,
)
from adapters.copilot import (
    CopilotAdapter,
    _inspect_copilot_memory,
    default_copilot_memory_path,
    init_memory_file,
)
from adapters.cursor import CursorAdapter
from core.codex_context import filter_manifest_for_access, write_codex_context_artifacts
from core.codex_fixtures import create_sample_codex_sources
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


def _default_cursor_l5_path() -> Path:
    return Path.home() / "agent-library" / "agents" / "cursor.l5.yaml"


def _default_copilot_l5_path() -> Path:
    return Path.home() / "agent-library" / "agents" / "copilot.l5.yaml"


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


def _build_adapter(args: argparse.Namespace) -> CodexAdapter:
    codex_home = Path(args.codex_home) if getattr(args, "codex_home", None) else None
    codex_brain = (
        Path(args.codex_brain) if getattr(args, "codex_brain", None) else None
    )
    return CodexAdapter(codex_home=codex_home, codex_brain=codex_brain)


def _manifest_for_access(
    adapter: CodexAdapter, since: datetime | None, access_level: str
) -> dict[str, Any]:
    manifest = adapter.export_l5(since=since)
    return filter_manifest_for_access(manifest, access_level=access_level)


def _handle_codex_export(args: argparse.Namespace) -> int:
    adapter = _build_adapter(args)
    data = _manifest_for_access(
        adapter,
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
    cursor_dir = Path(args.cursor_dir) if args.cursor_dir else None
    adapter = CursorAdapter(cursor_dir=cursor_dir)
    manifest = adapter.export_l5(since=_parse_since(args.since))
    data = filter_manifest_for_access(manifest, access_level=args.access_level)
    out_path = Path(args.out) if args.out else _default_cursor_l5_path()
    write_l5_dict(data, out_path)
    if args.print_manifest:
        _print_yaml(data)
    return 0


def _handle_copilot_export(args: argparse.Namespace) -> int:
    copilot_dir = Path(args.copilot_dir) if getattr(args, "copilot_dir", None) else None
    adapter = CopilotAdapter(copilot_dir=copilot_dir)
    manifest = adapter.export_l5(since=_parse_since(args.since))
    data = filter_manifest_for_access(manifest, access_level=args.access_level)
    out_path = Path(args.out) if args.out else _default_copilot_l5_path()
    write_l5_dict(data, out_path)
    if args.print_manifest:
        _print_yaml(data)
    return 0


def _handle_copilot_doctor(args: argparse.Namespace) -> int:
    copilot_dir = Path(args.copilot_dir) if getattr(args, "copilot_dir", None) else None
    adapter = CopilotAdapter(copilot_dir=copilot_dir)
    health = adapter.health_check()
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


def _handle_codex_build_context(args: argparse.Namespace) -> int:
    adapter = _build_adapter(args)
    manifest = _manifest_for_access(adapter, since=_parse_since(args.since), access_level="team")
    report = write_codex_context_artifacts(manifest, Path(args.out_dir), access_level="team")
    _print_yaml(report)
    return 0


def _handle_codex_doctor(args: argparse.Namespace) -> int:
    adapter = _build_adapter(args)
    report = {
        "source_coverage": _source_coverage(adapter),
        "codex_state_db": _inspect_codex_state_db(adapter._codex_home),
        "fallback_recall": _inspect_codex_fallback_recall(
            adapter._codex_home,
            adapter._codex_brain,
        ),
    }
    _write_yaml_if_requested(report, args.report_out)
    _print_yaml(report)
    return 0


def _handle_codex_sync_native(args: argparse.Namespace) -> int:
    adapter = _build_adapter(args)
    payload = _build_codex_native_memory_payload(
        adapter._codex_home,
        adapter._codex_brain,
        max_sessions=args.max_sessions,
    )
    target_kind = "memory_md" if args.memory_md else "bourdon_file"
    target = (
        Path(args.out)
        if getattr(args, "out", None)
        else (
            _default_codex_memory_md_path(adapter._codex_home)
            if args.memory_md
            else _default_codex_native_memory_path(adapter._codex_home)
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
    adapter = _build_adapter(args)
    manifest = _manifest_for_access(
        adapter,
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
    adapter = _build_adapter(args)
    access_level = args.access_level
    since = _parse_since(args.since)
    mode = "write" if args.write else "dry-run"

    native_payload = _build_codex_native_memory_payload(
        adapter._codex_home,
        adapter._codex_brain,
        max_sessions=args.max_sessions,
    )
    native_target_kind = "memory_md" if args.memory_md else "bourdon_file"
    native_target = (
        Path(args.native_out)
        if getattr(args, "native_out", None)
        else (
            _default_codex_memory_md_path(adapter._codex_home)
            if args.memory_md
            else _default_codex_native_memory_path(adapter._codex_home)
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
        adapter,
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
    report = {
        "mode": mode,
        "access_level": access_level,
        "recognition": recognition_report,
        "prompt_context": _build_recognition_prompt_context(result),
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
    _write_yaml_if_requested(report, args.report_out)
    _print_yaml(report)
    return 0


def _fixture_adapter() -> CodexAdapter:
    tmpdir = tempfile.TemporaryDirectory()
    sources = create_sample_codex_sources(Path(tmpdir.name) / "home")
    adapter = CodexAdapter(
        codex_home=sources["codex_home"],
        codex_brain=sources["codex_brain"],
    )
    adapter._fixture_tmpdir = tmpdir  # type: ignore[attr-defined]
    return adapter


def _source_coverage(adapter: CodexAdapter) -> dict[str, Any]:
    health = adapter.health_check()
    details = health.details or {}
    return {
        "status": health.status,
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


def _handle_codex_eval(args: argparse.Namespace) -> int:
    adapter = _fixture_adapter() if args.fixtures else _build_adapter(args)
    manifest = _manifest_for_access(
        adapter,
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
        "source_coverage": _source_coverage(adapter),
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
        adapter = ClaudeCodeAdapter()
    except Exception as exc:  # noqa: BLE001 -- hook contract: never raises
        if args.verbose:
            print(
                f"bourdon claude-code export: adapter init failed: {exc}",
                file=sys.stderr,
            )
        return 0

    try:
        manifest = adapter.export_l5(since=_parse_since(args.since))
    except AdapterDiscoveryError as exc:
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
    cursor_export_cmd.set_defaults(func=_handle_cursor_export)

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

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if not hasattr(args, "func"):
        parser.print_help()
        return 1
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
