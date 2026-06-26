"""Tests for the v0.3 network-adapter contract (#127).

Covers participants._network_base (cache / degrade / auth boundary) and the
participants.github_copilot reference adapter. No real network: fetch_payload is
always stubbed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from participants._network_base import (
    CONTRACT_VERSION,
    NetworkUnavailable,
    ParticipantAuthError,
    PayloadCache,
    env_auth_provider,
)
from participants.base import (
    BourdonParticipant,
)
from participants.github_copilot import (
    AGENT_ID,
    GitHubCopilotParticipant,
    _repo_from_item,
)

_SAMPLE = {
    "fetched_user": "ryan",
    "items": [
        {
            "title": "Add auth flow",
            "number": 12,
            "updated_at": "2026-06-20T10:00:00Z",
            "repository_url": "https://api.github.com/repos/ryan/bourdon",
        },
        {
            "title": "Fix tests",
            "number": 9,
            "updated_at": "2026-06-18T10:00:00Z",
            "repository_url": "https://api.github.com/repos/ryan/bourdon",
        },
    ],
}


def _make(tmp_path, *, token="tok", fetch=_SAMPLE, raise_exc=None, ttl=None):
    counter = {"n": 0}
    p = GitHubCopilotParticipant(
        auth_provider=lambda: token,
        cache_root=tmp_path,
    )
    if ttl is not None:
        p.cache_ttl_seconds = ttl

    def fake_fetch(tok):
        counter["n"] += 1
        if raise_exc is not None:
            raise raise_exc
        return fetch

    p.fetch_payload = fake_fetch  # type: ignore[method-assign]
    return p, counter


# ---- PayloadCache -----------------------------------------------------------


def test_cache_roundtrip(tmp_path: Path):
    c = PayloadCache("x", root=tmp_path)
    assert c.read() is None
    c.write({"hello": "world"})
    entry = c.read()
    assert entry is not None
    assert entry.payload == {"hello": "world"}
    assert entry.is_fresh(9999)


def test_cache_never_stores_token(tmp_path: Path):
    c = PayloadCache("x", root=tmp_path)
    c.write({"payload_field": "data"})
    raw = (tmp_path / "x" / "payload.json").read_text()
    assert "tok" not in raw  # sanity: no token string ever written
    assert "fetched_at" in raw


def test_cache_corrupt_returns_none(tmp_path: Path):
    c = PayloadCache("x", root=tmp_path)
    c.root.mkdir(parents=True)
    c.path.write_text("not json")
    assert c.read() is None


def test_env_auth_provider(monkeypatch):
    monkeypatch.setenv("MY_TOK", "abc")
    assert env_auth_provider("MY_TOK")() == "abc"
    monkeypatch.delenv("MY_TOK", raising=False)
    assert env_auth_provider("MY_TOK")() is None


# ---- the four acceptance-criteria scenarios ---------------------------------


def test_cache_miss_then_network_success(tmp_path: Path):
    p, counter = _make(tmp_path)
    manifest = p.export_l5()
    assert counter["n"] == 1
    assert len(manifest.recent_sessions) == 2
    assert {e.name for e in manifest.known_entities} == {"ryan/bourdon"}


def test_cache_hit_skips_network(tmp_path: Path):
    p1, c1 = _make(tmp_path)
    p1.export_l5()
    assert c1["n"] == 1
    # Second participant, same cache root, fresh TTL -> no fetch.
    p2, c2 = _make(tmp_path)
    p2.export_l5()
    assert c2["n"] == 0


def test_network_failure_serves_stale_cache(tmp_path: Path):
    # Seed cache.
    p_seed, _ = _make(tmp_path)
    p_seed.export_l5()
    # Now force expiry + network failure: should degrade to stale cache.
    p, counter = _make(tmp_path, raise_exc=NetworkUnavailable("boom"), ttl=-1)
    manifest = p.export_l5()
    assert len(manifest.recent_sessions) == 2  # served from stale cache
    health = p.health_check()
    assert health.status == "degraded"


def test_auth_failure_blocks_no_cache(tmp_path: Path):
    p = GitHubCopilotParticipant(auth_provider=lambda: None, cache_root=tmp_path)
    with pytest.raises(ParticipantAuthError):
        p.export_l5()
    health = p.health_check()
    assert health.status == "blocked"
    assert health.proposed_fix


def test_auth_error_never_masked_by_cache(tmp_path: Path):
    """A 401 must propagate even when a stale cache exists -- stale data must not
    mask a dead token."""
    p_seed, _ = _make(tmp_path)
    p_seed.export_l5()
    p, _ = _make(tmp_path, raise_exc=ParticipantAuthError("401"), ttl=-1)
    with pytest.raises(ParticipantAuthError):
        p.export_l5()


def test_no_token_but_cache_serves_stale(tmp_path: Path):
    """No token + existing cache -> serve stale, don't hard-block."""
    p_seed, _ = _make(tmp_path)
    p_seed.export_l5()
    p = GitHubCopilotParticipant(auth_provider=lambda: None, cache_root=tmp_path)
    p.cache_ttl_seconds = -1
    manifest = p.export_l5()
    assert len(manifest.recent_sessions) == 2
    assert p.health_check().status == "degraded"


