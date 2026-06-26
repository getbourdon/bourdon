"""GitHub-embedded Copilot participant -- the v0.3 network-adapter smoke test.

GitHub-side Copilot (PR review, ``@copilot`` comments, commit-suggestion
authoring) persists nothing on the user's machine: its state lives on
github.com. The convention-file ``participants/copilot.py`` adapter punts that to
the user (they hand-write a memory file). This adapter federates it for real, by
reading the user's GitHub activity over the REST API.

It is the first adapter on the :class:`~participants._network_base.NetworkParticipant`
contract (#127): the base provides the local cache, graceful degradation, and the
auth boundary; this class provides only the GitHub-specific fetch + the
payload->L5 normalization.

Auth path
---------
A GitHub token with ``repo`` + ``read:user`` scope, sourced (in priority order):
  1. ``$GITHUB_TOKEN`` / ``$GH_TOKEN`` env var, OR
  2. the ``gh`` CLI's stored token via ``gh auth token`` (already in the OS
     keychain on a machine where ``gh`` is logged in).
The token is fetched lazily at call time and never stored in the cache or the
L5 manifest.

Fallback
--------
If the network path can't be used (no token, API down with no cache), the base's
degradation kicks in. The existing convention-file ``CopilotParticipant`` keeps
publishing ``copilot.l5.yaml`` independently, so this adapter is purely additive
-- it publishes ``github-copilot.l5.yaml`` and never replaces the file adapter.

Registered under the ``bourdon.participants`` entry point as ``github-copilot``.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any

from core.redaction import redact_text
from participants._network_base import (
    AuthProvider,
    NetworkParticipant,
    NetworkUnavailable,
    ParticipantAuthError,
)
from participants.base import (
    SPEC_VERSION,
    AgentInfo,
    Entity,
    L5Manifest,
    Session,
    Visibility,
    VisibilityPolicy,
)

logger = logging.getLogger(__name__)

AGENT_ID = "github-copilot"
AGENT_TYPE = "code-assistant"
PARTICIPANT_SLUG = "github-copilot"
DISPLAY_NAME = "GitHub Copilot (cloud)"
ROLE_NARRATIVE = (
    "Cloud-side GitHub Copilot: PR review and @copilot-authored comments/"
    "suggestions that live on github.com, not on the user's machine. Federated "
    "via the GitHub REST API rather than a local artifact."
)

GITHUB_API_ROOT = "https://api.github.com"
COPILOT_LOGINS = ("copilot", "github-copilot[bot]", "copilot-swe-agent[bot]")

DEFAULT_POLICY = VisibilityPolicy(
    default=Visibility.TEAM,
    private_tags=["personal", "financial", "credential", "secret"],
    team_tags=["github-copilot", "pull-request"],
)


# -- Auth resolution -----------------------------------------------------------


def gh_token_provider() -> str | None:
    """Resolve a GitHub token: env var first, then the ``gh`` CLI keychain."""
    for var in ("GITHUB_TOKEN", "GH_TOKEN"):
        val = os.environ.get(var)
        if val:
            return val
    gh = shutil.which("gh")
    if gh:
        try:
            out = subprocess.run(
                [gh, "auth", "token"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            token = out.stdout.strip()
            if out.returncode == 0 and token:
                return token
        except (OSError, subprocess.SubprocessError) as exc:
            logger.debug("gh auth token failed: %s", exc)
    return None


# -- HTTP (stdlib, no extra dependency) ----------------------------------------


def _github_get(path: str, token: str, *, timeout: float = 10.0) -> Any:
    """GET a GitHub REST path. Raises ParticipantAuthError on 401/403,
    NetworkUnavailable on transport/5xx. Returns parsed JSON."""
    url = path if path.startswith("http") else f"{GITHUB_API_ROOT}{path}"
    req = urllib.request.Request(url)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    req.add_header("User-Agent", "bourdon-github-copilot-participant")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise ParticipantAuthError(
                f"GitHub API {exc.code}: token invalid or lacks scope"
            ) from exc
        if exc.code >= 500:
            raise NetworkUnavailable(f"GitHub API {exc.code}") from exc
        # 4xx other than auth: treat as unavailable (e.g. 422), don't crash.
        raise NetworkUnavailable(f"GitHub API {exc.code}: {exc.reason}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise NetworkUnavailable(f"GitHub API unreachable: {exc}") from exc


# -- Participant ---------------------------------------------------------------


class GitHubCopilotParticipant(NetworkParticipant):
    """Federates cloud-side GitHub Copilot activity via the GitHub REST API."""

    participant_slug = PARTICIPANT_SLUG
    agent_id = AGENT_ID
    agent_type = AGENT_TYPE
    display_name = DISPLAY_NAME
    native_path = GITHUB_API_ROOT

    def __init__(
        self,
        *,
        auth_provider: AuthProvider | None = None,
        cache_root: Any | None = None,
        max_results: int = 50,
    ) -> None:
        super().__init__(
            auth_provider=auth_provider or gh_token_provider,
            cache_root=cache_root,
        )
        self._max_results = max_results

    @classmethod
    def default_native_path(cls, home: Any | None = None) -> str:
        # Network participant: detection isn't filesystem-based. The setup wizard
        # treats presence as "is a token resolvable", checked lazily elsewhere.
        return GITHUB_API_ROOT

    # -- Fetch (GitHub-specific) ----------------------------------------------

    def fetch_payload(self, token: str) -> dict[str, Any]:
        """Search recent issues/PRs commented on by Copilot for the auth'd user.

        Uses the search API to find PRs where a Copilot login is involved,
        scoped to the authenticated user's own repos via ``involves:@me`` so we
        only ever federate the user's own context.
        """
        # Identify the authenticated user (also validates the token early).
        me = _github_get("/user", token)
        login = me.get("login") if isinstance(me, dict) else None

        # PRs that involve the user where Copilot is a commenter/reviewer.
        # `commenter` query catches @copilot review threads.
        q = "is:pr involves:@me commenter:copilot"
        encoded = urllib.parse.quote(q)
        search = _github_get(
            f"/search/issues?q={encoded}&sort=updated&per_page={self._max_results}",
            token,
        )
        items = search.get("items", []) if isinstance(search, dict) else []

        return {
            "fetched_user": login,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "items": items,
        }

    # -- Normalize (payload -> L5; pure, no I/O) ------------------------------

    def payload_to_l5(self, payload: dict[str, Any]) -> L5Manifest:
        items = payload.get("items", []) if isinstance(payload, dict) else []
        sessions: list[Session] = []
        entities: dict[str, Entity] = {}

        for item in items:
            if not isinstance(item, dict):
                continue
            repo = _repo_from_item(item)
            title = redact_text(str(item.get("title") or ""), limit=160)
            number = item.get("number")
            updated = _iso_date(item.get("updated_at"))
            focus = [repo] if repo else []

            sessions.append(
                Session(
                    date=updated or "1970-01-01",
                    cwd=f"github.com/{repo}" if repo else None,
                    project_focus=focus,
                    key_actions=[f"PR #{number}: {title}"] if number else [title],
                    visibility=Visibility.TEAM,
                )
            )
            if repo and repo not in entities:
                entities[repo] = Entity(
                    name=repo,
                    type="repository",
                    summary="GitHub repo with Copilot PR activity.",
                    aliases=[repo.split("/")[-1]] if "/" in repo else [],
                    last_touched=updated,
                    tags=["github-copilot", "pull-request"],
                    visibility=Visibility.TEAM,
                )

        # Keep sessions newest-first, bounded.
        sessions.sort(key=lambda s: s.date, reverse=True)

        return L5Manifest(
            spec_version=SPEC_VERSION,
            agent=AgentInfo(
                id=self.agent_id,
                type=self.agent_type,
                role_narrative=ROLE_NARRATIVE,
                instance=payload.get("fetched_user") if isinstance(payload, dict) else None,
            ),
            last_updated=datetime.now(timezone.utc).isoformat(),
            capabilities=["github-search", "pull-request-comments"],
            recent_sessions=sessions[: self._max_results],
            known_entities=list(entities.values()),
            visibility_policy=DEFAULT_POLICY,
        )


# -- Helpers -------------------------------------------------------------------


def _repo_from_item(item: dict[str, Any]) -> str | None:
    """Derive 'owner/repo' from a search-issues item's repository_url."""
    repo_url = item.get("repository_url")
    if isinstance(repo_url, str) and "/repos/" in repo_url:
        return repo_url.split("/repos/", 1)[1]
    return None


def _iso_date(value: Any) -> str | None:
    """GitHub timestamps are ISO 8601 strings; reduce to YYYY-MM-DD."""
    if isinstance(value, str) and len(value) >= 10:
        return value[:10]
    return None
