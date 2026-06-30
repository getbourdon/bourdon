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

import json
from pathlib import Path

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

# The secret battery + benign survivors now load from the language-neutral parity
# fixture (conformance/redaction_battery.json) -- the SINGLE source of truth shared
# with the TypeScript mirror's vitest suite, so a pattern can never drift between
# the two implementations. Token-shaped secrets are stored as fragment arrays (no
# contiguous literal lands in git); they are joined at load. Regenerate via
# `python tools/gen_conformance.py` after changing a pattern in core.redaction.
# See the bourdon-parity-fixture-harness skill.
_FIXTURE = json.loads(
    (Path(__file__).resolve().parent.parent / "conformance" / "redaction_battery.json")
    .read_text(encoding="utf-8")
)
SECRETS = ["".join(case["fragments"]) for case in _FIXTURE["secrets"]]
BENIGN = [case["text"] for case in _FIXTURE["benign"]]


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
