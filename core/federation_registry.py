"""
Bourdon federation identity registry — per-agent tokens + trust tiers (v0.9.0).

Single-operator registry at ``~/.bourdon/federation.yaml``. Each registered
federation member has:

- a **tier**: ``trusted`` (read/write per v0.8.0 behavior) or ``quarantined``
  (deny-by-default reads on granted namespaces only; writes staged),
- a **token**, generated once at ``add_agent`` time and stored only as a
  SHA-256 hash — the plaintext is shown once and never persisted or logged,
- a **grants** list: agent_id namespaces a quarantined member may read
  (a "namespace" is one agent's L5 manifest — see spec/SPEC_v0.9.0.md D1),
- a **revoked** flag: a revoked member's token authenticates nowhere, ever.

Caller identity is bound per-request via a contextvar (:func:`set_caller` /
:func:`get_caller`). stdio transport never sets it, so tools see the default
``OPERATOR`` identity — trusted, exactly v0.8.0 behavior. The HTTP auth
middleware sets it after validating the Bearer token.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import re
import secrets
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

DEFAULT_REGISTRY_PATH = Path.home() / ".bourdon" / "federation.yaml"

TIER_TRUSTED = "trusted"
TIER_QUARANTINED = "quarantined"
VALID_TIERS = (TIER_TRUSTED, TIER_QUARANTINED)

_AGENT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")

# Token prefix makes leaked tokens grep-able in secret scanners.
_TOKEN_PREFIX = "bdn_"


@dataclass(frozen=True)
class AgentIdentity:
    """Resolved caller identity for one federation request."""

    agent_id: str
    tier: str = TIER_TRUSTED
    grants: tuple[str, ...] = field(default_factory=tuple)

    @property
    def is_trusted(self) -> bool:
        return self.tier == TIER_TRUSTED

    def may_read(self, namespace: str) -> bool:
        """Whether this caller may read one agent-manifest namespace."""
        if self.is_trusted:
            return True
        return namespace in self.grants


#: The implicit identity of the operator's own process (stdio transport,
#: legacy shared-token peers). Trusted — preserves v0.8.0 behavior.
OPERATOR = AgentIdentity(agent_id="operator", tier=TIER_TRUSTED)

_caller_var: ContextVar[AgentIdentity] = ContextVar("bourdon_caller", default=OPERATOR)


def set_caller(identity: AgentIdentity):
    """Bind the caller identity for the current context. Returns the reset token."""
    return _caller_var.set(identity)


def get_caller() -> AgentIdentity:
    """Identity of the current request's caller (``OPERATOR`` on stdio)."""
    return _caller_var.get()


def reset_caller(token) -> None:
    _caller_var.reset(token)


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class RegistryError(ValueError):
    """Invalid registry operation (unknown agent, bad tier, duplicate add...)."""


