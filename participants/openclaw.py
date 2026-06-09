"""
OpenClaw participant — network-shaped, QUARANTINED-CLASS (v0.9.0, spec R4/D9).

OpenClaw is the highest-demand adapter target in the ecosystem and also its
highest-risk agent class: CVE-2026-25253 (one-click RCE, CVSS 8.8, first
patched in 2026.1.29), tens of thousands of internet-exposed instances (~93%
without authentication), ClawHub's malicious-skill problem, and auth disabled
by default on port 8080. Bourdon therefore treats OpenClaw differently from
every on-disk participant:

1. **Network-shaped** (the first instance of issue #127): state is read from
   the OpenClaw instance's local HTTP API, not on-disk artifacts.
2. **Hard handshake gate** — ``discover()`` refuses to talk to an instance
   that is unpatched (< 2026.1.29) or has authentication disabled. These are
   refusals with exact reasons and fixes, not warnings.
3. **Quarantined-class** — ``QUARANTINED_CLASS = True`` means
   ``bourdon agent add openclaw --tier trusted`` requires
   ``--i-understand-the-risk``, and ``bourdon openclaw export`` writes to
   the federation **staging** area, never directly to the live store
   (spec D6: quarantine follows the content, not the invoker).

We gate on instance hygiene (version, auth) — NOT on auditing the user's
installed skills; that's ClawSecure et al.'s job (spec non-goal 3).
"""

from __future__ import annotations

import json
import logging
import os
import re
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from participants.base import (
    AgentInfo,
    AgentStore,
    Entity,
    HealthStatus,
    L5Manifest,
    ParticipantDiscoveryError,
    Session,
    Visibility,
    VisibilityPolicy,
    filter_for_federation,
)
from participants.codex import _safe_native_memory_text

logger = logging.getLogger(__name__)

AGENT_ID = "openclaw"
AGENT_TYPE = "other"
ROLE_NARRATIVE = (
    "OpenClaw personal AI assistant — federated as a QUARANTINED member: "
    "its reads are limited to granted namespaces and its writes are staged "
    "for operator review."
)

#: First OpenClaw release that patches CVE-2026-25253 (one-click RCE).
MIN_PATCHED_VERSION = "2026.1.29"

DEFAULT_OPENCLAW_URL = "http://127.0.0.1:8080"

_SESSION_LIMIT = 50
_ENTITY_LIMIT = 100


def _parse_version(value: str) -> tuple[int, ...] | None:
    """``"2026.1.29"`` -> ``(2026, 1, 29)``. Tolerates suffixes like
    ``2026.1.29-beta``; returns None when nothing numeric parses."""
    parts: list[int] = []
    for chunk in str(value or "").strip().split("."):
        match = re.match(r"(\d+)", chunk)
        if not match:
            break
        parts.append(int(match.group(1)))
    return tuple(parts) if parts else None


class OpenClawApiClient:
    """Minimal HTTP client for a local OpenClaw instance (stdlib-only).

    Read-only: status, sessions, memories. Failures raise
    :class:`ParticipantDiscoveryError` with the reason — the participant
    decides how to surface them.
    """

    def __init__(self, url: str, token: str | None = None, timeout: float = 5.0):
        self.url = url.rstrip("/")
        self._token = token
        self.timeout = timeout

    def _get(self, path: str) -> Any:
        request = urllib.request.Request(self.url + path)
        if self._token:
            request.add_header("Authorization", f"Bearer {self._token}")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise ParticipantDiscoveryError(
                f"OpenClaw API {path} returned HTTP {exc.code}"
            ) from exc
        except (urllib.error.URLError, OSError, json.JSONDecodeError, TimeoutError) as exc:
            raise ParticipantDiscoveryError(
                f"OpenClaw instance unreachable at {self.url} ({exc})"
            ) from exc

    def status(self) -> dict[str, Any]:
        data = self._get("/api/status")
        return data if isinstance(data, dict) else {}

    def sessions(self) -> list[dict[str, Any]]:
        try:
            data = self._get("/api/sessions")
        except ParticipantDiscoveryError:
            return []
        if isinstance(data, dict):
            data = data.get("sessions") or []
        return [row for row in data if isinstance(row, dict)]

    def memories(self) -> list[dict[str, Any]]:
        try:
            data = self._get("/api/memories")
        except ParticipantDiscoveryError:
            return []
        if isinstance(data, dict):
            data = data.get("memories") or []
        return [row for row in data if isinstance(row, dict)]


def _status_version(status: dict[str, Any]) -> str:
    for key in ("version", "openclaw_version", "app_version"):
        value = status.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _status_auth_enabled(status: dict[str, Any]) -> bool:
    for key in ("auth_enabled", "authEnabled"):
        if key in status:
            return bool(status[key])
    auth = status.get("auth")
    if isinstance(auth, dict):
        return bool(auth.get("enabled"))
    return False


