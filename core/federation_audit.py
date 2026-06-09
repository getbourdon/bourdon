"""
Bourdon federation audit log — append-only JSONL (v0.9.0).

Every federation operation (tool call, allow or deny, on any transport) is
recorded as one line in ``~/.bourdon/audit.jsonl``::

    {"ts": "...Z", "agent": "openclaw", "op": "find_entity",
     "namespace": "claude-code", "decision": "deny", "detail": "not granted"}

Invariants:
- Append-only. Nothing in Bourdon rewrites or truncates this file.
- No token material is ever written (the enforcement layer only has the
  resolved :class:`~core.federation_registry.AgentIdentity`, never the token).
- A revoked agent's history remains queryable — revocation flips the registry
  flag, it does not touch the audit trail.
- Audit failures never break a federation call (log + continue): the audit
  log is for the operator's forensics, not a write-ahead gate.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

logger = logging.getLogger(__name__)

DEFAULT_AUDIT_PATH = Path.home() / ".bourdon" / "audit.jsonl"

DECISION_ALLOW = "allow"
DECISION_DENY = "deny"


class FederationAudit:
    """Append-only audit writer + query reader."""

    def __init__(self, path: Path | None = None) -> None:
        if path is None:
            env = os.environ.get("BOURDON_AUDIT_PATH")
            path = Path(env) if env else DEFAULT_AUDIT_PATH
        self.path = Path(path)
        self._lock = threading.Lock()

    def record(
        self,
        agent: str,
        op: str,
        namespace: str = "*",
        decision: str = DECISION_ALLOW,
        detail: str | None = None,
    ) -> None:
        entry: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            "agent": agent,
            "op": op,
            "namespace": namespace,
            "decision": decision,
        }
        if detail:
            entry["detail"] = detail
        line = json.dumps(entry, ensure_ascii=False)
        try:
            with self._lock:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
        except OSError as exc:  # noqa: PERF203 — audit must never break a call
            logger.warning("audit write failed (%s): %s", self.path, exc)

    def entries(
        self,
        agent: str | None = None,
        denials_only: bool = False,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Most-recent-last list of entries matching the filters."""
        rows = [
            e
            for e in self._iter_entries()
            if (agent is None or e.get("agent") == agent)
            and (not denials_only or e.get("decision") == DECISION_DENY)
        ]
        if limit is not None and limit >= 0:
            rows = rows[-limit:]
        return rows

    def _iter_entries(self) -> Iterator[dict[str, Any]]:
        if not self.path.exists():
            return
        with self.path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue  # tolerate a torn tail line; never abort a query
                if isinstance(entry, dict):
                    yield entry
