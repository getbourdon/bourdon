"""
``bourdon setup`` -- interactive post-install wizard.

The flow:

1. Detect which AI agents are installed on this machine (Claude Code, Codex,
   Cursor, Copilot, Cascade) by inspecting conventional filesystem paths.
2. Ensure ``~/agent-library/`` exists.
3. For Claude Code specifically, offer to wire a SessionEnd hook that runs
   ``bourdon claude-code export`` so manifests stay fresh automatically.
4. For Copilot / Cascade convention-file adapters, offer to initialize the
   memory file template.
5. For Codex specifically, offer to run ``sync-native --from-library --memory-md --write``
   immediately so the user can see federation content in Codex on their next turn.
6. Print a "what to do next" summary including how to set up cross-machine sync.

The wizard is idempotent: running it twice is safe (hooks/files are not
duplicated). It also supports ``--non-interactive`` (use defaults, never
prompt) and ``--dry-run`` (show what would happen, change nothing).

The per-step logic is exposed as small functions so the surface can be
tested without driving a TTY.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Agent detection
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AgentDetection:
    """A detected (or not-detected) agent on this machine."""
    id: str
    label: str
    present: bool
    hint_path: Path  # where we looked; useful when present=False


def detect_agents(home: Optional[Path] = None) -> list[AgentDetection]:
    """Inspect the user's home directory for known agent paths.

    Returns a list ordered so the "most common to wire" agents appear first.
    Detection is filesystem-only (no shelling out, no PATH lookups), which
    keeps the wizard fast and predictable.
    """
    h = home or Path.home()
    candidates = [
        ("claude-code", "Claude Code", h / ".claude"),
        ("codex", "Codex", h / ".codex"),
        ("cursor", "Cursor", h / ".cursor"),
        ("copilot", "GitHub Copilot", h / ".copilot-bourdon"),
        ("cascade", "Cascade (Windsurf)", h / ".cascade-bourdon"),
    ]
    return [
        AgentDetection(id=aid, label=label, present=path.exists(), hint_path=path)
        for aid, label, path in candidates
    ]


# ---------------------------------------------------------------------------
# ~/agent-library/
# ---------------------------------------------------------------------------


def ensure_agent_library(library_path: Path, *, dry_run: bool = False) -> bool:
    """Create ``library_path`` and its ``agents/`` subdirectory if missing.

    Returns True if anything was created, False if it already existed.
    """
    if library_path.exists() and (library_path / "agents").exists():
        return False
    if dry_run:
        return True
    library_path.mkdir(parents=True, exist_ok=True)
    (library_path / "agents").mkdir(parents=True, exist_ok=True)
    return True


# ---------------------------------------------------------------------------
# Claude Code SessionEnd hook
# ---------------------------------------------------------------------------


def _command_marks_bourdon_hook(command: str) -> bool:
    """Return True if ``command`` looks like a bourdon SessionEnd hook entry.

    We can't use exact substring match (``"bourdon claude-code export"``) because
    Windows resolves the binary to ``C:\\...\\bourdon.exe`` -- the ``.exe`` breaks
    the contiguous substring. Match by tokens instead: must mention ``bourdon``
    (anywhere -- handles bare name, absolute paths, and ``.exe``), the
    ``claude-code`` subcommand, and the ``export`` action.
    """
    if not isinstance(command, str):
        return False
    lowered = command.lower()
    return "bourdon" in lowered and "claude-code" in lowered and "export" in lowered


def claude_code_settings_path(home: Optional[Path] = None) -> Path:
    """Return the path to Claude Code's settings.json."""
    return (home or Path.home()) / ".claude" / "settings.json"