def verify_instance(status: dict[str, Any], url: str) -> str:
    """Hard handshake preconditions (spec D9). Returns the version on success.

    Raises :class:`ParticipantDiscoveryError` with the exact reason AND the
    fix — these are refusals, not warnings.
    """
    version = _status_version(status)
    parsed = _parse_version(version)
    minimum = _parse_version(MIN_PATCHED_VERSION)
    if parsed is None:
        raise ParticipantDiscoveryError(
            f"OpenClaw at {url} did not report a parseable version "
            f"(got {version!r}). Refusing handshake: cannot verify the "
            f"CVE-2026-25253 patch level. Fix: upgrade OpenClaw to "
            f">= {MIN_PATCHED_VERSION} and ensure /api/status reports it."
        )
    if parsed < minimum:
        raise ParticipantDiscoveryError(
            f"OpenClaw at {url} runs {version}, which predates the "
            f"CVE-2026-25253 patch (one-click RCE, CVSS 8.8). Refusing "
            f"handshake. Fix: upgrade OpenClaw to >= {MIN_PATCHED_VERSION}."
        )
    if not _status_auth_enabled(status):
        raise ParticipantDiscoveryError(
            f"OpenClaw at {url} has authentication DISABLED (the exposed-"
            "instance default). Refusing handshake. Fix: enable auth in "
            "your OpenClaw config (set auth.enabled=true / OPENCLAW_AUTH=1), "
            "restart the instance, and set OPENCLAW_TOKEN for Bourdon."
        )
    return version


class OpenClawParticipant:
    """Bourdon participant for OpenClaw (quarantined class)."""

    agent_id = AGENT_ID
    agent_type = AGENT_TYPE
    display_name = "OpenClaw (quarantined)"
    #: Trust marker consumed by `bourdon agent add/set-tier` and export-all:
    #: registering as trusted needs --i-understand-the-risk; exports stage.
    QUARANTINED_CLASS = True

    @classmethod
    def default_native_path(cls, home: Path | None = None) -> Path:
        # Network-shaped: shown in the wizard for orientation only. The
        # actual store is the instance API (OPENCLAW_URL).
        return (home or Path.home()) / ".openclaw"

    def __init__(
        self,
        url: str | None = None,
        token: str | None = None,
        client: OpenClawApiClient | None = None,
    ) -> None:
        self.url = (url or os.environ.get("OPENCLAW_URL") or DEFAULT_OPENCLAW_URL).rstrip("/")
        self._client = client or OpenClawApiClient(
            self.url, token=token or os.environ.get("OPENCLAW_TOKEN")
        )

    @property
    def native_path(self) -> str:
        return self.url

    # -- protocol ---------------------------------------------------------------

    def discover(self) -> AgentStore:
        status = self._client.status()
        version = verify_instance(status, self.url)
        return AgentStore(
            path=self.url,
            version=version,
            metadata={"auth_enabled": True, "transport": "http"},
        )

    def export_sessions(
        self, since: datetime | None = None, limit: int = _SESSION_LIMIT
    ) -> list[Session]:
        self.discover()  # handshake gate applies to every read
        sessions: list[Session] = []
        for row in self._client.sessions():
            date = str(
                row.get("updated_at") or row.get("started_at") or row.get("date") or ""
            ).strip()
            if not date:
                continue
            if since is not None and _before(date, since):
                continue
            title = _safe_native_memory_text(
                str(row.get("title") or row.get("summary") or "")
            )
            sessions.append(
                Session(
                    date=date,
                    cwd=None,
                    project_focus=[
                        _safe_native_memory_text(str(p))
                        for p in (row.get("projects") or [])[:5]
                    ],
                    key_actions=[title] if title else [],
                )
            )
            if len(sessions) >= limit:
                break
        return sessions

    def export_l5(self, since: datetime | None = None) -> L5Manifest:
        store = self.discover()
        entities: list[Entity] = []
        for row in self._client.memories()[:_ENTITY_LIMIT]:
            name = _safe_native_memory_text(str(row.get("name") or row.get("title") or ""))
            if not name:
                continue
            entities.append(
                Entity(
                    name=name,
                    type=str(row.get("type") or "topic"),
                    summary=_safe_native_memory_text(str(row.get("summary") or "")),
                    tags=[
                        _safe_native_memory_text(str(t))
                        for t in (row.get("tags") or [])[:8]
                    ],
                )
            )
        # Quarantined-class content defaults to TEAM, never PUBLIC: it only
        # federates beyond the local library after an explicit operator
        # promotion AND a team-level read.
        policy = VisibilityPolicy(default=Visibility.TEAM)
        return L5Manifest(
            spec_version="0.1",
            agent=AgentInfo(
                id=AGENT_ID,
                type=AGENT_TYPE,
                instance=self.url,
                role_narrative=ROLE_NARRATIVE,
            ),
            last_updated=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            capabilities=[f"openclaw {store.version}"],
            recent_sessions=self.export_sessions(since=since),
            known_entities=filter_for_federation(entities, policy),
            visibility_policy=policy,
        )

    def health_check(self) -> HealthStatus:
        try:
            store = self.discover()
        except ParticipantDiscoveryError as exc:
            reason = str(exc)
            fix = "see the refusal reason above"
            if "upgrade OpenClaw" in reason:
                fix = f"upgrade the OpenClaw instance to >= {MIN_PATCHED_VERSION}"
            elif "authentication DISABLED" in reason:
                fix = "enable auth on the OpenClaw instance, then set OPENCLAW_TOKEN"
            elif "unreachable" in reason:
                fix = (
                    "start OpenClaw locally or set OPENCLAW_URL to the "
                    "instance address"
                )
            return HealthStatus(status="blocked", reason=reason, proposed_fix=fix)
        except Exception as exc:  # noqa: BLE001 — health_check must never raise
            return HealthStatus(status="degraded", reason=str(exc))
        return HealthStatus(
            status="ok",
            details={"url": self.url, "version": store.version, "quarantined": True},
        )


def _before(date_text: str, since: datetime) -> bool:
    try:
        parsed = datetime.fromisoformat(date_text.replace("Z", "+00:00"))
    except ValueError:
        return False
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    cutoff = since if since.tzinfo else since.replace(tzinfo=timezone.utc)
    return parsed < cutoff
