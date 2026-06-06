"""
``bourdon demo`` -- self-contained cross-machine recognition walkthrough.

Recreates the 2026-05-26 cross-machine recognition test locally on one
machine using synthetic agent-library content. No real IDE state is touched;
no network calls are made. The point is to let a brand-new user *see*
what Bourdon does end-to-end without installing Codex, wiring SessionEnd
hooks, or doing any cross-machine work.

What actually runs:

  1. Stage a synthetic agent-library in a tempdir with three sample manifests
     (claude-code, codex, cursor) that share a "DemoProject" entity so dedup
     and multi-agent ``(via <agent>)`` attribution show up.
  2. Filter that library to the requested access level (default: public).
  3. Render the resulting recognition manifest into a Bourdon-style
     ``MEMORY.md`` (the same surface Codex.app reads at the end of the real
     cross-machine pipeline).
  4. Print byte uplift, source-attribution counts, and the first ~30 lines
     of the rendered file so the user sees the actual output.
  5. Leave the tempdir on disk and tell the user where to look.

The federation -> render machinery is the production code path
(``L6Store.build_recognition_manifest`` and the codex participant's
``_render_codex_federation_memory_text``). Only the input library is
synthetic.
"""

from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
from pathlib import Path
from textwrap import dedent
from typing import Optional

import yaml

# ---------------------------------------------------------------------------
# Synthetic agent-library payload
# ---------------------------------------------------------------------------


_SYNTHETIC_CLAUDE_CODE = {
    "spec_version": "0.1",
    "agent": {
        "id": "claude-code",
        "type": "code-assistant",
        "instance": "demo-host",
        "spec_version_compat": ">=0.1",
        "role_narrative": "Demo manager-and-reviewer agent.",
    },
    "last_updated": "2026-05-26T12:00:00+00:00",
    "capabilities": ["claude_brain", "auto_memory"],
    "recent_sessions": [
        {
            "date": "2026-05-26",
            "cwd": "/projects/demo-project",
            "project_focus": ["DemoProject"],
            "key_actions": ["Reviewed cross-machine federation",
                            "Wrote rerun benchmark"],
            "visibility": "public",
        },
        {
            "date": "2026-05-25",
            "cwd": "/projects/client-crm",
            "project_focus": ["ClientCRM"],
            "key_actions": ["Schema redesign"],
            "visibility": "public",
        },
    ],
    "known_entities": [
        {
            "name": "DemoProject",
            "type": "project",
            "summary": (
                "A two-sided demo of recognition-first runtime + cross-agent "
                "memory federation. Pre-alpha, source available."
            ),
            "tags": ["project", "active"],
            "visibility": "public",
        },
        {
            "name": "ClientCRM",
            "type": "project",
            "summary": "Customer relationship management for the demo team.",
            "tags": ["project"],
            "visibility": "public",
        },
        {
            "name": "ResearchSpike",
            "type": "concept",
            "summary": "Time-boxed exploration of new memory architectures.",
            "tags": ["concept"],
            "visibility": "team",
        },
        {
            "name": "PrivatePersonal",
            "type": "person",
            "summary": "Personal note kept out of public federation by visibility.",
            "tags": ["personal"],
            "visibility": "private",
        },
    ],
}


_SYNTHETIC_CODEX = {
    "spec_version": "0.1",
    "agent": {
        "id": "codex",
        "type": "code-assistant",
        "instance": "demo-host",
        "spec_version_compat": ">=0.1",
        "role_narrative": "Demo lead code-assistant.",
    },
    "last_updated": "2026-05-26T12:00:00+00:00",
    "capabilities": ["sessions_dir", "memory_md"],
    "recent_sessions": [
        {
            "date": "2026-05-24",
            "cwd": "/projects/demo-project",
            "project_focus": ["DemoProject"],
            "key_actions": ["Implemented sync push/pull verb"],
            "visibility": "public",
        },
    ],
    "known_entities": [
        {
            "name": "DemoProject",
            "type": "project",
            "summary": "Codex's view: recognition pipeline + rsync federation transport.",
            "tags": ["project", "active"],
            "visibility": "public",
        },
        {
            "name": "TeamArchitecture",
            "type": "concept",
            "summary": "Internal decisions about the L0-L6 stack.",
            "tags": ["concept"],
            "visibility": "team",
        },
    ],
}