class FederationRegistry:
    """Load / mutate / persist the federation member registry.

    All mutating methods persist immediately (atomic tmp+rename write).
    Reads re-load from disk when the file's mtime changed, so a running
    HTTP server picks up ``bourdon revoke`` without a restart.
    """

    def __init__(self, path: Path | None = None) -> None:
        if path is None:
            env = os.environ.get("BOURDON_FEDERATION_CONFIG")
            path = Path(env) if env else DEFAULT_REGISTRY_PATH
        self.path = Path(path)
        self._agents: dict[str, dict[str, Any]] = {}
        self._loaded_mtime: float | None = None
        self._load()

    # -- persistence -----------------------------------------------------------

    def _load(self) -> None:
        if not self.path.exists():
            self._agents = {}
            self._loaded_mtime = None
            return
        try:
            data = yaml.safe_load(self.path.read_text(encoding="utf-8")) or {}
        except Exception as exc:  # noqa: BLE001 — a corrupt registry must fail closed
            logger.error("Failed to parse federation registry %s: %s", self.path, exc)
            # Fail closed: no agents authenticate off a corrupt registry.
            self._agents = {}
            self._loaded_mtime = None
            return
        agents = data.get("agents")
        self._agents = dict(agents) if isinstance(agents, dict) else {}
        try:
            self._loaded_mtime = self.path.stat().st_mtime
        except OSError:
            self._loaded_mtime = None

    def _refresh_if_stale(self) -> None:
        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            mtime = None
        if mtime != self._loaded_mtime:
            self._load()

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": 1, "agents": self._agents}
        tmp = self.path.with_suffix(".yaml.tmp")
        tmp.write_text(yaml.safe_dump(payload, sort_keys=True), encoding="utf-8")
        os.replace(tmp, self.path)
        try:
            self._loaded_mtime = self.path.stat().st_mtime
        except OSError:
            self._loaded_mtime = None

    # -- queries ----------------------------------------------------------------

    def list_agents(self) -> dict[str, dict[str, Any]]:
        """Registry rows WITHOUT token hashes (safe to print)."""
        self._refresh_if_stale()
        out: dict[str, dict[str, Any]] = {}
        for agent_id, row in sorted(self._agents.items()):
            safe = {k: v for k, v in row.items() if k != "token_sha256"}
            safe["has_token"] = bool(row.get("token_sha256"))
            out[agent_id] = safe
        return out

    def get(self, agent_id: str) -> dict[str, Any] | None:
        self._refresh_if_stale()
        row = self._agents.get(agent_id)
        return dict(row) if row else None

    def has_active_agents(self) -> bool:
        """True when at least one non-revoked member with a token exists."""
        self._refresh_if_stale()
        return any(
            row.get("token_sha256") and not row.get("revoked")
            for row in self._agents.values()
        )

    def authenticate(self, token: str) -> AgentIdentity | None:
        """Resolve a presented Bearer token to an identity, or ``None``.

        Constant-time comparison over the stored hash. Revoked members never
        authenticate. The token value itself is never logged.
        """
        if not token:
            return None
        self._refresh_if_stale()
        presented = _hash_token(token)
        matched: AgentIdentity | None = None
        for agent_id, row in self._agents.items():
            stored = row.get("token_sha256") or ""
            # Compare against every row (no early exit) so timing doesn't
            # reveal which agent_id matched.
            if stored and hmac.compare_digest(presented, stored):
                if row.get("revoked"):
                    continue
                matched = AgentIdentity(
                    agent_id=agent_id,
                    tier=str(row.get("tier") or TIER_TRUSTED),
                    grants=tuple(row.get("grants") or ()),
                )
        return matched

    # -- mutations --------------------------------------------------------------

    def add_agent(
        self,
        agent_id: str,
        tier: str = TIER_QUARANTINED,
        grants: list[str] | None = None,
    ) -> str:
        """Register a member and return its plaintext token (shown ONCE)."""
        self._refresh_if_stale()
        if not _AGENT_ID_RE.match(agent_id or ""):
            raise RegistryError(
                f"invalid agent_id {agent_id!r}: must match ^[a-z0-9][a-z0-9_-]*$"
            )
        if tier not in VALID_TIERS:
            raise RegistryError(f"invalid tier {tier!r}: must be one of {VALID_TIERS}")
        if agent_id in self._agents and not self._agents[agent_id].get("revoked"):
            raise RegistryError(
                f"agent {agent_id!r} already registered; use `bourdon agent rotate` "
                "for a new token or `bourdon revoke` first"
            )
        token = _TOKEN_PREFIX + secrets.token_hex(24)
        self._agents[agent_id] = {
            "tier": tier,
            "token_sha256": _hash_token(token),
            "created_at": _utc_now_iso(),
            "revoked": False,
            "grants": list(grants or []),
        }
        self._save()
        return token

    def rotate_token(self, agent_id: str) -> str:
        """Replace a member's token, keeping tier/grants. Returns new plaintext."""
        self._refresh_if_stale()
        row = self._require(agent_id)
        if row.get("revoked"):
            raise RegistryError(f"agent {agent_id!r} is revoked; re-add it instead")
        token = _TOKEN_PREFIX + secrets.token_hex(24)
        row["token_sha256"] = _hash_token(token)
        row["rotated_at"] = _utc_now_iso()
        self._save()
        return token

    def revoke(self, agent_id: str) -> None:
        """Invalidate a member immediately. Its token stops authenticating;
        its audit history remains queryable."""
        self._refresh_if_stale()
        row = self._require(agent_id)
        row["revoked"] = True
        row["revoked_at"] = _utc_now_iso()
        self._save()

    def set_tier(self, agent_id: str, tier: str) -> None:
        self._refresh_if_stale()
        if tier not in VALID_TIERS:
            raise RegistryError(f"invalid tier {tier!r}: must be one of {VALID_TIERS}")
        row = self._require(agent_id)
        row["tier"] = tier
        self._save()

    def grant(self, agent_id: str, namespace: str) -> None:
        """Allow a quarantined member to read one agent-manifest namespace."""
        self._refresh_if_stale()
        if not _AGENT_ID_RE.match(namespace or ""):
            raise RegistryError(f"invalid namespace {namespace!r}")
        row = self._require(agent_id)
        grants = row.setdefault("grants", [])
        if namespace not in grants:
            grants.append(namespace)
        self._save()

    def ungrant(self, agent_id: str, namespace: str) -> None:
        self._refresh_if_stale()
        row = self._require(agent_id)
        grants = row.setdefault("grants", [])
        if namespace in grants:
            grants.remove(namespace)
        self._save()

    def _require(self, agent_id: str) -> dict[str, Any]:
        row = self._agents.get(agent_id)
        if row is None:
            raise RegistryError(f"unknown agent {agent_id!r}")
        return row
