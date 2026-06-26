"""Network-shaped participant base -- v0.3 adapter contract (issue #127).

Every Bourdon participant up to v0.2 is a **file-reader**: ``discover()`` walks a
known on-disk path. That blocks a whole class of agents whose state lives in a
SaaS API, not on the user's box -- GitHub-embedded Copilot, future cloud agent
surfaces, background agents.

A ``NetworkParticipant`` keeps the **same** ``export_l5()`` / ``export_sessions()``
contract as a file participant -- the L5-emit half is identical -- and differs
only in the discovery half: it fetches from an authenticated network API. The
v0.3 contract adds three guarantees on top of the base participant contract,
all provided by this base class so individual adapters don't reimplement them:

1. **Local cache layer.** Every fetched payload is cached under
   ``~/.cache/bourdon/<participant>/`` with a TTL. Adapters never hit the
   network on a cache hit, so a tight federation loop (``export-all`` on every
   session end) doesn't hammer the API.

2. **Graceful degradation.** If the network is unreachable, the base falls back
   to the most recent cached payload (even an expired one). Federation never
   goes silent because an API is flaky -- it goes *stale*, loudly, via
   ``health_check`` reporting ``degraded`` with the cache age.

3. **Authentication boundary.** Credentials come from an injected
   ``auth_token`` provider (OS keychain or env var, documented per adapter).
   They are NEVER read from or written to an L5 manifest. The base never logs
   the token and the cache never stores it.

Subclass responsibilities (the only things an adapter must implement):

  - ``participant_slug``        : cache namespace + agent id.
  - ``agent_type`` / ``agent_id``
  - ``fetch_payload(token)``    : do the actual authenticated API call, return a
    JSON-serializable dict. Raise ``NetworkUnavailable`` on a transport/5xx
    failure (-> triggers cache fallback) or ``ParticipantAuthError`` on a 401/403
    (-> surfaced as blocked, NOT silently degraded -- a bad token is a user
    problem to fix, not stale data to serve).
  - ``payload_to_l5(payload)``  : normalize a (fresh or cached) payload dict into
    an ``L5Manifest``. Pure function, no I/O -- the file-participant emit half.

See ``participants/github_copilot.py`` for the reference adapter and
``spec/PARTICIPANT_CONTRACT.md`` (v0.3 section) for the normative contract.
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from participants.base import (
    AgentStore,
    HealthStatus,
    L5Manifest,
    ParticipantError,
    Session,
)

logger = logging.getLogger(__name__)

CONTRACT_VERSION = "0.3"

#: Default cache TTL. Adapters override via ``cache_ttl_seconds``.
DEFAULT_CACHE_TTL_SECONDS = 15 * 60  # 15 minutes


# -- Network-specific errors ---------------------------------------------------


class NetworkUnavailable(ParticipantError):
    """Transport failure / timeout / 5xx -- recoverable via cache fallback."""


class ParticipantAuthError(ParticipantError):
    """401/403 / missing credential -- NOT recoverable via cache. The user must
    fix auth. Surfaced as ``health_check() -> blocked``, never silently served
    from stale cache (stale data would mask a broken token indefinitely)."""


# -- Auth provider -------------------------------------------------------------

#: An auth provider is any zero-arg callable returning the token string, or None
#: if no credential is available. Keeping it a callable (not a bare string) means
#: the token is fetched lazily at call time from the keychain/env and never has
#: to sit in the participant's attributes or be passed through the cache.
AuthProvider = Callable[[], str | None]


def env_auth_provider(var_name: str) -> AuthProvider:
    """Auth provider that reads a token from an environment variable."""

    def _provider() -> str | None:
        return os.environ.get(var_name) or None

    return _provider


# -- Cache ---------------------------------------------------------------------


def default_cache_root() -> Path:
    """``$XDG_CACHE_HOME/bourdon`` or ``~/.cache/bourdon``."""
    xdg = os.environ.get("XDG_CACHE_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".cache"
    return base / "bourdon"


@dataclass
class CacheEntry:
    fetched_at: float  # epoch seconds
    payload: dict[str, Any]

    @property
    def age_seconds(self) -> float:
        return max(0.0, time.time() - self.fetched_at)

    def is_fresh(self, ttl_seconds: float) -> bool:
        return self.age_seconds < ttl_seconds


class PayloadCache:
    """A tiny JSON file cache, one file per participant slug.

    Stores only ``{fetched_at, payload}`` -- never the auth token. Read/write are
    best-effort: a corrupt or unwritable cache degrades to "no cache", never
    raises into the participant.
    """

    def __init__(self, slug: str, *, root: Path | None = None) -> None:
        self.slug = slug
        self.root = (root or default_cache_root()) / slug
        self.path = self.root / "payload.json"

    def read(self) -> CacheEntry | None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(raw, dict) or "payload" not in raw:
            return None
        try:
            fetched_at = float(raw.get("fetched_at") or 0.0)
        except (TypeError, ValueError):
            fetched_at = 0.0
        payload = raw.get("payload")
        if not isinstance(payload, dict):
            return None
        return CacheEntry(fetched_at=fetched_at, payload=payload)

    def write(self, payload: dict[str, Any]) -> None:
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps({"fetched_at": time.time(), "payload": payload}),
                encoding="utf-8",
            )
            tmp.replace(self.path)  # atomic
        except OSError as exc:
            logger.debug("cache write failed for %s: %s", self.slug, exc)


# -- Base participant ----------------------------------------------------------


class NetworkParticipant:
    """Base for participants whose native state lives behind an authenticated API.

    Subclasses set ``participant_slug`` / ``agent_id`` / ``agent_type`` and
    implement ``fetch_payload`` + ``payload_to_l5``. Everything else (caching,
    degradation, the auth boundary, ``discover``/``export_l5``/``health_check``)
    is provided here.
    """

    participant_slug: str = "network"
    agent_id: str = "network"
    agent_type: str = "other"
    cache_ttl_seconds: float = DEFAULT_CACHE_TTL_SECONDS

    # External participants expose a filesystem-ish ``native_path``; for a
    # network participant it's the API root, set by the subclass.
    native_path: str = "network://"

    def __init__(
        self,
        *,
        auth_provider: AuthProvider | None = None,
        cache_root: Path | None = None,
    ) -> None:
        self._auth_provider = auth_provider
        self._cache = PayloadCache(self.participant_slug, root=cache_root)

    # -- Subclass hooks --------------------------------------------------------

    def fetch_payload(self, token: str) -> dict[str, Any]:
        """Do the authenticated API call. Return a JSON-serializable dict.

        Raise :class:`NetworkUnavailable` on transport/5xx (cache fallback) or
        :class:`ParticipantAuthError` on 401/403 (blocked, no fallback).
        """
        raise NotImplementedError

    def payload_to_l5(self, payload: dict[str, Any]) -> L5Manifest:
        """Normalize a payload dict into an L5 manifest. Pure, no I/O."""
        raise NotImplementedError

    # -- Core fetch-with-cache-and-degrade -------------------------------------

    def _get_payload(self) -> tuple[dict[str, Any], str]:
        """Return (payload, source) where source is 'cache'|'network'|'stale-cache'.

        Resolution:
          1. Fresh cache hit          -> ('cache')          no network.
          2. Cache miss/expired + net  -> ('network')        fetch + refresh cache.
          3. Net fails + any cache     -> ('stale-cache')    serve last good payload.
          4. Net fails + no cache      -> raise (the failure that occurred).
        Auth errors are never swallowed -- they propagate so health surfaces
        'blocked' rather than serving stale data behind a dead token.
        """
        cached = self._cache.read()
        if cached is not None and cached.is_fresh(self.cache_ttl_seconds):
            return cached.payload, "cache"

        token = self._auth_provider() if self._auth_provider else None
        if not token:
            # No credential: if we have *any* cache, serve it stale; else blocked.
            if cached is not None:
                return cached.payload, "stale-cache"
            raise ParticipantAuthError(
                f"{self.participant_slug}: no auth token available and no cache"
            )

        try:
            payload = self.fetch_payload(token)
        except ParticipantAuthError:
            raise  # never mask a bad token with stale data
        except NetworkUnavailable as exc:
            if cached is not None:
                logger.info(
                    "%s: network unavailable (%s), serving stale cache (age %.0fs)",
                    self.participant_slug,
                    exc,
                    cached.age_seconds,
                )
                return cached.payload, "stale-cache"
            raise

        self._cache.write(payload)
        return payload, "network"

    # -- Participant protocol --------------------------------------------------

    def discover(self) -> AgentStore:
        """Confirm we can obtain a payload (cached or live). Raises
        ParticipantAuthError / NetworkUnavailable on a hard miss."""
        _payload, source = self._get_payload()
        return AgentStore(
            path=self.native_path,
            version=f"{self.participant_slug}-network-v1",
            metadata={"source": source, "contract_version": CONTRACT_VERSION},
        )

    def export_l5(self, since: datetime | None = None) -> L5Manifest:
        payload, _source = self._get_payload()
        return self.payload_to_l5(payload)

    def export_sessions(
        self, since: datetime, limit: int = 100
    ) -> list[Session]:
        manifest = self.export_l5(since=since)
        sessions = manifest.recent_sessions
        if since is not None:
            cutoff = since.date().isoformat()
            sessions = [s for s in sessions if (s.date or "") >= cutoff]
        return sessions[:limit]

    def health_check(self) -> HealthStatus:
        """ok (live or fresh cache) / degraded (stale cache) / blocked (auth)."""
        try:
            _payload, source = self._get_payload()
        except ParticipantAuthError as exc:
            return HealthStatus(
                status="blocked",
                reason=str(exc),
                details={"participant": self.participant_slug},
                proposed_fix=(
                    f"Provide a valid credential for {self.participant_slug} "
                    "(see the adapter docstring for the auth path) and re-run "
                    "`bourdon export-all`."
                ),
            )
        except NetworkUnavailable as exc:
            return HealthStatus(
                status="degraded",
                reason=f"network unavailable and no cache: {exc}",
                details={"participant": self.participant_slug},
                proposed_fix="Check connectivity, then re-run `bourdon export-all`.",
            )
        except Exception as exc:  # noqa: BLE001 -- health_check must NEVER raise (contract)
            # Defense in depth for the whole NetworkParticipant category: an
            # adapter's fetch_payload should only raise ParticipantAuthError /
            # NetworkUnavailable, but if one leaks anything else (e.g. an
            # unhandled parse error) health_check must still return a status, not
            # propagate. Report it as blocked so it surfaces loudly.
            return HealthStatus(
                status="blocked",
                reason=f"unexpected error resolving payload: {exc}",
                details={"participant": self.participant_slug},
                proposed_fix=(
                    f"{self.participant_slug} raised an unexpected error during "
                    "health_check; its fetch_payload should only raise "
                    "ParticipantAuthError / NetworkUnavailable. File an issue with "
                    "this reason string."
                ),
            )
        cached = self._cache.read()
        details: dict[str, Any] = {
            "participant": self.participant_slug,
            "source": source,
        }
        if cached is not None:
            details["cache_age_seconds"] = round(cached.age_seconds, 1)
            details["cache_fetched_at"] = datetime.fromtimestamp(
                cached.fetched_at, tz=timezone.utc
            ).isoformat()
        if source == "stale-cache":
            return HealthStatus(
                status="degraded",
                reason="serving stale cache (network unavailable or no token)",
                details=details,
                proposed_fix="Restore connectivity / refresh the token to update.",
            )
        return HealthStatus(status="ok", details=details)
