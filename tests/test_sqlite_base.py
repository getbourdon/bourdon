"""Tests for participants._sqlite_base -- shared SQLite + normalization helpers."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from participants._sqlite_base import (
    SqliteReadMixin,
    epoch_to_iso_date,
    epoch_to_iso_datetime,
    friendly_label,
    open_readonly,
    project_key_from_cwd,
    table_columns,
    table_exists,
    try_open_readonly,
)


def _make_db(path: Path) -> None:
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE t(id INTEGER, name TEXT, ts REAL)")
    db.execute("INSERT INTO t VALUES(1, 'a', 1782379919.0)")
    db.commit()
    db.close()


# ---- read-only connection ---------------------------------------------------


def test_open_readonly_can_read(tmp_path: Path) -> None:
    db_path = tmp_path / "x.db"
    _make_db(db_path)
    conn = open_readonly(db_path)
    try:
        row = conn.execute("SELECT name FROM t WHERE id=1").fetchone()
        assert row["name"] == "a"
    finally:
        conn.close()


def test_open_readonly_default_reads_concurrent_commits(tmp_path: Path) -> None:
    """The default (immutable=False) opens plain ``mode=ro``, so a row a live
    writer commits *after* the read connection opens is still visible. Opening
    with ``immutable=True`` would pin the main db file and ignore the WAL, missing
    the later commit -- exactly the stale-read hazard the default avoids."""
    db_path = tmp_path / "live.db"
    writer = sqlite3.connect(db_path)
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("CREATE TABLE t(id INTEGER)")
    writer.execute("INSERT INTO t VALUES(1)")
    writer.commit()

    ro = open_readonly(db_path)  # default: immutable=False
    writer.execute("INSERT INTO t VALUES(2)")  # committed AFTER ro opened
    writer.commit()
    try:
        seen = {r[0] for r in ro.execute("SELECT id FROM t ORDER BY id").fetchall()}
    finally:
        ro.close()
        writer.close()
    assert seen == {1, 2}


def test_open_readonly_rejects_writes(tmp_path: Path) -> None:
    db_path = tmp_path / "x.db"
    _make_db(db_path)
    conn = open_readonly(db_path)
    try:
        with __import__("pytest").raises(sqlite3.OperationalError):
            conn.execute("INSERT INTO t VALUES(2, 'b', 0)")
    finally:
        conn.close()


def test_try_open_readonly_missing_returns_none(tmp_path: Path) -> None:
    assert try_open_readonly(tmp_path / "nope.db") is None


def test_try_open_readonly_corrupt_returns_none(tmp_path: Path) -> None:
    bad = tmp_path / "bad.db"
    bad.write_text("not a database at all")
    # immutable=1 may defer the error to first query; force a read.
    conn = try_open_readonly(bad)
    if conn is not None:
        try:
            import pytest

            with pytest.raises(sqlite3.DatabaseError):
                conn.execute("SELECT 1 FROM sqlite_master").fetchall()
        finally:
            conn.close()


# ---- schema probes ----------------------------------------------------------


def test_table_exists_and_columns(tmp_path: Path) -> None:
    db_path = tmp_path / "x.db"
    _make_db(db_path)
    conn = open_readonly(db_path)
    try:
        assert table_exists(conn, "t")
        assert not table_exists(conn, "missing")
        assert table_columns(conn, "t") == {"id", "name", "ts"}
        assert table_columns(conn, "missing") == set()
    finally:
        conn.close()


# ---- normalization ----------------------------------------------------------


def test_epoch_to_iso_date() -> None:
    ts = datetime(2026, 6, 26, 12, 0, tzinfo=timezone.utc).timestamp()
    assert epoch_to_iso_date(ts) == "2026-06-26"
    assert epoch_to_iso_date(None) is None
    assert epoch_to_iso_date("not a number") is None


def test_epoch_to_iso_datetime() -> None:
    ts = datetime(2026, 6, 26, 12, 0, tzinfo=timezone.utc).timestamp()
    out = epoch_to_iso_datetime(ts)
    assert out is not None and out.startswith("2026-06-26T12:00")
    assert epoch_to_iso_datetime(None) is None


def test_project_key_from_cwd() -> None:
    generic = frozenset({"root", "home"})
    assert project_key_from_cwd("/projects/bourdon", generic_names=generic) == "bourdon"
    assert project_key_from_cwd("/root", generic_names=generic) is None
    assert project_key_from_cwd("", generic_names=generic) is None
    assert project_key_from_cwd(None, generic_names=generic) is None
    # No generic set -> any non-empty basename is a key.
    assert project_key_from_cwd("/root") == "root"


def test_friendly_label() -> None:
    assert friendly_label("my-cool_project") == "My Cool Project"
    assert friendly_label("bourdon") == "Bourdon"


# ---- mixin ------------------------------------------------------------------


def test_mixin_connect(tmp_path: Path) -> None:
    db_path = tmp_path / "x.db"
    _make_db(db_path)

    class P(SqliteReadMixin):
        def __init__(self, p: Path) -> None:
            self._db_path = p

    p = P(db_path)
    conn = p._connect()
    assert conn is not None
    try:
        assert p._table_exists(conn, "t")
        assert p._columns(conn, "t") == {"id", "name", "ts"}
    finally:
        conn.close()


def test_mixin_connect_none_when_no_path() -> None:
    class P(SqliteReadMixin):
        pass

    assert P()._connect() is None