# ---- discover / protocol conformance ----------------------------------------


def test_discover_reports_source(tmp_path: Path):
    p, _ = _make(tmp_path)
    store = p.discover()
    assert store.metadata["source"] == "network"
    assert store.metadata["contract_version"] == CONTRACT_VERSION


def test_structural_conformance(tmp_path: Path):
    p, _ = _make(tmp_path)
    assert isinstance(p, BourdonParticipant)
    assert p.agent_id == AGENT_ID == "github-copilot"


def test_export_sessions_since_filter(tmp_path: Path):
    p, _ = _make(tmp_path)
    from datetime import datetime, timezone

    # Cutoff after the older PR (2026-06-18), before the newer (2026-06-20).
    out = p.export_sessions(datetime(2026, 6, 19, tzinfo=timezone.utc))
    dates = [s.date for s in out]
    assert "2026-06-20" in dates
    assert "2026-06-18" not in dates


# ---- normalization ----------------------------------------------------------


def test_payload_to_l5_redacts_and_shapes(tmp_path: Path):
    p, _ = _make(
        tmp_path,
        fetch={
            "fetched_user": "ryan",
            "items": [
                {
                    "title": "leak sk-ant-aaaaaaaaaaaaaaaaaaaaaaaa",
                    "number": 1,
                    "updated_at": "2026-06-20T10:00:00Z",
                    "repository_url": "https://api.github.com/repos/ryan/x",
                }
            ],
        },
    )
    manifest = p.export_l5()
    blob = str(manifest.to_dict())
    assert "sk-ant-aaaaaaaaaaaaaaaaaaaaaaaa" not in blob  # redacted


def test_repo_from_item():
    assert _repo_from_item(
        {"repository_url": "https://api.github.com/repos/a/b"}
    ) == "a/b"
    assert _repo_from_item({"repository_url": "garbage"}) is None
    assert _repo_from_item({}) is None


# ---- malformed-response / health_check-never-raises -------------------------


class _FakeResp:
    """Minimal urlopen() stand-in: a context manager whose read() returns body."""

    def __init__(self, body: bytes):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._body


def test_github_get_non_json_body_is_network_unavailable(monkeypatch):
    """A 200 with a non-JSON body (captive portal / proxy HTML) must degrade to
    NetworkUnavailable -- NOT escape as a raw JSONDecodeError, which would crash
    health_check (the protocol's 'health_check must not raise')."""
    import urllib.request

    from participants.github_copilot import _github_get

    monkeypatch.setattr(
        urllib.request, "urlopen", lambda *a, **k: _FakeResp(b"<html>captive portal</html>")
    )
    with pytest.raises(NetworkUnavailable):
        _github_get("/x", "tok")


def test_health_check_degraded_not_raised_on_non_json_body(tmp_path: Path, monkeypatch):
    """End-to-end: the reference adapter's real fetch_payload hits a non-JSON
    body -> health_check returns degraded, never propagates."""
    import urllib.request

    monkeypatch.setattr(
        urllib.request, "urlopen", lambda *a, **k: _FakeResp(b"<html>proxy splash</html>")
    )
    p = GitHubCopilotParticipant(auth_provider=lambda: "tok", cache_root=tmp_path)
    health = p.health_check()  # must not raise
    assert health.status == "degraded"


def test_health_check_never_raises_on_unexpected_fetch_error(tmp_path: Path):
    """Base-class defense in depth: if an adapter's fetch_payload leaks something
    other than ParticipantAuthError / NetworkUnavailable, health_check must still
    return a status (blocked), not propagate -- the contract is absolute."""
    p, _ = _make(tmp_path, raise_exc=ValueError("kaboom"), ttl=-1)
    health = p.health_check()  # must not raise
    assert health.status == "blocked"
    assert "kaboom" in health.reason


def _http_error(code: int, *, rate_remaining: str | None = None):
    import email.message
    import urllib.error

    hdrs = email.message.Message()
    if rate_remaining is not None:
        hdrs["X-RateLimit-Remaining"] = rate_remaining
    return urllib.error.HTTPError("http://api.github.test/x", code, "err", hdrs, None)


def test_github_get_rate_limited_403_is_network_unavailable(monkeypatch):
    """403 + X-RateLimit-Remaining: 0 is a transient rate-limit, not a bad token:
    it must degrade (NetworkUnavailable -> cache fallback), not falsely tell the
    user to fix their credential."""
    import urllib.request

    from participants.github_copilot import _github_get

    err = _http_error(403, rate_remaining="0")
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: (_ for _ in ()).throw(err))
    with pytest.raises(NetworkUnavailable):
        _github_get("/x", "tok")


def test_github_get_genuine_403_is_auth_error(monkeypatch):
    """A 403 WITHOUT a rate-limit signal is a real auth/scope failure -> blocked."""
    import urllib.request

    from participants.github_copilot import _github_get

    err = _http_error(403)  # no X-RateLimit-Remaining header
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: (_ for _ in ()).throw(err))
    with pytest.raises(ParticipantAuthError):
        _github_get("/x", "tok")
