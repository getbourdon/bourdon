#!/usr/bin/env python3
"""Cross-platform regression matrix for short-index schema enforcement.

Runs migrate / validate / build_bourdon_l5 against each fixture case under
``tests/fixtures/short-index/``, compares actual exit codes and the
``known_entities`` count against the expectations declared in each case's
``meta.json``, and writes a JSON report to
``.cursor/memory/reports/regression-matrix-report.json``.

Replaces the legacy PowerShell version (``scripts/regression_matrix.ps1``)
with a stdlib-only Python implementation so contributors can run the
matrix on macOS, Linux, and Windows without PowerShell.

CLI:
    python scripts/regression_matrix.py [--workspace-root PATH]
                                        [--fixtures-root PATH]
                                        [--report-path PATH]

Exits non-zero (and writes ``status: "fail"`` to the report) if any case
diverges from its expectations.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any

DEFAULT_FIXTURES_ROOT = "tests/fixtures/short-index"
DEFAULT_REPORT_PATH = ".cursor/memory/reports/regression-matrix-report.json"

EMPTY_INDEX = '{\n  "version": 1,\n  "entries": []\n}\n'


def _run_python_step(python_cmd: str, args: list[str]) -> int:
    """Run a Python subprocess, stream output, return exit code."""
    completed = subprocess.run(
        [python_cmd, *args],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if completed.stdout:
        sys.stdout.write(completed.stdout)
    if completed.stderr:
        sys.stderr.write(completed.stderr)
    return completed.returncode


def _read_known_entities(
    python_cmd: str, yaml_path: Path, case_name: str | None = None
) -> int | None:
    """Parse ``known_entities`` count from a generated L5 YAML; ``None`` on failure."""
    context_prefix = f"[{case_name}] " if case_name else ""
    if not yaml_path.exists():
        print(
            f"{context_prefix}Expected export artifact missing: {yaml_path}",
            file=sys.stderr,
        )
        return None
    completed = subprocess.run(
        [
            python_cmd,
            "-c",
            (
                "import sys, yaml; "
                "data = yaml.safe_load(open(sys.argv[1], encoding='utf-8')); "
                "print(len(data.get('known_entities', [])))"
            ),
            str(yaml_path),
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if completed.returncode != 0:
        print(
            f"{context_prefix}Failed to parse known_entities count for {yaml_path}: "
            f"{completed.stderr.strip()}",
            file=sys.stderr,
        )
        return None
    raw_lines = completed.stdout.strip().splitlines()
    if not raw_lines:
        return None
    try:
        return int(raw_lines[-1])
    except ValueError:
        return None


def _setup_case_dirs(case_dir: Path) -> dict[str, Path]:
    """Create the temp workspace for a fixture case and copy/seed indices."""
    temp_root = Path(tempfile.gettempdir()) / f"bourdon-regression-{uuid.uuid4().hex}"
    workspace_root = temp_root / "workspace"
    global_root = temp_root / "global-memory"
    out_root = temp_root / "out"
    workspace_memory = workspace_root / ".cursor" / "memory"
    workspace_memory.mkdir(parents=True, exist_ok=True)
    global_root.mkdir(parents=True, exist_ok=True)
    out_root.mkdir(parents=True, exist_ok=True)

    workspace_index = workspace_memory / "short-index.json"
    global_index = global_root / "short-index.json"
    workspace_out = out_root / "workspace.l5.yaml"
    global_out = out_root / "global.l5.yaml"

    workspace_fixture = case_dir / "workspace-short-index.json"
    global_fixture = case_dir / "global-short-index.json"

    if workspace_fixture.exists():
        shutil.copy2(workspace_fixture, workspace_index)
    else:
        workspace_index.write_text(EMPTY_INDEX, encoding="utf-8")

    if global_fixture.exists():
        shutil.copy2(global_fixture, global_index)
    else:
        global_index.write_text(EMPTY_INDEX, encoding="utf-8")

    return {
        "temp_root": temp_root,
        "workspace_root": workspace_root,
        "global_root": global_root,
        "workspace_index": workspace_index,
        "global_index": global_index,
        "workspace_out": workspace_out,
        "global_out": global_out,
    }


def _run_case(
    python_cmd: str,
    case_dir: Path,
    schema_path: Path,
    migrate_script: Path,
    validate_script: Path,
    build_script: Path,
) -> dict[str, Any]:
    """Execute one regression case and return its result dict."""
    case_name = case_dir.name
    print()
    print(f"=== Regression case: {case_name} ===")

    meta_path = case_dir / "meta.json"
    if not meta_path.exists():
        raise FileNotFoundError(f"Missing fixture metadata: {meta_path}")
    meta = json.loads(meta_path.read_text(encoding="utf-8"))

    expected_check = int(meta.get("expectCheckExit", 0))
    expected_validate = int(meta.get("expectValidateExit", 0))
    expected_export = int(meta.get("expectExportExit", 0))
    run_migrate_write = bool(meta.get("runMigrateWrite", False))
    expected_known_entities = meta.get("expectedKnownEntities")
    if expected_known_entities is not None:
        expected_known_entities = int(expected_known_entities)

    paths = _setup_case_dirs(case_dir)
    try:
        check_exit = _run_python_step(
            python_cmd,
            [
                str(migrate_script),
                "--workspace-root",
                str(paths["workspace_root"]),
                "--path",
                str(paths["workspace_index"]),
                "--path",
                str(paths["global_index"]),
                "--check",
            ],
        )

        migrate_write_exit: int | None = None
        if run_migrate_write:
            migrate_write_exit = _run_python_step(
                python_cmd,
                [
                    str(migrate_script),
                    "--workspace-root",
                    str(paths["workspace_root"]),
                    "--path",
                    str(paths["workspace_index"]),
                    "--path",
                    str(paths["global_index"]),
                ],
            )

        validate_exit = _run_python_step(
            python_cmd,
            [
                str(validate_script),
                "--workspace-root",
                str(paths["workspace_root"]),
                "--path",
                str(paths["workspace_index"]),
                "--path",
                str(paths["global_index"]),
            ],
        )

        export_exit: int | None = None
        known_entities: int | None = None
        if validate_exit == 0:
            export_exit = _run_python_step(
                python_cmd,
                [
                    str(build_script),
                    "--workspace-root",
                    str(paths["workspace_root"]),
                    "--global-root",
                    str(paths["global_root"]),
                    "--workspace-out",
                    str(paths["workspace_out"]),
                    "--global-out",
                    str(paths["global_out"]),
                    "--strict-aliases",
                    "--strict-precedence",
                    "--schema-path",
                    str(schema_path),
                ],
            )
            if export_exit == 0 and expected_known_entities is not None:
                known_entities = _read_known_entities(
                    python_cmd, paths["workspace_out"], case_name=case_name
                )

        case_pass = True
        if check_exit != expected_check:
            case_pass = False
        if validate_exit != expected_validate:
            case_pass = False
        if (
            validate_exit == 0
            and export_exit is not None
            and export_exit != expected_export
        ):
            case_pass = False
        if run_migrate_write and migrate_write_exit != 0:
            case_pass = False
        if (
            expected_known_entities is not None
            and known_entities is not None
            and known_entities != expected_known_entities
        ):
            case_pass = False

        return {
            "case": case_name,
            "description": str(meta.get("description", "")),
            "pass": case_pass,
            "expectCheckExit": expected_check,
            "checkExit": check_exit,
            "expectValidateExit": expected_validate,
            "validateExit": validate_exit,
            "expectExportExit": expected_export,
            "exportExit": export_exit,
            "runMigrateWrite": run_migrate_write,
            "migrateWriteExit": migrate_write_exit,
            "expectedKnownEntities": expected_known_entities,
            "knownEntities": known_entities,
        }
    finally:
        shutil.rmtree(paths["temp_root"], ignore_errors=True)


def _print_summary(results: list[dict[str, Any]]) -> None:
    print()
    print("Regression matrix results:")
    headers = ["case", "pass", "checkExit", "validateExit", "exportExit", "knownEntities"]
    rows = [
        [
            r["case"],
            str(r["pass"]),
            str(r["checkExit"]),
            str(r["validateExit"]),
            "" if r["exportExit"] is None else str(r["exportExit"]),
            "" if r["knownEntities"] is None else str(r["knownEntities"]),
        ]
        for r in results
    ]
    widths = [max(len(h), *(len(row[i]) for row in rows)) for i, h in enumerate(headers)]
    fmt = "  ".join(f"{{:<{w}}}" for w in widths)
    print(fmt.format(*headers))
    print(fmt.format(*["-" * w for w in widths]))
    for row in rows:
        print(fmt.format(*row))


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run short-index regression matrix.")
    parser.add_argument("--workspace-root", type=Path, default=Path.cwd())
    parser.add_argument("--fixtures-root", type=Path, default=Path(DEFAULT_FIXTURES_ROOT))
    parser.add_argument("--report-path", type=Path, default=Path(DEFAULT_REPORT_PATH))
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    workspace_root = args.workspace_root.resolve()

    fixtures_root = (
        args.fixtures_root
        if args.fixtures_root.is_absolute()
        else workspace_root / args.fixtures_root
    )
    if not fixtures_root.exists():
        print(f"Fixtures root not found: {fixtures_root}", file=sys.stderr)
        return 1

    schema_path = workspace_root / "spec" / "L5_schema.json"
    build_script = workspace_root / "scripts" / "build_bourdon_l5.py"
    migrate_script = workspace_root / "scripts" / "migrate_short_index.py"
    validate_script = workspace_root / "scripts" / "validate_short_index.py"

    case_dirs = sorted(d for d in fixtures_root.iterdir() if d.is_dir())
    if not case_dirs:
        print(f"No regression fixtures found in {fixtures_root}", file=sys.stderr)
        return 1

    python_cmd = sys.executable
    results: list[dict[str, Any]] = []
    overall_pass = True
    for case_dir in case_dirs:
        result = _run_case(
            python_cmd=python_cmd,
            case_dir=case_dir,
            schema_path=schema_path,
            migrate_script=migrate_script,
            validate_script=validate_script,
            build_script=build_script,
        )
        results.append(result)
        if not result["pass"]:
            overall_pass = False

    report_path = (
        args.report_path
        if args.report_path.is_absolute()
        else workspace_root / args.report_path
    )
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_payload = {
        "status": "pass" if overall_pass else "fail",
        "ranAtUtc": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        "caseCount": len(results),
        "cases": results,
    }
    report_path.write_text(json.dumps(report_payload, indent=2), encoding="utf-8")

    _print_summary(results)
    print(f"Report: {report_path}")

    if not overall_pass:
        print(f"Regression matrix failed. See report at {report_path}.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