_SYNTHETIC_CURSOR = {
    "spec_version": "0.1",
    "agent": {
        "id": "cursor",
        "type": "code-assistant",
        "instance": "demo-host",
        "spec_version_compat": ">=0.1",
        "role_narrative": "Demo IDE-embedded assistant.",
    },
    "last_updated": "2026-05-26T12:00:00+00:00",
    "capabilities": ["state_vscdb"],
    "recent_sessions": [],
    "known_entities": [
        {
            "name": "UIPolishPass",
            "type": "project",
            "summary": "Cosmetic refinements to the demo product's storefront.",
            "tags": ["project"],
            "visibility": "public",
        },
    ],
}


def stage_synthetic_library(root: Path) -> Path:
    """Write the three synthetic manifests into ``root/agent-library/agents/``.

    Returns the staged ``agent-library/`` root path.
    """
    library = root / "agent-library"
    agents = library / "agents"
    agents.mkdir(parents=True, exist_ok=True)
    (agents / "claude-code.l5.yaml").write_text(
        yaml.safe_dump(_SYNTHETIC_CLAUDE_CODE, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    (agents / "codex.l5.yaml").write_text(
        yaml.safe_dump(_SYNTHETIC_CODEX, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    (agents / "cursor.l5.yaml").write_text(
        yaml.safe_dump(_SYNTHETIC_CURSOR, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    return library


# ---------------------------------------------------------------------------
# Demo runner
# ---------------------------------------------------------------------------


def run_demo(
    *,
    access_level: str = "public",
    keep_tempdir: bool = True,
    stream=sys.stdout,
) -> dict[str, object]:
    """Execute the demo end-to-end. Returns a dict describing the outcome."""
    # Defer the heavy imports until run-time so `bourdon --help` stays snappy.
    from participants.codex import _render_codex_federation_memory_text

    # mkdtemp returns a tempdir we own; we explicitly rmtree() at the end
    # only when keep_tempdir is False. Avoids private-API gymnastics around
    # TemporaryDirectory's auto-finalizer.
    tempdir = Path(tempfile.mkdtemp(prefix="bourdon-demo-"))
    try:
        _banner(stream)

        library = stage_synthetic_library(tempdir)
        _print_library_summary(library, access_level, stream)

        # Render the federation->Codex memory text. This is the same function
        # that powers `bourdon codex sync-native --from-library` in production.
        rendered = _render_codex_federation_memory_text(
            library_path=library,
            access_level=access_level,
            include_local=False,
        )
        rendered_path = tempdir / "MEMORY.md"
        rendered_path.write_text(rendered, encoding="utf-8")

        _print_rendered_summary(rendered_path, rendered, stream)
        _print_outcome(rendered, library, tempdir, access_level, keep_tempdir, stream)

        return {
            "tempdir": str(tempdir),
            "library_path": str(library),
            "rendered_path": str(rendered_path),
            "bytes": len(rendered.encode("utf-8")),
            "access_level": access_level,
        }
    finally:
        if not keep_tempdir:
            shutil.rmtree(tempdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Rendering pieces (kept separate from run_demo for testability)
# ---------------------------------------------------------------------------


def _banner(stream) -> None:
    print(
        dedent(
            """
            ==> Bourdon cross-machine recognition demo <==

            This demo recreates the 2026-05-26 cross-machine recognition test
            locally using synthetic federation content. No real IDE state is
            touched, no network calls are made. The federation pipeline itself
            is the production code path; only the input library is synthetic.
            """
        ).strip(),
        file=stream,
    )
    print(file=stream)


def _print_library_summary(library: Path, access_level: str, stream) -> None:
    print(f"Staged synthetic library at: {library}", file=stream)
    print(file=stream)
    for manifest in sorted((library / "agents").glob("*.l5.yaml")):
        data = yaml.safe_load(manifest.read_text(encoding="utf-8")) or {}
        entities = data.get("known_entities") or []
        sessions = data.get("recent_sessions") or []
        agent_id = (data.get("agent") or {}).get("id") or manifest.stem
        print(
            f"  {agent_id:<14}  {len(entities)} entities, {len(sessions)} sessions",
            file=stream,
        )
    print(file=stream)
    print(
        f"Federation will be filtered at access_level={access_level!r}. "
        "Entries above that level are dropped before the render step.",
        file=stream,
    )
    print(file=stream)


def _print_rendered_summary(path: Path, rendered: str, stream) -> None:
    byte_count = len(rendered.encode("utf-8"))
    attribution_count = rendered.count("(via ")
    entity_lines = sum(
        1 for line in rendered.splitlines() if line.startswith("- ")
    )

    print("=== Render result ===", file=stream)
    print(f"  output file:                  {path}", file=stream)
    print(f"  size:                         {byte_count:,} bytes", file=stream)
    print(f"  source-attribution strings:   {attribution_count}", file=stream)
    print(f"  entity list items:            {entity_lines}", file=stream)
    print(file=stream)

    # Show the first 30 lines so the user sees what Codex would consult.
    print("--- first 30 lines of rendered MEMORY.md ---", file=stream)
    for line in rendered.splitlines()[:30]:
        print(line, file=stream)
    if len(rendered.splitlines()) > 30:
        remaining = len(rendered.splitlines()) - 30
        print(f"... ({remaining} more lines)", file=stream)
    print(file=stream)


def _print_outcome(
    rendered: str,
    library: Path,
    tempdir: Path,
    access_level: str,
    keep_tempdir: bool,
    stream,
) -> None:
    # Demonstrate that visibility filtering kicked in.
    excluded_present = "PrivatePersonal" in rendered
    team_present = "ResearchSpike" in rendered or "TeamArchitecture" in rendered
    dedup_present = "DemoProject (via claude-code, codex)" in rendered or (
        "(via " in rendered and "claude-code" in rendered and "codex" in rendered
    )

    print("=== What just happened ===", file=stream)
    if access_level == "public":
        if not excluded_present:
            print(
                "  + Visibility filter dropped the 'PrivatePersonal' entity (private)",
                file=stream,
            )
        if not team_present:
            print(
                "  + Visibility filter dropped 'ResearchSpike' + 'TeamArchitecture' (team)",
                file=stream,
            )
    if dedup_present:
        print(
            "  + Multi-agent dedup preserved: 'DemoProject' carries (via claude-code, codex)",
            file=stream,
        )
    print(
        "  + The exact L6Store + codex render functions used in production "
        "ran against the synthetic library",
        file=stream,
    )
    print(file=stream)

    print("=== Next ===", file=stream)
    if keep_tempdir:
        print(f"  Inspect the rendered file:    cat {tempdir / 'MEMORY.md'}", file=stream)
        print(f"  Inspect the source library:   ls {library / 'agents'}", file=stream)
        print(f"  Clean up when done:           rm -rf {tempdir}", file=stream)
    else:
        print("  (--no-keep set; tempdir was deleted after the run)", file=stream)
    print(file=stream)
    print(
        "  To run this for real with your IDE state instead of synthetic data:",
        file=stream,
    )
    print("    bourdon setup", file=stream)
    print("    bourdon sync push <remote>", file=stream)
    print(file=stream)


# ---------------------------------------------------------------------------
# CLI handler
# ---------------------------------------------------------------------------


def handle_demo(args: argparse.Namespace) -> int:
    access_level = getattr(args, "access_level", "public")
    keep_tempdir = not getattr(args, "no_keep", False)
    run_demo(
        access_level=access_level,
        keep_tempdir=keep_tempdir,
    )
    return 0


def add_demo_parser(subparsers: argparse._SubParsersAction) -> None:
    """Register the ``demo`` subcommand."""
    demo_cmd = subparsers.add_parser(
        "demo",
        help=(
            "Self-contained cross-machine recognition walkthrough using "
            "synthetic federation content (no real IDE state required)."
        ),
    )
    demo_cmd.add_argument(
        "--access-level",
        choices=("public", "team", "private"),
        default="public",
        help=(
            "Visibility level to filter the synthetic library to (default: public). "
            "Try `team` or `private` to see entries that would otherwise be hidden."
        ),
    )
    demo_cmd.add_argument(
        "--no-keep",
        action="store_true",
        help=(
            "Delete the tempdir after the run instead of leaving it for inspection."
        ),
    )
    demo_cmd.set_defaults(func=handle_demo)


__all__ = [
    "add_demo_parser",
    "handle_demo",
    "run_demo",
    "stage_synthetic_library",
]
