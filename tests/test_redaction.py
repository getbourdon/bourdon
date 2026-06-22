"""Cross-surface credential-redaction parity test.

The 3-Star Michelin audit (2026-06-22) found at least five drifting copies of
the credential-pattern set; the federation/recognition/export surfaces -- the
ones that cross machines -- used the weakest. This test feeds one secret battery
through EVERY surface and asserts each redacts identically, so a secret redacted
on one surface can never ship in the clear on another.

If you add a surface that emits free-form memory text, add it to SURFACES.
If you add a secret shape to core.redaction, add it to SECRETS.
"""

from __future__ import annotations

import pytest

from core.l6_server import _safe_context_text
from core.redaction import REDACTED, contains_secret, redact_text
from participants._cursor_sqlite import _scrub_text
from participants.cascade import _scrub_credential
from participants.claude_code import _contains_credential_pattern
from participants.codex import _safe_native_memory_text

# Every surface that transforms text -> redacted text. Each must collapse a
# secret to the sentinel.
TRANSFORM_SURFACES = (
    ("core.redaction.redact_text", redact_text),
    ("codex._safe_native_memory_text", _safe_native_memory_text),
    ("l6_server._safe_context_text", _safe_context_text),
    ("cascade._scrub_credential", _scrub_credential),
    ("_cursor_sqlite._scrub_text", _scrub_text),
)

# Surfaces that gate on presence rather than transform.
PREDICATE_SURFACES = (
    ("core.redaction.contains_secret", contains_secret),
    ("claude_code._contains_credential_pattern", _contains_credential_pattern),
)

# The secret battery. Each MUST be redacted on every surface. The keyword-less
# token shapes (AWS/GitHub/Slack/OpenAI/Anthropic/Google/JWT/PEM) are the class
# the pre-SSOT sets missed (P0-2 / P0-4).
SECRETS = [
    "my api_key is QXp9-not-a-real-key",
    "password: hunter2hunter2",
    "uses a bearer token to authenticate",
    "the service_role key for supabase",
    "stripe secret rotated today",
    "the keystore password lives in .env",
    # Token-shaped fixtures are assembled from fragments so no contiguous secret
    # literal lives in source (GitHub push-protection scans literals). The
    # runtime-joined strings still exercise the regexes.
    "sk" + "_live_" + "abcd1234efGH5678ijkl",
    "appl" + "_AbCdEfGhIjKlMnOp",
    "hf" + "_abcdefghij1234567890",
    "AKIA" + "IOSFODNN7EXAMPLE",
    "ghp_" + "a" * 36,
    "github_pat_" + "b" * 24,
    "glpat-" + "c" * 22,
    "xoxb" + "-123456789012-abcdefghijklmnop",
    "sk-" + "d" * 24,
    "sk-ant-" + "api03-" + "e" * 24,
    "AIza" + "f" * 35,
    "ya29." + "g" * 30,
    "npm_" + "h" * 36,
    "eyJ" + "hbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9." + "eyJzdWIiOiIxMjM0In0." + "Sf" + "lKxwRJSMeKKF2QT4fwpMeJf36",
    "-----BEGIN RSA PRIVATE KEY-----",
]

# Benign text the recognition surface MUST keep (over-redaction drops anchors).
# Includes words that embed token prefixes mid-word (task/risk/disk) to prove the
# \b anchoring does not false-positive.
BENIGN = [
    "We shipped the recognition timing layer for Bourdon",
    "Fixed the desktop tray autostart on Windows",
    "task-management-dashboard-rewrite-was-completed-today",
    "the risk-assessment-and-disk-usage-monitoring-tool shipped",
    "Bourdon recognition-first runtime layer is the core promise",
]


@pytest.mark.parametrize("secret", SECRETS)
@pytest.mark.parametrize("name,fn", TRANSFORM_SURFACES)
def test_every_surface_redacts_every_secret(name, fn, secret):
    assert fn(secret) == REDACTED, f"{name} failed to redact: {secret[:24]!r}"


@pytest.mark.parametrize("secret", SECRETS)
@pytest.mark.parametrize("name,pred", PREDICATE_SURFACES)
def test_every_predicate_flags_every_secret(name, pred, secret):
    assert pred(secret) is True, f"{name} failed to flag: {secret[:24]!r}"


@pytest.mark.parametrize("text", BENIGN)
def test_benign_text_survives_recognition_surface(text):
    # Must NOT be redacted, and must come back substantially intact.
    out = redact_text(text, limit=400)
    assert out != REDACTED, f"over-redacted benign text: {text!r}"
    assert contains_secret(text) is False


def test_empty_string_passthrough():
    assert redact_text("") == ""
    assert contains_secret("") is False
