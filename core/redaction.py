"""Single source of truth for credential redaction across every Bourdon surface.

Historically each participant, the export summarizer, and the L6 server carried
its own credential-pattern tuple and scrub function. They drifted: the
federation and recognition surfaces -- the ones that actually cross machines --
used the *weakest* sets, so keyword-less secrets (AWS ``AKIA...``, GitHub
``ghp_...``, JWTs / Supabase ``service_role``, PEM private keys) federated
verbatim. (3-Star Michelin audit 2026-06-22, findings P0-2 / P0-4.)

Every surface now routes through :func:`redact_text` / :func:`contains_secret`
here. ``tests/test_redaction.py`` feeds a secret battery through every surface
and asserts identical redaction, so the invariant can never silently drift
again -- adding a new pattern here upgrades all surfaces at once.

This module imports only :mod:`re` so it is safe to import from anywhere
(participants, core, server) with no risk of an import cycle.
"""

from __future__ import annotations

import re

#: The one canonical redaction sentinel. Asserted across the test-suite; do not
#: change without updating every ``assert "[redacted credential-like text]"``.
REDACTED = "[redacted credential-like text]"

# Keyword-shaped triggers: a nearby word strongly implies a secret value is
# present even when the value itself is unrecognizable. Kept specific -- bare
# ambiguous words like "secret"/"token"/"key" are deliberately NOT here because
# they over-redact the recognition surface (that drops real anchors). A surface
# that wants a more aggressive local rule layers it on top (see cascade).
_KEYWORD_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bapi[_-]?keys?\b", re.IGNORECASE),
    re.compile(r"\bapi[_-]?tokens?\b", re.IGNORECASE),
    re.compile(r"\baccess[_-]?tokens?\b", re.IGNORECASE),
    re.compile(r"\brefresh[_-]?tokens?\b", re.IGNORECASE),
    re.compile(r"\bbearer\s+token\b", re.IGNORECASE),
    re.compile(r"\bservice[_-]?role\b", re.IGNORECASE),
    re.compile(r"\bclient[_-]?secret\b", re.IGNORECASE),
    re.compile(r"\bpassword\b", re.IGNORECASE),
    re.compile(r"\bpasswd\b", re.IGNORECASE),
    re.compile(r"\bstripe\s+(?:key|secret|token)\b", re.IGNORECASE),
    re.compile(r"\b(?:keystore|private[_-]?key|ssh[_-]?key)\b", re.IGNORECASE),
    re.compile(r"\.env\b", re.IGNORECASE),
)

# Value-shaped triggers: the literal token, no keyword needed. THESE are what
# the pre-SSOT sets missed and what the audit flagged as the P0 leak class.
_TOKEN_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\b[sprk]k_(?:live|test)_[A-Za-z0-9]{8,}\b"),  # Stripe sk/pk/rk_live|test
    re.compile(r"\bappl_[A-Za-z0-9]{10,}\b", re.IGNORECASE),    # RevenueCat
    re.compile(r"\bhf_[A-Za-z0-9]{10,}\b", re.IGNORECASE),      # HuggingFace
    re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),              # AWS access-key id
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b"),            # GitHub PAT / OAuth / app
    re.compile(r"\bgithub_pat_[0-9A-Za-z_]{22,}\b"),          # GitHub fine-grained PAT
    re.compile(r"\bglpat-[0-9A-Za-z_\-]{20,}\b"),             # GitLab PAT
    re.compile(r"\bxox[baprs]-[0-9A-Za-z-]{10,}\b"),          # Slack
    re.compile(r"\bsk-(?:ant-)?[A-Za-z0-9_\-]{20,}\b"),       # OpenAI / Anthropic
    re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b"),                # Google API key
    re.compile(r"\bya29\.[0-9A-Za-z_\-]{20,}\b"),            # Google OAuth token
    re.compile(r"\bnpm_[0-9A-Za-z]{36}\b"),                   # npm token
    # JWT (three base64url segments) -- covers Supabase service_role / anon keys.
    re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{4,}\b"),
    re.compile(r"-----BEGIN (?:[A-Z]+ )*PRIVATE KEY-----"),    # PEM private key
)

#: The full pattern set. Re-exported as each surface's legacy tuple name.
SENSITIVE_PATTERNS: tuple[re.Pattern[str], ...] = _KEYWORD_PATTERNS + _TOKEN_PATTERNS

_WHITESPACE = re.compile(r"\s+")
_URL = re.compile(r"https?://\S+")


def contains_secret(value: str) -> bool:
    """Return ``True`` if ``value`` matches any credential pattern.

    Used by surfaces (e.g. claude_code) that gate on presence rather than
    transform the text in place.
    """
    if not value:
        return False
    return any(pattern.search(value) for pattern in SENSITIVE_PATTERNS)


def redact_text(value: str, limit: int = 180) -> str:
    """Canonical scrub used by every surface that emits free-form memory text.

    Collapse whitespace, drop the whole string to :data:`REDACTED` if it looks
    like it contains a secret, strip URLs to ``[link]``, then cap at ``limit``
    characters. This is the ONE implementation; per-surface wrappers exist only
    to preserve their public names and length budgets.
    """
    if not value:
        return value
    text = _WHITESPACE.sub(" ", value.strip())
    if any(pattern.search(text) for pattern in SENSITIVE_PATTERNS):
        return REDACTED
    text = _URL.sub("[link]", text)
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."
