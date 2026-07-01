"""
Bourdon live-session presence — ephemeral, real-time session liveness.

This is deliberately SEPARATE from the durable L5 manifests. An L5 manifest
answers "what has this agent done?" (durable memory). Presence answers "which
agent SESSIONS are alive right now?" — a fundamentally ephemeral question that
does not belong in the ``BourdonParticipant`` export path (most participants are
read-post-hoc and cannot heartbeat).

Design — a directory of tiny per-session files, no daemon, no locks:

    ~/.bourdon/presence/<agent_id>__<session_id>.json   (mode 0600)

A session ``register``\\s on start, ``heartbeat``\\s each turn, and
``deregister``\\s on clean exit. Because **each session only ever writes its own
file**, concurrent sessions never contend — there is no lock to get wrong.

Liveness is derived from ``last_heartbeat`` + a TTL. A crashed session (which
never deregisters) simply goes stale once its heartbeat ages past the TTL and is
then ignored — and reaped lazily on the next read. The TTL is generous
(default 1h) because clean exits are handled by ``deregister``; the TTL only
has to catch crashes, and an idle-but-live interactive session (waiting on the
user between prompts) must not be declared dead.

The reader (``core.agents_export``) joins ``live_sessions_by_agent`` onto the
``bourdon.agents/v1`` tray contract so each agent row can show live_count and,
per session, {instance, host, project, started_at, age_s}.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import socket
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# --- constants ---------------------------------------------------------------

#: Directory (under ~/.bourdon) holding one JSON file per live session.
PRESENCE_DIRNAME = "presence"

#: Default liveness window in seconds. Generous on purpose: clean exits are
#: handled by deregister(), so the TTL only needs to reap CRASHED sessions and
#: must not kill an idle-but-live interactive session between user turns.
#: Override with the BOURDON_PRESENCE_TTL env var.
DEFAULT_TTL_SECONDS = 3600

#: Files older than TTL * this are deleted opportunistically on read (lazy GC),
#: so a machine that crashes often does not accumulate presence files forever.
REAP_MULTIPLIER = 3

#: Filename-token sanitizer. agent_id / session_id arrive from hook stdin, so we
#: constrain them to a safe charset (defense-in-depth against path traversal).
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")


# --- paths -------------------------------------------------------------------


def bourdon_home() -> Path:
    """Return ~/.bourdon, honoring BOURDON_HOME for tests/relocation."""
    override = os.environ.get("BOURDON_HOME")
    if override:
        return Path(override)
    return Path.home() / ".bourdon"


def presence_dir() -> Path:
    """Return the presence directory (~/.bourdon/presence). Not created here."""
    return bourdon_home() / PRESENCE_DIRNAME


def _safe_token(token: str) -> str:
    """Sanitize an id for use in a filename. Empty input becomes 'unknown'."""
    cleaned = _UNSAFE.sub("-", (token or "").strip())
    return cleaned or "unknown"


def _session_file(agent_id: str, session_id: str) -> Path:
    return presence_dir() / f"{_safe_token(agent_id)}__{_safe_token(session_id)}.json"


def _ttl_seconds(override: int | None = None) -> int:
    if override is not None:
        return override
    raw = os.environ.get("BOURDON_PRESENCE_TTL")
    if raw:
        try:
            return max(1, int(raw))
        except ValueError:
            logger.warning("Invalid BOURDON_PRESENCE_TTL=%r; using default", raw)
    return DEFAULT_TTL_SECONDS


# --- time helpers ------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    # A tz-naive timestamp (hand-edited file / foreign writer) would raise
    # TypeError on `now - hb` and, unguarded, blank ALL presence. Force UTC.
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


# --- write / delete ----------------------------------------------------------


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    """Durable atomic write: unique tmp (mkstemp, 0600) + fsync + os.replace,
    then fsync the parent dir so the rename survives a crash.

    A UNIQUE tmp per writer (not a fixed ``<name>.tmp``) is what makes concurrent
    writes to the SAME session_id safe: there is no shared inode to interleave,
    and each rename is independent (last-writer-wins) instead of one writer's
    rename yanking the tmp out from under the other. The parent dir is created
    0700 and the tmp is 0600 from birth (``mkstemp``), so a local co-tenant never
    sees presence content — not even the brief pre-chmod window the old code had.
    """
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with contextlib.suppress(OSError):
        os.chmod(path.parent, 0o700)  # tighten if the dir pre-existed looser
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=path.name + ".", suffix=".tmp"
    )
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        # Durability: fsync the directory so the rename is on disk after a crash.
        with contextlib.suppress(OSError):
            dir_fd = os.open(str(path.parent), os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
    except OSError:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise


def _project_from_cwd(cwd: str | None) -> str | None:
    """Reduce a cwd to a low-PII project label (its basename)."""
    if not cwd:
        return None
    name = os.path.basename(os.path.normpath(cwd))
    return name or None


def register(
    agent_id: str,
    session_id: str,
    *,
    cwd: str | None = None,
    pid: int | None = None,
    host: str | None = None,
) -> Path:
    """Record a session as live. Idempotent: re-registering an existing session
    preserves its original ``started_at`` and just refreshes the heartbeat."""
    path = _session_file(agent_id, session_id)
    existing = _read_one(path) or {}
    now = _now()
    started_at = existing.get("started_at") or _iso(now)
    record = {
        "agent_id": agent_id,
        "session_id": session_id,
        "host": host or socket.gethostname(),
        "project": _project_from_cwd(cwd),
        "pid": pid if pid is not None else os.getpid(),
        "started_at": started_at,
        "last_heartbeat": _iso(now),
    }
    _atomic_write_json(path, record)
    return path


def heartbeat(
    agent_id: str, session_id: str, *, cwd: str | None = None
) -> Path:
    """Refresh a session's liveness. Self-heals: if the file is missing (e.g. a
    heartbeat arrived before/without a register), it creates it."""
    path = _session_file(agent_id, session_id)
    existing = _read_one(path)
    if existing is None:
        return register(agent_id, session_id, cwd=cwd)
    existing["last_heartbeat"] = _iso(_now())
    if cwd:
        existing["project"] = _project_from_cwd(cwd)
    _atomic_write_json(path, existing)
    return path


def deregister(agent_id: str, session_id: str) -> bool:
    """Remove a session's presence file. Returns True if a file was removed.
    Missing file is not an error (idempotent clean-exit)."""
    path = _session_file(agent_id, session_id)
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False
    except OSError as e:
        logger.warning("Failed to deregister %s: %s", path, e)
        return False


# --- read --------------------------------------------------------------------


def _read_one(path: Path) -> dict[str, Any] | None:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return None
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Unreadable presence file %s: %s", path, e)
        return None
    return data if isinstance(data, dict) else None


def _summarize(record: dict[str, Any], now: datetime) -> dict[str, Any]:
    """Shape one raw presence record into the tray-facing live-session dict.

    The full ``session_id`` is intentionally NOT emitted — only the 8-char
    ``instance`` the tray renders — so a full id never egresses to a peer. The
    age is clamped at 0 so a future-dated / clock-skewed heartbeat can't render
    a negative age.
    """
    hb = _parse_iso(record.get("last_heartbeat"))
    age_s = max(0, int((now - hb).total_seconds())) if hb else None
    session_id = str(record.get("session_id") or "")
    return {
        "instance": session_id[:8] or None,
        "host": record.get("host"),
        "project": record.get("project"),
        "started_at": record.get("started_at"),
        "age_s": age_s,
    }


def _iter_live_records(
    ttl: int, now: datetime, reap: bool
) -> list[tuple[str, dict[str, Any]]]:
    """Return ``(agent_id, record)`` for every LIVE session, reaping hard-stale.

    Shared by :func:`live_sessions` and :func:`live_sessions_by_agent` so BOTH
    read paths reap — the tray reads via ``live_sessions_by_agent``, which
    previously never reaped, so crashed-session files accumulated forever.

    A session is live iff its ``last_heartbeat`` is within ``ttl`` (a small
    future skew is tolerated; a wildly-future heartbeat is treated as stale, not
    immortal). Files hard-stale in either direction (> TTL * REAP_MULTIPLIER) are
    deleted opportunistically. A single malformed file (bad timestamp,
    unreadable) is skipped, never aborting the whole scan.
    """
    directory = presence_dir()
    out: list[tuple[str, dict[str, Any]]] = []
    if not directory.is_dir():
        return out
    try:
        paths = sorted(directory.glob("*.json"))
    except OSError:
        return out

    reap_window = ttl * REAP_MULTIPLIER
    for path in paths:
        try:
            record = _read_one(path)
            if record is None:
                continue
            hb = _parse_iso(record.get("last_heartbeat"))
            age = (now - hb).total_seconds() if hb else None
            live = age is not None and -ttl <= age <= ttl
            if not live:
                if reap and (age is None or abs(age) > reap_window):
                    with contextlib.suppress(OSError):
                        path.unlink()
                continue
            out.append((str(record.get("agent_id") or "").strip(), record))
        except Exception:  # noqa: BLE001 -- one bad file must not abort the scan
            logger.warning("Skipping unusable presence file %s", path, exc_info=True)
            continue
    return out


def live_sessions(
    ttl_seconds: int | None = None,
    *,
    now: datetime | None = None,
    reap: bool = True,
) -> list[dict[str, Any]]:
    """Return all live sessions across every agent, newest-heartbeat first."""
    ttl = _ttl_seconds(ttl_seconds)
    now = now or _now()
    records = [rec for _agent_id, rec in _iter_live_records(ttl, now, reap)]
    records.sort(key=lambda r: str(r.get("last_heartbeat") or ""), reverse=True)
    return [_summarize(r, now) for r in records]


def live_sessions_by_agent(
    ttl_seconds: int | None = None,
    *,
    now: datetime | None = None,
    reap: bool = True,
) -> dict[str, list[dict[str, Any]]]:
    """Group live sessions by ``agent_id`` for the tray-contract join."""
    ttl = _ttl_seconds(ttl_seconds)
    now = now or _now()
    grouped: dict[str, list[dict[str, Any]]] = {}
    for agent_id, record in _iter_live_records(ttl, now, reap):
        if not agent_id:
            continue
        grouped.setdefault(agent_id, []).append(_summarize(record, now))
    for sessions in grouped.values():
        sessions.sort(key=lambda s: str(s.get("started_at") or ""), reverse=False)
    return grouped
