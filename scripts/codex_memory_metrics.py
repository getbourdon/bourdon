#!/usr/bin/env python3
# ruff: noqa: E501
"""Collect read-only metrics for Codex native memory and Bourdon federation.

The output is a graph-ready JSON/YAML snapshot intended for recurring tracking
and later association/pattern-recognition analysis. The script never reads
``~/.codex/auth.json`` and never mutates Codex SQLite or Bourdon manifests.
"""

from __future__ import annotations

import argparse
import html
import json
import socket
import subprocess
import sys
from collections import Counter
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml

from participants.codex import (
    _default_codex_memory_md_path,
    _default_codex_native_memory_path,
    _inspect_codex_fallback_recall,
    _inspect_codex_state_db,
    _resolve_codex_home,
)
from core.l6_store import DEFAULT_LIBRARY_PATH

SCHEMA_VERSION = "codex-memory-metrics/v1"
BOURDON_MCP_NAME = "bourdon"
DEFAULT_L5_STALENESS_DAYS = 7


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso_from_timestamp(timestamp: float | None) -> str | None:
    if timestamp is None:
        return None
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat()


def _file_snapshot(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {
            "path": str(path),
            "exists": False,
            "bytes": 0,
            "modified_at": None,
        }
    stat = path.stat()
    return {
        "path": str(path),
        "exists": True,
        "bytes": stat.st_size,
        "modified_at": _iso_from_timestamp(stat.st_mtime),
    }


def _directory_snapshot(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {
            "path": str(path),
            "exists": False,
            "count": 0,
            "newest_file": None,
            "newest_modified_at": None,
        }
    files = [entry for entry in path.iterdir() if entry.is_file()]
    newest = max(files, key=lambda entry: entry.stat().st_mtime, default=None)
    return {
        "path": str(path),
        "exists": True,
        "count": len(files),
        "newest_file": str(newest) if newest else None,
        "newest_modified_at": (
            _iso_from_timestamp(newest.stat().st_mtime) if newest else None
        ),
    }


def _memory_files_snapshot(codex_home: Path | None) -> dict[str, Any]:
    if codex_home is None:
        memories_dir = Path.home() / ".codex" / "memories"
    else:
        memories_dir = codex_home / "memories"
    return {
        "memory_md": _file_snapshot(_default_codex_memory_md_path(codex_home)),
        "raw_memories_md": _file_snapshot(memories_dir / "raw_memories.md"),
        "bourdon_fallback_md": _file_snapshot(
            _default_codex_native_memory_path(codex_home)
        ),
        "rollout_summaries": _directory_snapshot(memories_dir / "rollout_summaries"),
    }


def _read_yaml_dict(path: Path) -> dict[str, Any]:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return {}
    return data if isinstance(data, dict) else {}


def _l5_summary(path: Path) -> dict[str, Any]:
    file_info = _file_snapshot(path)
    if not file_info["exists"]:
        return {
            **file_info,
            "agent_id": path.stem,
            "last_updated": None,
            "entity_count": 0,
            "session_count": 0,
        }
    manifest = _read_yaml_dict(path)
    agent = manifest.get("agent") if isinstance(manifest.get("agent"), dict) else {}
    return {
        **file_info,
        "agent_id": str(agent.get("id") or path.stem),
        "agent_type": agent.get("type"),
        "last_updated": manifest.get("last_updated"),
        "entity_count": len(manifest.get("known_entities") or []),
        "session_count": len(manifest.get("recent_sessions") or []),
    }


def _agent_library_snapshot(library_path: Path) -> dict[str, Any]:
    agents_dir = library_path / "agents"
    agent_files = sorted(agents_dir.glob("*.l5.yaml")) if agents_dir.exists() else []
    agents = {path.stem: _l5_summary(path) for path in agent_files}
    codex_l5 = agents.get("codex") or _l5_summary(agents_dir / "codex.l5.yaml")
    return {
        "path": str(library_path),
        "agents_dir": str(agents_dir),
        "agents_dir_exists": agents_dir.exists(),
        "agents": agents,
        "codex_l5": codex_l5,
        "totals": {
            "agent_count": len(agents),
            "entity_count": sum(agent["entity_count"] for agent in agents.values()),
            "session_count": sum(agent["session_count"] for agent in agents.values()),
        },
    }


def _safe_mcp_output(value: str) -> str:
    redacted_lines: list[str] = []
    sensitive_markers = ("token", "secret", "password", "api_key", "apikey")
    for line in value.strip().splitlines():
        lowered = line.lower()
        if any(marker in lowered for marker in sensitive_markers):
            redacted_lines.append("[redacted sensitive MCP line]")
        else:
            redacted_lines.append(line)
    return "\n".join(redacted_lines)


def _run_codex_mcp(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )


def _codex_mcp_snapshot(
    run_codex_mcp: Callable[[list[str]], subprocess.CompletedProcess[str]] | None,
) -> dict[str, Any]:
    command = ["codex", "mcp", "get", BOURDON_MCP_NAME]
    if run_codex_mcp is None:
        return {
            "name": BOURDON_MCP_NAME,
            "checked": False,
            "installed": None,
            "status": "skipped",
            "command": command,
        }
    try:
        result = run_codex_mcp(command)
    except FileNotFoundError:
        return {
            "name": BOURDON_MCP_NAME,
            "checked": True,
            "installed": False,
            "status": "error",
            "command": command,
            "message": "codex CLI not found on PATH.",
        }
    output = "\n".join(part for part in (result.stdout, result.stderr) if part)
    return {
        "name": BOURDON_MCP_NAME,
        "checked": True,
        "installed": result.returncode == 0,
        "status": "installed" if result.returncode == 0 else "missing_or_error",
        "command": command,
        "returncode": result.returncode,
        "output": _safe_mcp_output(output),
    }


def _classify_stage1_errors(state_db_report: dict[str, Any]) -> dict[str, int]:
    counter: Counter[str] = Counter()
    jobs = state_db_report.get("memory_stage1_jobs") or {}
    for error in jobs.get("errors") or []:
        text = str(error.get("last_error") or "").lower()
        if "usage limit" in text:
            counter["usage_limit"] += 1
        elif "context window" in text or "ran out of room" in text:
            counter["context_window"] += 1
        elif text:
            counter["other"] += 1
        else:
            counter["unknown"] += 1
    return dict(sorted(counter.items()))


def _ratio(numerator: int, denominator: int) -> float | None:
    if denominator <= 0:
        return None
    return numerator / denominator


def _derived_metrics(
    state_db_report: dict[str, Any],
    fallback_recall: dict[str, Any],
    memory_files: dict[str, Any],
    agent_library: dict[str, Any],
) -> dict[str, Any]:
    schema = state_db_report.get("schema") or {}
    stage1_outputs = state_db_report.get("stage1_outputs") or {}
    jobs = state_db_report.get("memory_stage1_jobs") or {}
    by_status = jobs.get("by_status") or {}
    agent_jobs = state_db_report.get("agent_jobs") or {}
    total_jobs = int(jobs.get("total") or 0)
    done_jobs = int(by_status.get("done") or 0)
    error_jobs = int(by_status.get("error") or 0)
    codex_l5 = agent_library.get("codex_l5") or {}
    distilled_memory_items = int(fallback_recall.get("distilled_memory_items") or 0)
    return {
        "state_schema_variant": schema.get("variant"),
        "stage1_counters_available": bool(schema.get("stage1_counters_available")),
        "stage1_outputs_total": int(stage1_outputs.get("total") or 0),
        "stage1_jobs_total": total_jobs,
        "stage1_jobs_done": done_jobs,
        "stage1_jobs_error": error_jobs,
        "stage1_success_ratio": _ratio(done_jobs, total_jobs),
        "stage1_failure_ratio": _ratio(error_jobs, total_jobs),
        "stage1_error_classes": _classify_stage1_errors(state_db_report),
        "agent_jobs_total": int(agent_jobs.get("total") or 0),
        "agent_job_items_total": int(agent_jobs.get("items_total") or 0),
        "agent_jobs_by_status": agent_jobs.get("by_status") or {},
        "agent_job_items_by_status": agent_jobs.get("items_by_status") or {},
        "distilled_memory_items": distilled_memory_items,
        "fallback_memory_items": int(fallback_recall.get("fallback_memory_items") or 0),
        "session_records": int(fallback_recall.get("session_records") or 0),
        "rollout_records": int(fallback_recall.get("rollout_records") or 0),
        "fallback_recall_active": bool(fallback_recall.get("active")),
        "fallback_recall_reason": fallback_recall.get("reason"),
        "raw_memories_bytes": memory_files["raw_memories_md"]["bytes"],
        "memory_md_bytes": memory_files["memory_md"]["bytes"],
        "bourdon_fallback_bytes": memory_files["bourdon_fallback_md"]["bytes"],
        "rollout_summary_count": memory_files["rollout_summaries"]["count"],
        "codex_l5_entity_count": int(codex_l5.get("entity_count") or 0),
        "codex_l5_session_count": int(codex_l5.get("session_count") or 0),
        "codex_l5_last_updated": codex_l5.get("last_updated"),
        "native_memory_present": distilled_memory_items > 0,
    }


def _trend(current: dict[str, Any], previous: dict[str, Any] | None) -> dict[str, Any]:
    if not previous:
        return {"available": False}
    current_derived = current.get("derived") or {}
    previous_derived = previous.get("derived") or {}
    current_files = current.get("memory_files") or {}
    previous_files = previous.get("memory_files") or {}

    def delta(key: str) -> int | float | None:
        current_value = current_derived.get(key)
        previous_value = previous_derived.get(key)
        if isinstance(current_value, (int, float)) and isinstance(
            previous_value,
            (int, float),
        ):
            return current_value - previous_value
        return None

    raw_current = (current_files.get("raw_memories_md") or {}).get("bytes")
    raw_previous = (previous_files.get("raw_memories_md") or {}).get("bytes")
    raw_delta = (
        raw_current - raw_previous
        if isinstance(raw_current, int) and isinstance(raw_previous, int)
        else None
    )
    return {
        "available": True,
        "stage1_outputs_total_delta": delta("stage1_outputs_total"),
        "stage1_jobs_done_delta": delta("stage1_jobs_done"),
        "stage1_jobs_error_delta": delta("stage1_jobs_error"),
        "agent_jobs_total_delta": delta("agent_jobs_total"),
        "agent_job_items_total_delta": delta("agent_job_items_total"),
        "distilled_memory_items_delta": delta("distilled_memory_items"),
        "fallback_memory_items_delta": delta("fallback_memory_items"),
        "session_records_delta": delta("session_records"),
        "rollout_records_delta": delta("rollout_records"),
        "codex_l5_entity_count_delta": delta("codex_l5_entity_count"),
        "codex_l5_session_count_delta": delta("codex_l5_session_count"),
        "raw_memories_bytes_delta": raw_delta,
    }


def _graph(snapshot: dict[str, Any]) -> dict[str, Any]:
    derived = snapshot["derived"]
    return {
        "nodes": [
            {
                "id": "codex.native.stage1",
                "type": "memory_pipeline",
                "metrics": {
                    "available": derived["stage1_counters_available"],
                    "outputs": derived["stage1_outputs_total"],
                    "jobs_done": derived["stage1_jobs_done"],
                    "jobs_error": derived["stage1_jobs_error"],
                    "failure_ratio": derived["stage1_failure_ratio"],
                },
            },
            {
                "id": "codex.native.agent_jobs",
                "type": "native_job_schema",
                "metrics": {
                    "schema_variant": derived["state_schema_variant"],
                    "jobs": derived["agent_jobs_total"],
                    "items": derived["agent_job_items_total"],
                },
            },
            {
                "id": "codex.distilled.raw_memories",
                "type": "memory_artifact",
                "metrics": {
                    "items": derived["distilled_memory_items"],
                    "bytes": derived["raw_memories_bytes"],
                },
            },
            {
                "id": "bourdon.fallback",
                "type": "recognition_fallback",
                "metrics": {
                    "active": derived["fallback_recall_active"],
                    "items": derived["fallback_memory_items"],
                    "bytes": derived["bourdon_fallback_bytes"],
                },
            },
            {
                "id": "bourdon.federation.codex_l5",
                "type": "l5_manifest",
                "metrics": {
                    "entities": derived["codex_l5_entity_count"],
                    "sessions": derived["codex_l5_session_count"],
                },
            },
        ],
        "edges": [
            {
                "source": "codex.native.stage1",
                "target": "codex.distilled.raw_memories",
                "relation": "legacy_produces_when_available",
            },
            {
                "source": "codex.native.agent_jobs",
                "target": "codex.distilled.raw_memories",
                "relation": "new_schema_observed_alongside",
            },
            {
                "source": "codex.distilled.raw_memories",
                "target": "bourdon.federation.codex_l5",
                "relation": "can_seed",
            },
            {
                "source": "bourdon.federation.codex_l5",
                "target": "bourdon.fallback",
                "relation": "can_render_recognition_bridge",
            },
        ],
    }


def build_snapshot(
    codex_home: Path | None = None,
    library_path: Path | None = None,
    collected_at: datetime | None = None,
    run_codex_mcp: Callable[[list[str]], subprocess.CompletedProcess[str]] | None = _run_codex_mcp,
    previous_snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    resolved_codex_home = codex_home or _resolve_codex_home()
    resolved_library_path = library_path or DEFAULT_LIBRARY_PATH
    timestamp = collected_at or _now()
    memory_files = _memory_files_snapshot(resolved_codex_home)
    state_db_report = _inspect_codex_state_db(resolved_codex_home)
    fallback_recall = _inspect_codex_fallback_recall(resolved_codex_home, codex_brain=None)
    agent_library = _agent_library_snapshot(resolved_library_path)
    snapshot = {
        "schema_version": SCHEMA_VERSION,
        "collected_at": timestamp.astimezone(timezone.utc).isoformat(),
        "host": socket.gethostname(),
        "safety": {
            "read_only": True,
            "auth_json_inspected": False,
            "sqlite_mutated": False,
        },
        "codex_home": str(resolved_codex_home) if resolved_codex_home else None,
        "library_path": str(resolved_library_path),
        "memory_files": memory_files,
        "codex_state_db": state_db_report,
        "fallback_recall": fallback_recall,
        "agent_library": agent_library,
        "codex_mcp": _codex_mcp_snapshot(run_codex_mcp),
    }
    snapshot["derived"] = _derived_metrics(
        state_db_report,
        fallback_recall,
        memory_files,
        agent_library,
    )
    snapshot["trend"] = _trend(snapshot, previous_snapshot)
    snapshot["graph"] = _graph(snapshot)
    return snapshot


def _load_previous(path: Path | None) -> dict[str, Any] | None:
    if path is None or not path.exists():
        return None
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() in {".yaml", ".yml"}:
        data = yaml.safe_load(text) or {}
    else:
        data = json.loads(text)
    return data if isinstance(data, dict) else None


def _render(snapshot: dict[str, Any], output_format: str) -> str:
    if output_format == "yaml":
        return yaml.safe_dump(snapshot, sort_keys=False)
    return json.dumps(snapshot, indent=2, sort_keys=False) + "\n"


def _report_extension(output_format: str) -> str:
    return "yaml" if output_format == "yaml" else "json"


def _latest_report_path(reports_dir: Path, output_format: str) -> Path:
    return reports_dir / f"latest.{_report_extension(output_format)}"


def _timestamped_report_path(
    reports_dir: Path,
    collected_at: datetime,
    output_format: str,
) -> Path:
    timestamp = collected_at.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return reports_dir / f"codex-memory-metrics-{timestamp}.{_report_extension(output_format)}"


def _resolve_previous_snapshot_path(
    previous_path: Path | None,
    reports_dir: Path | None,
    output_format: str,
) -> Path | None:
    if previous_path is not None:
        return previous_path
    if reports_dir is None:
        return None
    latest_path = _latest_report_path(reports_dir, output_format)
    return latest_path if latest_path.exists() else None


def _load_previous_safely(path: Path | None) -> tuple[dict[str, Any] | None, str | None]:
    if path is None:
        return None, None
    try:
        return _load_previous(path), None
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, yaml.YAMLError) as error:
        return None, f"{type(error).__name__}: {error}"


def _attach_reporting_metadata(
    snapshot: dict[str, Any],
    previous_path: Path | None,
    previous_error: str | None,
    output_path: Path | None,
    reports_dir: Path | None,
    output_format: str,
    collected_at: datetime,
) -> tuple[Path | None, Path | None]:
    timestamped_path = None
    latest_path = None
    if reports_dir is not None:
        timestamped_path = _timestamped_report_path(reports_dir, collected_at, output_format)
        latest_path = _latest_report_path(reports_dir, output_format)

    snapshot["reporting"] = {
        "previous_snapshot_path": str(previous_path) if previous_path else None,
        "previous_snapshot_loaded": previous_path is not None and previous_error is None,
        "previous_snapshot_error": previous_error,
        "explicit_out_path": str(output_path) if output_path else None,
        "timestamped_report_path": str(timestamped_path) if timestamped_path else None,
        "latest_report_path": str(latest_path) if latest_path else None,
        "output_format": output_format,
    }
    return timestamped_path, latest_path


def _write_report(path: Path | None, rendered: str) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(rendered, encoding="utf-8")


def _html_escape(value: Any) -> str:
    return html.escape(str(value), quote=True)


def _format_metric(value: Any) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:.4f}"
    if isinstance(value, int):
        return f"{value:,}"
    return str(value)


def _format_bytes(value: Any) -> str:
    if not isinstance(value, int):
        return "n/a"
    if value >= 1024 * 1024:
        return f"{value / (1024 * 1024):.2f} MiB"
    if value >= 1024:
        return f"{value / 1024:.1f} KiB"
    return f"{value} B"


def _html_report_paths(
    html_report_dir: Path,
    collected_at: datetime,
) -> tuple[Path, Path]:
    report_date = collected_at.astimezone(timezone.utc).date().isoformat()
    return html_report_dir / f"{report_date}.html", html_report_dir / "latest.html"


def _l5_staleness_cutoff(snapshot: dict[str, Any], staleness_days: int) -> str:
    collected_at_raw = str(snapshot.get("collected_at") or "")
    try:
        collected_at = datetime.fromisoformat(collected_at_raw)
    except ValueError:
        collected_at = _now()
    return (collected_at - timedelta(days=staleness_days)).date().isoformat()


def _dashboard_status(
    snapshot: dict[str, Any],
    staleness_days: int = DEFAULT_L5_STALENESS_DAYS,
) -> dict[str, Any]:
    derived = snapshot["derived"]
    codex_l5_last_updated = str(derived.get("codex_l5_last_updated") or "")
    cutoff = _l5_staleness_cutoff(snapshot, staleness_days)
    blockers: list[str] = []
    if not derived.get("stage1_counters_available"):
        blockers.append("Legacy Stage 1 metric continuity unavailable")
    if codex_l5_last_updated and codex_l5_last_updated < cutoff:
        blockers.append("Codex L5 publication stale")
    if not snapshot.get("codex_mcp", {}).get("installed"):
        blockers.append("Codex MCP unavailable")
    if not derived.get("native_memory_present"):
        blockers.append("Native memory accumulation inactive")

    if blockers:
        readiness = "Read-only adoption eval ready; Native-primary adoption blocked"
    else:
        readiness = "Native-primary adoption candidate"
    return {
        "schema_detection": "Schema detection fixed",
        "stage1_continuity": (
            "Legacy Stage 1 metric continuity available"
            if derived.get("stage1_counters_available")
            else "Legacy Stage 1 metric continuity unavailable"
        ),
        "native_accumulation": (
            "Native memory accumulation active"
            if derived.get("native_memory_present")
            else "Native memory accumulation inactive"
        ),
        "readiness": readiness,
        "blockers": blockers,
    }


def _metric_card(
    title: str,
    value: Any,
    note: str,
    *,
    kind: str,
    path_kind: str,
    accent: str,
    changed: bool,
) -> str:
    return f"""
      <article class="metric-card accent-{accent}" data-kind="{kind}" data-path="{path_kind}" data-changed="{str(changed).lower()}">
        <div class="metric-top"><span>{_html_escape(title)}</span><span class="chip">{_html_escape(kind)}</span></div>
        <strong>{_html_escape(value)}</strong>
        <p>{_html_escape(note)}</p>
      </article>"""


def _trend_row(label: str, value: Any, delta: Any, max_abs_delta: float) -> str:
    changed = isinstance(delta, (int, float)) and delta != 0
    width = 0
    if isinstance(delta, (int, float)) and max_abs_delta > 0:
        width = min(100, int(abs(delta) / max_abs_delta * 100))
    direction = "pos" if isinstance(delta, (int, float)) and delta > 0 else "neg" if changed else "flat"
    return f"""
      <tr data-changed="{str(changed).lower()}">
        <td>{_html_escape(label)}</td>
        <td>{_html_escape(_format_metric(value))}</td>
        <td class="delta {direction}">{_html_escape(_format_metric(delta))}</td>
        <td><span class="bar"><span class="bar-fill {direction}" style="width:{width}%"></span></span></td>
      </tr>"""


def _render_html_dashboard(
    snapshot: dict[str, Any],
    staleness_days: int = DEFAULT_L5_STALENESS_DAYS,
) -> str:
    derived = snapshot["derived"]
    trend = snapshot.get("trend") or {}
    status = _dashboard_status(snapshot, staleness_days)
    memory_files = snapshot["memory_files"]
    codex_l5 = snapshot["agent_library"]["codex_l5"]
    reporting = snapshot.get("reporting") or {}

    trend_rows = [
        ("Stage 1 outputs", derived["stage1_outputs_total"], trend.get("stage1_outputs_total_delta")),
        ("Stage 1 done jobs", derived["stage1_jobs_done"], trend.get("stage1_jobs_done_delta")),
        ("Stage 1 error jobs", derived["stage1_jobs_error"], trend.get("stage1_jobs_error_delta")),
        ("Agent jobs", derived["agent_jobs_total"], trend.get("agent_jobs_total_delta")),
        ("Agent job items", derived["agent_job_items_total"], trend.get("agent_job_items_total_delta")),
        ("Distilled memory items", derived["distilled_memory_items"], trend.get("distilled_memory_items_delta")),
        ("Fallback memory items", derived["fallback_memory_items"], trend.get("fallback_memory_items_delta")),
        ("Session records", derived["session_records"], trend.get("session_records_delta")),
        ("Rollout records", derived["rollout_records"], trend.get("rollout_records_delta")),
        ("Raw memory bytes", derived["raw_memories_bytes"], trend.get("raw_memories_bytes_delta")),
        ("Codex L5 entities", derived["codex_l5_entity_count"], trend.get("codex_l5_entity_count_delta")),
        ("Codex L5 sessions", derived["codex_l5_session_count"], trend.get("codex_l5_session_count_delta")),
    ]
    max_abs_delta = max(
        [abs(delta) for _, _, delta in trend_rows if isinstance(delta, (int, float))]
        or [1]
    )

    cards = [
        _metric_card(
            "Schema detection",
            status["schema_detection"],
            f"variant: {derived['state_schema_variant']}",
            kind="evidence",
            path_kind="collector",
            accent="leaf",
            changed=False,
        ),
        _metric_card(
            "Legacy Stage 1 continuity",
            status["stage1_continuity"],
            "old done/error ratios are not comparable when unavailable",
            kind="evidence",
            path_kind="collector",
            accent="honey" if not derived["stage1_counters_available"] else "leaf",
            changed=False,
        ),
        _metric_card(
            "Native accumulation",
            status["native_accumulation"],
            f"distilled items: {_format_metric(derived['distilled_memory_items'])}",
            kind="memory",
            path_kind="collector",
            accent="teal",
            changed=bool(trend.get("distilled_memory_items_delta")),
        ),
        _metric_card(
            "Adoption readiness",
            status["readiness"],
            f"blockers: {len(status['blockers'])}",
            kind="evidence",
            path_kind="collector",
            accent="orange" if status["blockers"] else "leaf",
            changed=False,
        ),
        _metric_card(
            "Raw memory file",
            _format_bytes(derived["raw_memories_bytes"]),
            f"delta { _format_metric(trend.get('raw_memories_bytes_delta')) } bytes",
            kind="evidence",
            path_kind="collector",
            accent="orchid",
            changed=bool(trend.get("raw_memories_bytes_delta")),
        ),
        _metric_card(
            "Codex L5",
            f"{_format_metric(derived['codex_l5_entity_count'])} entities",
            f"{_format_metric(derived['codex_l5_session_count'])} sessions; last {derived.get('codex_l5_last_updated')}",
            kind="memory",
            path_kind="collector",
            accent="orange",
            changed=False,
        ),
        _metric_card(
            "Codex MCP",
            "installed" if snapshot["codex_mcp"].get("installed") else "missing",
            str(snapshot["codex_mcp"].get("status")),
            kind="evidence",
            path_kind="collector",
            accent="leaf" if snapshot["codex_mcp"].get("installed") else "ember",
            changed=False,
        ),
        _metric_card(
            "Fallback recall",
            "active" if derived["fallback_recall_active"] else "inactive",
            str(derived["fallback_recall_reason"]),
            kind="memory",
            path_kind="collector",
            accent="teal",
            changed=False,
        ),
    ]

    commands = [
        "./.venv/bin/python scripts/codex_memory_metrics.py --reports-dir /Users/radman/agent-library/reports/codex-memory-metrics --html-report-dir /Users/radman/.codex/automation-reports/check-codex-native-memory-integration --format json",
        "codex mcp get bourdon",
    ]
    paths = [
        str(reporting.get("latest_report_path")),
        str(reporting.get("timestamped_report_path")),
        str(reporting.get("html_latest_path")),
        str(reporting.get("html_report_path")),
        str(memory_files["memory_md"]["path"]),
        str(memory_files["raw_memories_md"]["path"]),
        str(memory_files["bourdon_fallback_md"]["path"]),
        str(memory_files["rollout_summaries"]["path"]),
        str(codex_l5["path"]),
        str(Path(snapshot["codex_home"] or "") / "state_5.sqlite"),
    ]

    action_items = status["blockers"] + [
        "Run a read-only adoption/comparison eval before native-primary adoption.",
        "Keep Bourdon fallback/federated recognition primary until continuity and publication catch up.",
    ]
    trend_table = "".join(
        _trend_row(label, value, delta, float(max_abs_delta))
        for label, value, delta in trend_rows
    )
    action_html = "".join(
        f'<div class="action">{_html_escape(action)}</div>' for action in action_items
    )
    collected_at = _html_escape(snapshot["collected_at"])
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Codex Native Memory Dashboard</title>
  <style>
    :root {{
      --bg: #131614; --surface: #1b201e; --surface-2: #252b28;
      --text: #e5f0e3; --muted: #b8d4bb; --dim: #839c87;
      --line: rgba(184, 212, 187, 0.22); --leaf: #a7c080;
      --teal: #7fbbb3; --deep-teal: #35a77c; --honey: #dfa000;
      --ember: #f85552; --orange: #e69875; --steel: #859289;
      --orchid: #df69ba;
    }}
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; background: radial-gradient(circle at top left, rgba(127,187,179,.12), transparent 36%), var(--bg); color: var(--text); font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; line-height: 1.45; }}
    main {{ width: min(1180px, calc(100vw - 32px)); margin: 0 auto; padding: 34px 0 48px; }}
    header, .panel, .filters, .metric-card {{ border: 1px solid var(--line); border-radius: 22px; background: linear-gradient(180deg, rgba(37,43,40,.92), rgba(27,32,30,.94)); box-shadow: inset 0 1px rgba(229,240,227,.08); }}
    header {{ display: grid; gap: 14px; padding: 22px; }}
    h1, h2, p {{ margin: 0; }} h1 {{ font-size: clamp(24px, 4vw, 42px); }} h2 {{ font-size: 18px; color: var(--muted); }}
    .sub {{ color: var(--dim); max-width: 860px; }} .status-strip, .filters {{ display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }}
    .chip, button {{ border: 1px solid var(--line); border-radius: 999px; background: rgba(229,240,227,.04); color: var(--muted); padding: 7px 10px; font: inherit; }}
    button {{ cursor: pointer; }} button.active {{ color: var(--bg); background: var(--leaf); border-color: transparent; }}
    section {{ margin-top: 22px; }} .filters {{ padding: 14px; }}
    .metrics {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(230px, 1fr)); gap: 14px; }}
    .metric-card {{ min-height: 150px; padding: 16px; }}
    .metric-top {{ display: flex; justify-content: space-between; gap: 10px; color: var(--dim); font-size: 12px; text-transform: uppercase; }}
    .metric-card strong {{ display: block; margin-top: 18px; font-size: 22px; }} .metric-card p {{ color: var(--muted); margin-top: 8px; font-size: 13px; }}
    .accent-leaf strong {{ color: var(--leaf); }} .accent-teal strong {{ color: var(--teal); }} .accent-honey strong {{ color: var(--honey); }} .accent-ember strong {{ color: var(--ember); }} .accent-orange strong {{ color: var(--orange); }} .accent-orchid strong {{ color: var(--orchid); }}
    .grid-2 {{ display: grid; grid-template-columns: minmax(0, 1.1fr) minmax(280px, .9fr); gap: 14px; }} .panel {{ padding: 18px; }}
    table {{ width: 100%; border-collapse: collapse; }} th, td {{ border-bottom: 1px solid var(--line); padding: 10px 8px; text-align: left; }} th {{ color: var(--dim); font-size: 12px; text-transform: uppercase; }}
    .delta.pos {{ color: var(--leaf); }} .delta.neg {{ color: var(--ember); }} .delta.flat {{ color: var(--steel); }} .bar {{ display: block; width: 100%; height: 9px; background: rgba(229,240,227,.07); border-radius: 999px; overflow: hidden; }} .bar-fill {{ display: block; height: 100%; border-radius: inherit; }} .bar-fill.pos {{ background: var(--deep-teal); }} .bar-fill.neg {{ background: var(--ember); }} .bar-fill.flat {{ background: var(--steel); }}
    .action {{ padding: 12px; border: 1px solid var(--line); border-radius: 16px; background: rgba(37,43,40,.72); margin-top: 10px; }}
    details {{ border: 1px solid var(--line); border-radius: 18px; background: rgba(37,43,40,.55); padding: 12px 14px; margin-top: 12px; }} summary {{ cursor: pointer; color: var(--muted); }}
    pre {{ white-space: pre-wrap; overflow-wrap: anywhere; color: var(--text); background: #131614; padding: 12px; border-radius: 14px; border: 1px solid var(--line); }} .hidden {{ display: none !important; }}
    @media (max-width: 820px) {{ .grid-2 {{ grid-template-columns: 1fr; }} main {{ width: min(100vw - 20px, 1180px); }} }}
  </style>
</head>
<body>
  <main>
    <header>
      <div class="status-strip">
        <span class="chip">Run {collected_at}</span>
        <span class="chip">Collector present</span>
        <span class="chip">Blockers {len(status["blockers"])}</span>
        <span class="chip">{_html_escape(status["readiness"])}</span>
      </div>
      <h1>Codex Native Memory Health</h1>
      <p class="sub">Schema detection is fixed. The remaining native-readiness gap is legacy Stage 1 metric continuity plus publication freshness, while native memory accumulation is measured from current artifacts.</p>
    </header>
    <section class="filters">
      <span class="chip">Filters</span>
      <button data-filter-kind="all" class="active">All</button><button data-filter-kind="evidence">Evidence</button><button data-filter-kind="memory">Memory-derived</button>
      <button data-filter-path="all" class="active">All paths</button><button data-filter-path="collector">Collector</button><button data-filter-path="fallback">Fallback</button><button id="changedOnly">Changed-only</button>
    </section>
    <section class="metrics">{''.join(cards)}</section>
    <section class="grid-2">
      <div class="panel"><h2>Trend Table</h2><table><thead><tr><th>Signal</th><th>Current</th><th>Delta</th><th>Magnitude</th></tr></thead><tbody>{trend_table}</tbody></table></div>
      <aside class="panel"><h2>Action Lane</h2>{action_html}</aside>
    </section>
    <section class="panel">
      <h2>Exact Evidence</h2>
      <details open><summary>Commands</summary><pre>{_html_escape(chr(10).join(commands))}</pre></details>
      <details><summary>Paths</summary><pre>{_html_escape(chr(10).join(paths))}</pre></details>
      <details><summary>SQLite Schema</summary><pre>{_html_escape(json.dumps(snapshot['codex_state_db'].get('schema'), indent=2))}</pre></details>
      <details><summary>Graph</summary><pre>{_html_escape(json.dumps(snapshot.get('graph'), indent=2))}</pre></details>
      <details><summary>Raw Snapshot</summary><pre>{_html_escape(json.dumps(snapshot, indent=2))}</pre></details>
    </section>
  </main>
  <script>
    const state = {{ kind: 'all', path: 'all', changedOnly: false }};
    const cards = document.querySelectorAll('.metric-card');
    const rows = document.querySelectorAll('tbody tr');
    function applyFilters() {{
      cards.forEach(card => {{
        const kindOk = state.kind === 'all' || card.dataset.kind === state.kind;
        const pathOk = state.path === 'all' || card.dataset.path === state.path;
        const changedOk = !state.changedOnly || card.dataset.changed === 'true';
        card.classList.toggle('hidden', !(kindOk && pathOk && changedOk));
      }});
      rows.forEach(row => row.classList.toggle('hidden', state.changedOnly && row.dataset.changed !== 'true'));
    }}
    document.querySelectorAll('button').forEach(button => button.addEventListener('click', () => {{
      if (button.dataset.filterKind) {{
        state.kind = button.dataset.filterKind;
        document.querySelectorAll('[data-filter-kind]').forEach(item => item.classList.remove('active'));
        button.classList.add('active');
      }}
      if (button.dataset.filterPath) {{
        state.path = button.dataset.filterPath;
        document.querySelectorAll('[data-filter-path]').forEach(item => item.classList.remove('active'));
        button.classList.add('active');
      }}
      if (button.id === 'changedOnly') {{ state.changedOnly = !state.changedOnly; button.classList.toggle('active', state.changedOnly); }}
      applyFilters();
    }}));
  </script>
</body>
</html>
"""


def _write_html_reports(
    html_report_dir: Path | None,
    snapshot: dict[str, Any],
    collected_at: datetime,
    staleness_days: int = DEFAULT_L5_STALENESS_DAYS,
) -> None:
    if html_report_dir is None:
        return
    report_path, latest_path = _html_report_paths(html_report_dir, collected_at)
    snapshot["reporting"]["html_report_path"] = str(report_path)
    snapshot["reporting"]["html_latest_path"] = str(latest_path)
    rendered = _render_html_dashboard(snapshot, staleness_days)
    _write_report(report_path, rendered)
    _write_report(latest_path, rendered)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Collect Codex memory metrics for Bourdon.")
    parser.add_argument("--codex-home", type=Path, default=None)
    parser.add_argument("--library-path", type=Path, default=DEFAULT_LIBRARY_PATH)
    parser.add_argument(
        "--previous",
        type=Path,
        default=None,
        help="Previous JSON/YAML snapshot for deltas.",
    )
    parser.add_argument("--out", type=Path, default=None, help="Write the snapshot to this path.")
    parser.add_argument(
        "--reports-dir",
        type=Path,
        default=None,
        help=(
            "Write a timestamped snapshot and refresh latest.json/latest.yaml. "
            "When --previous is omitted, latest is used for deltas if it exists."
        ),
    )
    parser.add_argument(
        "--html-report-dir",
        type=Path,
        default=None,
        help="Write a dated self-contained HTML dashboard and refresh latest.html.",
    )
    parser.add_argument(
        "--l5-staleness-days",
        type=int,
        default=DEFAULT_L5_STALENESS_DAYS,
        help=(
            "Mark the Codex L5 publication as stale when its last_updated is older "
            "than this many days before the run's collected_at. Default: "
            f"{DEFAULT_L5_STALENESS_DAYS}."
        ),
    )
    parser.add_argument(
        "--format",
        choices=("json", "yaml"),
        default="json",
        help="Output format (default: json).",
    )
    parser.add_argument(
        "--skip-mcp",
        action="store_true",
        help="Do not run `codex mcp get bourdon`.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    collected_at = _now()
    previous_path = _resolve_previous_snapshot_path(
        args.previous,
        args.reports_dir,
        args.format,
    )
    previous, previous_error = _load_previous_safely(previous_path)
    snapshot = build_snapshot(
        codex_home=args.codex_home,
        library_path=args.library_path,
        collected_at=collected_at,
        previous_snapshot=previous,
        run_codex_mcp=None if args.skip_mcp else _run_codex_mcp,
    )
    timestamped_path, latest_path = _attach_reporting_metadata(
        snapshot,
        previous_path,
        previous_error,
        args.out,
        args.reports_dir,
        args.format,
        collected_at,
    )
    if args.html_report_dir:
        report_path, latest_html_path = _html_report_paths(
            args.html_report_dir,
            collected_at,
        )
        snapshot["reporting"]["html_report_path"] = str(report_path)
        snapshot["reporting"]["html_latest_path"] = str(latest_html_path)
    rendered = _render(snapshot, args.format)
    _write_report(args.out, rendered)
    _write_report(timestamped_path, rendered)
    _write_report(latest_path, rendered)
    _write_html_reports(
        args.html_report_dir,
        snapshot,
        collected_at,
        args.l5_staleness_days,
    )
    sys.stdout.write(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
