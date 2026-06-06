"""
Bourdon L5 I/O -- atomic write + read helpers for L5 manifests.

Writing is done via tmp-file + atomic rename so readers (e.g., the L6Store
file watcher in future versions) never observe a half-written manifest.
On POSIX and on Windows/NTFS, ``Path.replace`` is atomic within the same
filesystem, so this pattern is safe for the common case of writing into
``~/agent-library/agents/`` on local disk.

Minimal surface -- most of the heavy lifting lives in callers (participants
build L5Manifest objects; the L6 server reads them back through L6Store).
This module is only the boundary between in-memory and on-disk form.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

import yaml

from participants.base import L5Manifest

logger = logging.getLogger(__name__)


def write_l5_dict(manifest: dict[str, Any], path: Path) -> None:
    """
    Atomically write a manifest dict to ``path``.

    Writes to ``<path>.tmp`` first, then renames into place. The rename is
    atomic on the same filesystem, so concurrent readers never see partial
    content. Creates parent directories if missing.

    Raises
    ------
    OSError
        If the write or rename fails. Callers should treat this as a hard
        error (filesystem full, permissions denied, etc.) and propagate.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(manifest, f, sort_keys=False, default_flow_style=False)
            f.flush()
            os.fsync(f.fileno())
        tmp_path.replace(path)
    except OSError:
        # Best-effort cleanup of the tmp file on failure
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            pass
        raise


def write_l5(manifest: L5Manifest, path: Path) -> None:
    """
    Atomically write an :class:`L5Manifest` object to ``path``.

    Serializes via ``manifest.to_dict()`` which applies the
    drop-None-and-empty-list cleanup rules from the dataclass, then hands
    off to :func:`write_l5_dict`.
    """
    write_l5_dict(manifest.to_dict(), path)


def read_l5_dict(path: Path) -> dict[str, Any] | None:
    """
    Read an L5 manifest from ``path``. Returns None on any failure.

    Intentionally lenient: returns None instead of raising so callers can
    treat "no manifest yet" the same as "couldn't parse manifest" without
    branching. Logs at WARNING so real problems are visible.
    """
    path = Path(path)
    if not path.is_file():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except (yaml.YAMLError, OSError) as e:
        logger.warning("Failed to read L5 manifest %s: %s", path, e)
        return None
    if not isinstance(data, dict):
        logger.warning("L5 manifest %s is not a dict, treating as empty", path)
        return None
    return data
