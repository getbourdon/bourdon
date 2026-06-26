"""Shared SQLite + normalization helpers for external participants.

Several Bourdon participants (Codex, Cursor, Copilot CLI, Hermes, ...) read a
native SQLite store and normalize rows into L5. They repeat the same handful of
primitives: open the DB *read-only* so a live agent's write-lock can't block the
export, probe for tables/columns defensively (native schemas drift across
versions), and turn epoch timestamps + filesystem paths into the L5 shapes.

This module centralizes those primitives so a new SQLite-backed participant is a
thin parser over `SqliteReadMixin` instead of a copy of the boilerplate. It is
deliberately tiny and dependency-free (stdlib `sqlite3` only); richer,
agent-specific parsing stays in each participant.

Nothing here applies visibility policy or builds manifests — that is the
participant's job (and must stay there, per PARTICIPANT_CONTRACT.md).
"""

from __future__ import annotations

import logging
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


# -- Read-only connection ------------------------------------------------------


def open_readonly(db_path: Path, *, immutable: bool = False, timeout: float = 2.0) -> sqlite3.Connection:
    """Open a SQLite DB read-only via URI so a live writer is never disturbed.

    Default is plain ``mode=ro``: it still cannot write, but it respects SQLite
    locking and reads the ``-wal`` sidecar, so a live, *actively-writing* agent is
    read correctly. This matches every other participant (codex/copilot) and is
    the safe default.

    ``immutable=True`` adds ``immutable=1``, which tells SQLite the file will not
    change for the life of the connection — it skips locking entirely AND ignores
    the WAL. Only opt in for a genuinely static snapshot or read-only media;
    against a concurrently-written DB it risks stale reads or ``SQLITE_CORRUPT``.

    The connection uses ``sqlite3.Row`` so callers can index by column name.
    Raises ``sqlite3.Error`` only on genuine open failure; callers that want the
    never-raises contract should wrap with :func:`try_open_readonly`.
    """
    flags = "mode=ro&immutable=1" if immutable else "mode=ro"
    uri = f"file:{db_path}?{flags}"
    conn = sqlite3.connect(uri, uri=True, timeout=timeout)
    conn.row_factory = sqlite3.Row
    return conn


def try_open_readonly(
    db_path: Path, *, immutable: bool = False, timeout: float = 2.0
) -> Optional[sqlite3.Connection]:
    """Like :func:`open_readonly` but returns ``None`` instead of raising.

    Used on the export/health path where a missing or corrupt DB must degrade to
    "no rows" rather than crash the participant.
    """
    if not db_path.is_file():
        return None
    try:
        return open_readonly(db_path, immutable=immutable, timeout=timeout)
    except sqlite3.Error as exc:
        logger.debug("sqlite: cannot open %s read-only: %s", db_path, exc)
        return None


# -- Defensive schema probes ---------------------------------------------------


def table_exists(conn: sqlite3.Connection, name: str) -> bool:
    """True if ``name`` is a table in the connected DB."""
    try:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
            (name,),
        ).fetchone()
    except sqlite3.Error:
        return False
    return row is not None


def table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    """Column names for ``table`` (empty set if the table is absent)."""
    try:
        return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
    except sqlite3.Error:
        return set()


# -- Normalization -------------------------------------------------------------


def epoch_to_iso_date(value: Any) -> Optional[str]:
    """Convert epoch seconds (int/float/str) to an ISO ``YYYY-MM-DD`` UTC date.

    Returns ``None`` for missing or unparseable values. Many native stores keep
    timestamps as float epoch seconds; this is the L5 ``date`` shape.
    """
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(float(value), tz=timezone.utc).date().isoformat()
    except (ValueError, OSError, OverflowError):
        return None


def epoch_to_iso_datetime(value: Any) -> Optional[str]:
    """Convert epoch seconds to a full ISO 8601 UTC datetime, or ``None``."""
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat()
    except (ValueError, OSError, OverflowError):
        return None


def project_key_from_cwd(
    cwd: Optional[str], *, generic_names: frozenset[str] = frozenset()
) -> Optional[str]:
    """Derive a lowercase project key from a working-directory path.

    Returns the basename lowercased, or ``None`` when the cwd is empty or its
    basename is in ``generic_names`` (e.g. ``root``/``home``/``tmp`` — paths that
    carry no project identity). Callers pass their own ``generic_names`` set.
    """
    if not cwd:
        return None
    name = Path(cwd).name.strip().lower()
    if not name or name in generic_names:
        return None
    return name


def friendly_label(key: str) -> str:
    """Turn a slug-ish key (``my-cool_project``) into a Title Case label."""
    return re.sub(r"[-_]+", " ", key).strip().title()


# -- Mixin ---------------------------------------------------------------------


class SqliteReadMixin:
    """Convenience mixin exposing the read-only SQLite primitives as methods.

    A participant that sets ``self._db_path`` (a :class:`pathlib.Path`) gets
    ``_connect`` / ``_table_exists`` / ``_columns`` for free. Purely optional —
    the module-level functions are the real API; the mixin just saves a little
    plumbing for participants that want it.
    """

    _db_path: Optional[Path] = None

    def _connect(self) -> Optional[sqlite3.Connection]:
        if self._db_path is None:
            return None
        return try_open_readonly(self._db_path)

    @staticmethod
    def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
        return table_exists(conn, name)

    @staticmethod
    def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
        return table_columns(conn, table)