def is_claude_code_hook_wired(
    settings_path: Path,
) -> bool:
    """Return True if a Bourdon SessionEnd hook is already present."""
    if not settings_path.is_file():
        return False
    try:
        data = json.loads(settings_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(data, dict):
        return False
    hooks_root = data.get("hooks") or {}
    if not isinstance(hooks_root, dict):
        return False
    session_end_list = hooks_root.get("SessionEnd") or []
    if not isinstance(session_end_list, list):
        return False
    for entry in session_end_list:
        if not isinstance(entry, dict):
            continue
        for nested in entry.get("hooks") or []:
            if not isinstance(nested, dict):
                continue
            if _command_marks_bourdon_hook(nested.get("command") or ""):
                return True
    return False


def resolve_bourdon_binary() -> str:
    """Return the best path to invoke 'bourdon' from a non-interactive shell.

    Uses ``shutil.which`` so the path is absolute when the binary is on PATH.
    Falls back to the bare name if not found -- the hook will then rely on
    the user's shell PATH at hook-fire time.
    """
    found = shutil.which("bourdon")
    return found or "bourdon"


def wire_claude_code_hook(
    settings_path: Path,
    bourdon_binary: str,
    *,
    dry_run: bool = False,
) -> bool:
    """Patch Claude Code's settings.json to run ``bourdon claude-code export`` at SessionEnd.

    Conservative: reads + merges + writes back. Existing hooks (including
    other SessionEnd entries) are preserved. Returns True if the file was
    changed, False if no change was needed (idempotent).
    """
    if is_claude_code_hook_wired(settings_path):
        return False
    if dry_run:
        return True

    settings_path.parent.mkdir(parents=True, exist_ok=True)

    if settings_path.is_file():
        try:
            data = json.loads(settings_path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                data = {}
        except (OSError, json.JSONDecodeError):
            data = {}
    else:
        data = {}

    hooks_root = data.setdefault("hooks", {})
    if not isinstance(hooks_root, dict):
        hooks_root = {}
        data["hooks"] = hooks_root

    session_end_list = hooks_root.setdefault("SessionEnd", [])
    if not isinstance(session_end_list, list):
        session_end_list = []
        hooks_root["SessionEnd"] = session_end_list

    session_end_list.append(
        {
            "matcher": "*",
            "hooks": [
                {
                    "type": "command",
                    "command": f"{bourdon_binary} claude-code export",
                }
            ],
        }
    )

    settings_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return True


# ---------------------------------------------------------------------------
# Convention-file init helpers (copilot, cascade)
# ---------------------------------------------------------------------------


def init_copilot_memory_if_missing(
    *,
    dry_run: bool = False,
    home: Optional[Path] = None,
) -> Optional[Path]:
    """Create ``~/.copilot-bourdon/memory.md`` template if missing.

    Returns the file path if created, None if already present.
    """
    from adapters.copilot import default_copilot_bourdon_dir, init_memory_file

    h = home or Path.home()
    target_dir = default_copilot_bourdon_dir() if not home else h / ".copilot-bourdon"
    target = target_dir / "memory.md"
    if target.is_file():
        return None
    if dry_run:
        return target
    return init_memory_file(copilot_dir=target_dir, force=False)


def init_cascade_memory_if_missing(
    *,
    dry_run: bool = False,
    home: Optional[Path] = None,
) -> Optional[Path]:
    """Create ``~/.cascade-bourdon/memory.md`` template if missing."""
    from adapters.cascade import default_cascade_dir, init_memory_file as cascade_init

    h = home or Path.home()
    target_dir = default_cascade_dir() if not home else h / ".cascade-bourdon"
    target = target_dir / "memory.md"
    if target.is_file():
        return None
    if dry_run:
        return target
    return cascade_init(cascade_dir=target_dir, force=False)


def init_cursor_automations_if_missing(
    *,
    dry_run: bool = False,
    home: Optional[Path] = None,
) -> Optional[Path]:
    """Create ``~/.cursor/automations/cursor-cloud-agent/`` starter if missing.

    Returns the automation directory path if created, None if already present.
    """
    from adapters.cursor_automations import default_cursor_automations_dir, init_automations_dir

    h = home or Path.home()
    base = default_cursor_automations_dir() if not home else h / ".cursor" / "automations"
    target = base / "cursor-cloud-agent"
    if target.is_dir():
        return None
    if dry_run:
        return target
    return init_automations_dir(automations_dir=base, automation_id="cursor-cloud-agent")


# ---------------------------------------------------------------------------
# Plan + outcome
# ---------------------------------------------------------------------------


@dataclass
class SetupChoices:
    """User-supplied or default answers, decoupled from the prompt loop."""
    library_path: Path
    wire_claude_code_hook: bool = True
    init_copilot: bool = False
    init_cascade: bool = False
    init_cursor_automations: bool = True
    run_codex_sync: bool = True
    run_initial_export: bool = True


@dataclass
class SetupOutcome:
    """What the wizard actually did. Each field is a one-line summary."""
    detected: list[AgentDetection]
    library_created: bool
    claude_code_hook_wired: Optional[bool] = None  # None = skipped
    copilot_init: Optional[Path] = None
    cascade_init: Optional[Path] = None
    cursor_automations_init: Optional[Path] = None
    codex_sync_ran: Optional[bool] = None  # None = skipped, True/False = ran with success/fail
    initial_export_ran: Optional[bool] = None
    notes: list[str] = field(default_factory=list)


def apply_choices(
    detected: list[AgentDetection],
    choices: SetupChoices,
    *,
    dry_run: bool = False,
    home: Optional[Path] = None,
    bourdon_binary: Optional[str] = None,
) -> SetupOutcome:
    """Execute the user's choices against the filesystem.

    All filesystem mutations live here -- no prompts, no stdout. Returns
    a SetupOutcome describing what changed. The CLI handler is responsible
    for rendering the outcome to the user.
    """
    outcome = SetupOutcome(detected=detected, library_created=False)

    # 1. agent-library/
    outcome.library_created = ensure_agent_library(choices.library_path, dry_run=dry_run)

    # 2. Claude Code SessionEnd hook
    cc_present = any(a.id == "claude-code" and a.present for a in detected)
    if cc_present and choices.wire_claude_code_hook:
        settings_path = claude_code_settings_path(home)
        binary = bourdon_binary or resolve_bourdon_binary()
        outcome.claude_code_hook_wired = wire_claude_code_hook(
            settings_path, binary, dry_run=dry_run
        )
    elif cc_present:
        outcome.notes.append("Skipped Claude Code SessionEnd hook (--no-hook chosen)")

    # 3. Copilot memory init
    cop_present = any(a.id == "copilot" and a.present for a in detected)
    if choices.init_copilot and not cop_present:
        outcome.copilot_init = init_copilot_memory_if_missing(dry_run=dry_run, home=home)

    # 4. Cascade memory init
    cas_present = any(a.id == "cascade" and a.present for a in detected)
    if choices.init_cascade and not cas_present:
        outcome.cascade_init = init_cascade_memory_if_missing(dry_run=dry_run, home=home)

    # 4b. Cursor automations init
    cur_present = any(a.id == "cursor" and a.present for a in detected)
    if choices.init_cursor_automations and cur_present:
        outcome.cursor_automations_init = init_cursor_automations_if_missing(
            dry_run=dry_run, home=home
        )

    # 5. Initial export-all
    if choices.run_initial_export and not dry_run:
        outcome.initial_export_ran = _run_bourdon_subprocess(
            ["export-all"], bourdon_binary=bourdon_binary
        )
    elif choices.run_initial_export and dry_run:
        outcome.initial_export_ran = True

    # 6. Codex sync-native --from-library
    codex_present = any(a.id == "codex" and a.present for a in detected)
    if choices.run_codex_sync and codex_present and not dry_run:
        outcome.codex_sync_ran = _run_bourdon_subprocess(
            [
                "codex",
                "sync-native",
                "--from-library",
                "--memory-md",
                "--write",
            ],
            bourdon_binary=bourdon_binary,
        )
    elif choices.run_codex_sync and codex_present and dry_run:
        outcome.codex_sync_ran = True

    return outcome


def _run_bourdon_subprocess(
    argv: list[str], *, bourdon_binary: Optional[str] = None
) -> bool:
    """Invoke a Bourdon subcommand. Returns True on returncode == 0."""
    binary = bourdon_binary or resolve_bourdon_binary()
    cmd = [binary, *argv]
    try:
        completed = subprocess.run(cmd, check=False)
        return completed.returncode == 0
    except FileNotFoundError:
        return False


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def render_detection(detected: list[AgentDetection], stream=sys.stdout) -> None:
    print("Detected agents on this machine:", file=stream)
    for a in detected:
        mark = "[OK]" if a.present else " -  "
        print(f"  {mark}  {a.label:<22}  {a.hint_path}", file=stream)
    print(file=stream)


def render_outcome(
    outcome: SetupOutcome, *, dry_run: bool = False, stream=sys.stdout
) -> None:
    prefix = "(dry-run) " if dry_run else ""
    print(f"\n{prefix}setup outcome:", file=stream)
    print(
        f"  agent-library: {'created' if outcome.library_created else 'already present'}",
        file=stream,
    )
    if outcome.claude_code_hook_wired is True:
        print("  claude-code SessionEnd hook: wired", file=stream)
    elif outcome.claude_code_hook_wired is False:
        print("  claude-code SessionEnd hook: already wired", file=stream)
    if outcome.copilot_init:
        print(f"  copilot memory: initialized at {outcome.copilot_init}", file=stream)
    if outcome.cascade_init:
        print(f"  cascade memory: initialized at {outcome.cascade_init}", file=stream)
    if outcome.cursor_automations_init:
        print(
            f"  cursor automations: initialized at {outcome.cursor_automations_init}",
            file=stream,
        )
    if outcome.initial_export_ran is True:
        print("  initial export-all: ok", file=stream)
    elif outcome.initial_export_ran is False:
        print("  initial export-all: failed (run manually to diagnose)", file=stream)
    if outcome.codex_sync_ran is True:
        print("  codex sync-native --from-library --memory-md: ok", file=stream)
    elif outcome.codex_sync_ran is False:
        print("  codex sync-native --from-library --memory-md: failed", file=stream)
    for note in outcome.notes:
        print(f"  note: {note}", file=stream)

    print("\nNext steps:", file=stream)
    print("  - `bourdon doctor`            -- check current adapter health", file=stream)
    print("  - `bourdon export-all`        -- refresh all manifests anytime", file=stream)
    print(
        "  - `bourdon sync push <dest>`  -- distribute the library to another machine",
        file=stream,
    )
    print(
        "  - Open your IDE and ask 'what am I currently working on' to see"
        " federation context surface.",
        file=stream,
    )


# ---------------------------------------------------------------------------
# Interactive prompts
# ---------------------------------------------------------------------------


def _prompt_yes_no(question: str, default: bool, *, stream=sys.stdout) -> bool:
    """Y/n prompt. Returns the user's choice or the default on empty input."""
    indicator = "[Y/n]" if default else "[y/N]"
    while True:
        print(f"{question} {indicator} ", end="", file=stream, flush=True)
        try:
            answer = input().strip().lower()
        except EOFError:
            return default
        if not answer:
            return default
        if answer in {"y", "yes"}:
            return True
        if answer in {"n", "no"}:
            return False


def _prompt_path(question: str, default: Path, *, stream=sys.stdout) -> Path:
    """Path prompt with default. Returns the user's input or the default."""
    print(f"{question} [{default}] ", end="", file=stream, flush=True)
    try:
        answer = input().strip()
    except EOFError:
        return default
    return Path(answer).expanduser() if answer else default


def collect_choices_interactive(
    detected: list[AgentDetection],
    default_library: Path,
    *,
    stream=sys.stdout,
) -> SetupChoices:
    """Drive the wizard's question loop, returning a SetupChoices."""
    library = _prompt_path(
        "[1/5] Where should the agent-library live?", default_library, stream=stream
    )
    cc_present = any(a.id == "claude-code" and a.present for a in detected)
    cox_present = any(a.id == "codex" and a.present for a in detected)
    cop_present = any(a.id == "copilot" and a.present for a in detected)
    cas_present = any(a.id == "cascade" and a.present for a in detected)

    wire_hook = (
        cc_present
        and _prompt_yes_no(
            "[2/5] Wire SessionEnd hook in Claude Code so manifests auto-update?",
            default=True,
            stream=stream,
        )
    )
    init_cop = (
        not cop_present
        and _prompt_yes_no(
            "[3/5] Initialize Copilot convention-file memory at ~/.copilot-bourdon/?",
            default=False,
            stream=stream,
        )
    )
    init_cas = (
        not cas_present
        and _prompt_yes_no(
            "[4/5] Initialize Cascade convention-file memory at ~/.cascade-bourdon/?",
            default=False,
            stream=stream,
        )
    )
    sync_codex = (
        cox_present
        and _prompt_yes_no(
            "[5/5] Run `bourdon codex sync-native --from-library --memory-md --write` now?",
            default=True,
            stream=stream,
        )
    )

    return SetupChoices(
        library_path=library,
        wire_claude_code_hook=wire_hook,
        init_copilot=init_cop,
        init_cascade=init_cas,
        run_codex_sync=sync_codex,
        run_initial_export=True,
    )


# ---------------------------------------------------------------------------
# CLI handler
# ---------------------------------------------------------------------------


def handle_setup(args: argparse.Namespace) -> int:
    """``bourdon setup`` entry point."""
    from core.l6_store import DEFAULT_LIBRARY_PATH

    home: Optional[Path] = None  # honor real $HOME

    detected = detect_agents(home=home)
    render_detection(detected)

    if not any(a.present for a in detected):
        print(
            "No supported agents found in conventional paths. You can still "
            "set up ~/agent-library/ for future use.",
        )

    library_path = (
        Path(args.library_path).expanduser()
        if getattr(args, "library_path", None)
        else DEFAULT_LIBRARY_PATH
    )

    if args.non_interactive:
        choices = SetupChoices(
            library_path=library_path,
            wire_claude_code_hook=any(a.id == "claude-code" and a.present for a in detected),
            init_copilot=False,
            init_cascade=False,
            init_cursor_automations=any(a.id == "cursor" and a.present for a in detected),
            run_codex_sync=any(a.id == "codex" and a.present for a in detected),
            run_initial_export=True,
        )
    else:
        choices = collect_choices_interactive(detected, library_path)

    outcome = apply_choices(
        detected,
        choices,
        dry_run=args.dry_run,
        home=home,
    )
    render_outcome(outcome, dry_run=args.dry_run)
    return 0


def add_setup_parser(subparsers: argparse._SubParsersAction) -> None:
    """Register the ``setup`` subcommand with the top-level parser."""
    setup_cmd = subparsers.add_parser(
        "setup",
        help=(
            "Interactive post-install wizard: detect agents, init ~/agent-library/, "
            "wire SessionEnd hooks, run first export."
        ),
    )
    setup_cmd.add_argument(
        "--library-path",
        help="Where to put ~/agent-library/ (default: $HOME/agent-library).",
    )
    setup_cmd.add_argument(
        "--non-interactive",
        action="store_true",
        help="Use defaults for every prompt; do not read stdin.",
    )
    setup_cmd.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what setup would do without changing the filesystem.",
    )
    setup_cmd.set_defaults(func=handle_setup)


__all__ = [
    "AgentDetection",
    "SetupChoices",
    "SetupOutcome",
    "add_setup_parser",
    "apply_choices",
    "claude_code_settings_path",
    "collect_choices_interactive",
    "detect_agents",
    "ensure_agent_library",
    "handle_setup",
    "init_cascade_memory_if_missing",
    "init_copilot_memory_if_missing",
    "init_cursor_automations_if_missing",
    "is_claude_code_hook_wired",
    "render_detection",
    "render_outcome",
    "resolve_bourdon_binary",
    "wire_claude_code_hook",
]
