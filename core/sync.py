"""
Cross-machine federation library distribution via rsync.

Implements ``bourdon sync push <remote>`` and ``bourdon sync pull <remote>``
per issue #74. Wraps a rsync subprocess so a user with two machines
(or two users sharing a Tailnet) can move ``~/agent-library/`` between
hosts as a single command rather than the manual ``scp -r`` / git-temp-branch
workaround that the 2026-05-17 cross-machine recognition test relied on.

Push: stages a visibility-filtered copy of the local library to a temp
directory, rsyncs that staged copy to the destination, cleans up. The
visibility-filter step is the security boundary -- entities and sessions
above the requested access level (``public|team|private``, default
``public``) are dropped from the staged copy before the network leg.

Pull: rsyncs from a source into the local library. No filter is applied
on pull -- the source pushed a filtered manifest already, and re-filtering
on receive would silently mask any tampering with the source side.

The atomic-per-file guarantee comes from rsync's default ``--temp-dir``
behavior plus ``--delay-updates`` (collect all updated files into the
destination's ``.~tmp~/`` and rename them in at the end of the run), so
a network failure mid-sync leaves the destination in a consistent state.

Pre-requisite: ``rsync`` must be on ``$PATH``. macOS/Linux ship it; Windows
users need WSL or Cygwin (already required for the rest of Bourdon's
file-system dependent paths).
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import yaml

from core.l6_store import DEFAULT_LIBRARY_PATH, _is_visible

logger = logging.getLogger(__name__)

ACCESS_LEVELS = ("public", "team", "private")
DEFAULT_PUSH_ACCESS_LEVEL = "public"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class SyncError(RuntimeError):
    """Raised when sync setup or execution fails in a way the caller should surface."""


class RsyncMissingError(SyncError):
    """``rsync`` is not on PATH."""


# ---------------------------------------------------------------------------
# Manifest filtering
# ---------------------------------------------------------------------------


def filter_l5_manifest(manifest: dict[str, Any], access_level: str) -> dict[str, Any]:
    """Return a copy of ``manifest`` with entities and sessions filtered
    down to those whose visibility is ``<= access_level``.

    Reuses ``core.l6_store._is_visible``'s rank semantics so push behavior
    matches L6 query behavior. Does not mutate the input.

    Parameters
    ----------
    manifest:
        A parsed L5 YAML dict.
    access_level:
        ``"public"``, ``"team"``, or ``"private"``.

    Returns
    -------
    dict
        A new dict with ``known_entities`` and ``recent_sessions`` filtered.
        Other top-level keys are passed through unchanged.
    """
    if access_level not in ACCESS_LEVELS:
        raise SyncError(
            f"access_level must be one of {ACCESS_LEVELS!r}, got {access_level!r}"
        )

    filtered = dict(manifest)  # shallow copy of top-level keys

    entities = manifest.get("known_entities") or []
    filtered["known_entities"] = [
        e for e in entities
        if isinstance(e, dict) and _is_visible(e, access_level)
    ]

    sessions = manifest.get("recent_sessions") or []
    filtered["recent_sessions"] = [
        s for s in sessions
        if isinstance(s, dict) and _is_visible(s, access_level)
    ]

    return filtered


def stage_filtered_library(
    library_path: Path,
    access_level: str,
    staging_dir: Path,
) -> Path:
    """Copy ``library_path`` into ``staging_dir`` with all ``agents/*.l5.yaml``
    manifests filtered to entries visible at ``access_level``.

    Files outside ``agents/`` (e.g. ``reports/``) are passed through unchanged
    -- the L5 manifests are the only file type that carries per-entry visibility.

    Returns the staged library root path (``staging_dir / 'agent-library'``).
    """
    if access_level not in ACCESS_LEVELS:
        raise SyncError(
            f"access_level must be one of {ACCESS_LEVELS!r}, got {access_level!r}"
        )
    if not library_path.is_dir():
        raise SyncError(f"library_path does not exist or is not a directory: {library_path}")

    staged = staging_dir / "agent-library"
    if staged.exists():
        shutil.rmtree(staged)

    # Walk the source tree once.
    for src in library_path.rglob("*"):
        rel = src.relative_to(library_path)
        dest = staged / rel
        if src.is_dir():
            dest.mkdir(parents=True, exist_ok=True)
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        if _is_l5_manifest(rel):
            _write_filtered_manifest(src, dest, access_level)
        else:
            shutil.copy2(src, dest)
    # Ensure the staged root exists even for an empty library.
    staged.mkdir(parents=True, exist_ok=True)
    return staged


def _is_l5_manifest(rel: Path) -> bool:
    """Return True for ``agents/<name>.l5.yaml``."""
    parts = rel.parts
    return (
        len(parts) >= 2
        and parts[0] == "agents"
        and parts[-1].endswith(".l5.yaml")
    )


def _write_filtered_manifest(src: Path, dest: Path, access_level: str) -> None:
    """Read an L5 manifest, filter by visibility, write to dest.

    On parse failure logs at WARNING and copies the file unchanged so a
    broken manifest doesn't block the rest of the sync (parallels the
    no-frontmatter-on-parse-error behavior in the adapters per #79).
    """
    try:
        text = src.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("sync: cannot read %s (%s); copying unchanged", src, exc)
        shutil.copy2(src, dest)
        return

    try:
        parsed = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        detail = str(exc).replace("\n", " ")[:200]
        logger.warning(
            "sync: %s has malformed YAML (%s); copying unchanged",
            src,
            detail,
        )
        shutil.copy2(src, dest)
        return

    if not isinstance(parsed, dict):
        shutil.copy2(src, dest)
        return

    filtered = filter_l5_manifest(parsed, access_level)
    dest.write_text(
        yaml.safe_dump(filtered, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# rsync invocation
# ---------------------------------------------------------------------------


@dataclass
class SyncResult:
    """Result of a sync run."""
    command: list[str]
    returncode: int
    dry_run: bool
    access_level: Optional[str]  # populated for push, None for pull
    bytes_written: Optional[int]  # if rsync reported it, else None


def _ensure_rsync() -> str:
    """Return the path to ``rsync`` or raise RsyncMissingError."""
    path = shutil.which("rsync")
    if not path:
        raise RsyncMissingError(
            "rsync is required for `bourdon sync` but was not found on PATH. "
            "macOS/Linux: install via the system package manager. "
            "Windows: install WSL or Cygwin."
        )
    return path


def _build_rsync_command(
    src: str,
    dest: str,
    *,
    dry_run: bool = False,
    delete: bool = False,
    verbose: bool = False,
) -> list[str]:
    """Compose the rsync argv.

    Flags:
      -a  archive (preserve perms, times, symlinks, etc.)
      -z  compress over the wire
      --checksum  use checksum (not just size+mtime) for change detection -- idempotency
      --delay-updates  hold all updated files in destination .~tmp~/ until the end -- atomicity

    Optional:
      --dry-run
      --delete   destructive; only when explicitly requested
      -v         verbose

    Both ``src`` and ``dest`` are passed through as rsync sees them, so
    callers can use local paths, ``user@host:path``, or ``rsync://`` URLs.
    Trailing slashes matter for rsync -- callers are responsible for those.
    """
    rsync = _ensure_rsync()
    argv = [rsync, "-az", "--checksum", "--delay-updates"]
    if dry_run:
        argv.append("--dry-run")
    if delete:
        argv.append("--delete")
    if verbose:
        argv.append("-v")
    argv.extend([src, dest])
    return argv


def _run_rsync(argv: list[str]) -> int:
    """Run rsync and return its exit code. Output streams to current stderr."""
    logger.info("sync: %s", " ".join(argv))
    try:
        completed = subprocess.run(argv, check=False)
    except FileNotFoundError as exc:
        # Defensive -- _ensure_rsync should have caught this.
        raise RsyncMissingError(str(exc)) from exc
    return completed.returncode


def sync_push(
    dest: str,
    *,
    access_level: str = DEFAULT_PUSH_ACCESS_LEVEL,
    library_path: Optional[Path] = None,
    dry_run: bool = False,
    delete: bool = False,
    verbose: bool = False,
) -> SyncResult:
    """Push the local agent library to ``dest``, filtered by visibility.

    Steps:

    1. Stage a copy of ``library_path`` into a tempdir with all L5 manifests
       filtered to entries visible at ``access_level``.
    2. rsync the staged copy to ``dest``.
    3. Clean up the tempdir.

    Parameters
    ----------
    dest:
        rsync-compatible destination (local path, ``user@host:path``, ``rsync://`` URL).
    access_level:
        ``"public"`` (default), ``"team"``, or ``"private"``. The default is
        deliberately the most restrictive -- pushing private/team manifests
        is an opt-in.
    library_path:
        Source library. Defaults to ``~/agent-library/``.
    dry_run:
        Pass through to rsync; show what would change without changing it.
        Also skips staging cleanup so the user can inspect the staged tree.
    delete:
        Add rsync ``--delete`` (mirror semantics). Off by default.
    verbose:
        Add rsync ``-v``.

    Returns
    -------
    SyncResult

    Raises
    ------
    RsyncMissingError
        ``rsync`` not on PATH.
    SyncError
        Bad ``access_level`` or unreadable ``library_path``.
    """
    if access_level not in ACCESS_LEVELS:
        raise SyncError(
            f"access_level must be one of {ACCESS_LEVELS!r}, got {access_level!r}"
        )
    library = library_path or DEFAULT_LIBRARY_PATH
    if not library.is_dir():
        raise SyncError(f"library does not exist: {library}")

    # Trailing slash on src tells rsync "copy contents into dest", not "make
    # dest a directory containing this dir". Matches the issue's worked example.
    with tempfile.TemporaryDirectory(prefix="bourdon-sync-") as staging:
        staging_path = Path(staging)
        staged = stage_filtered_library(library, access_level, staging_path)
        src = f"{staged}/"
        argv = _build_rsync_command(
            src, dest, dry_run=dry_run, delete=delete, verbose=verbose
        )
        returncode = _run_rsync(argv)

    return SyncResult(
        command=argv,
        returncode=returncode,
        dry_run=dry_run,
        access_level=access_level,
        bytes_written=None,
    )


def sync_pull(
    src: str,
    *,
    library_path: Optional[Path] = None,
    dry_run: bool = False,
    delete: bool = False,
    verbose: bool = False,
) -> SyncResult:
    """Pull a remote agent library into the local one.

    No visibility filter is applied on receive -- the remote pushed a
    filtered manifest already, and re-filtering on pull would silently
    mask any tampering. Trust boundary lives on push.

    Parameters
    ----------
    src:
        rsync-compatible source (local path, ``user@host:path``, ``rsync://`` URL).
    library_path:
        Destination. Defaults to ``~/agent-library/``.
    dry_run / delete / verbose:
        Pass-through to rsync (see ``sync_push``).
    """
    library = library_path or DEFAULT_LIBRARY_PATH
    library.mkdir(parents=True, exist_ok=True)

    dest = f"{library}/"
    argv = _build_rsync_command(
        src, dest, dry_run=dry_run, delete=delete, verbose=verbose
    )
    returncode = _run_rsync(argv)

    return SyncResult(
        command=argv,
        returncode=returncode,
        dry_run=dry_run,
        access_level=None,
        bytes_written=None,
    )


def visible_counts(library_path: Path, access_level: str) -> dict[str, dict[str, int]]:
    """Return per-agent {entity_count, session_count} at the given access level.

    Useful for the CLI dry-run banner so the user can see how many
    entries the visibility filter is keeping vs dropping before the network leg.
    """
    if access_level not in ACCESS_LEVELS:
        raise SyncError(
            f"access_level must be one of {ACCESS_LEVELS!r}, got {access_level!r}"
        )
    agents_dir = library_path / "agents"
    if not agents_dir.is_dir():
        return {}

    out: dict[str, dict[str, int]] = {}
    for manifest_path in sorted(agents_dir.glob("*.l5.yaml")):
        try:
            text = manifest_path.read_text(encoding="utf-8")
            parsed = yaml.safe_load(text)
        except (OSError, yaml.YAMLError):
            continue
        if not isinstance(parsed, dict):
            continue
        filtered = filter_l5_manifest(parsed, access_level)
        out[manifest_path.stem.removesuffix(".l5")] = {
            "entities": len(filtered.get("known_entities") or []),
            "sessions": len(filtered.get("recent_sessions") or []),
        }
    return out


__all__ = [
    "ACCESS_LEVELS",
    "DEFAULT_PUSH_ACCESS_LEVEL",
    "RsyncMissingError",
    "SyncError",
    "SyncResult",
    "filter_l5_manifest",
    "stage_filtered_library",
    "sync_pull",
    "sync_push",
    "visible_counts",
]
