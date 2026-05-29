"""Shared text-safety helpers for recognition surfaces.

These were originally defined in :mod:`adapters.codex` but are entirely
agent-agnostic: they normalize whitespace, redact credential-like text, and
bound length for any native-memory or federation text that may be surfaced to
a model. The turn-scoped compilers (Codex + Claude) share them through this
module so the shared compiler core does not need to import a specific adapter.

:mod:`adapters.codex` re-exports these names for backwards compatibility.
"""

from __future__ import annotations

import re

# Patterns that, if matched anywhere in the text, cause the whole value to be
# redacted before it is surfaced. Kept conservative and case-insensitive.
_NATIVE_MEMORY_SENSITIVE_PATTERNS = (
    re.compile(r"\bapi[_-]?key\b", re.IGNORECASE),
    re.compile(r"\bapi[_-]?token\b", re.IGNORECASE),
    re.compile(r"\baccess[_-]?token\b", re.IGNORECASE),
    re.compile(r"\bbearer\s+token\b", re.IGNORECASE),
    re.compile(r"\bpassword\b", re.IGNORECASE),
    re.compile(r"\bsk_live_[A-Za-z0-9_]+\b"),
    re.compile(r"\bhf_[A-Za-z0-9_]{10,}\b", re.IGNORECASE),
)


def _normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip())


def _safe_native_memory_text(value: str, limit: int = 180) -> str:
    text = _normalize_text(value)
    if any(pattern.search(text) for pattern in _NATIVE_MEMORY_SENSITIVE_PATTERNS):
        return "[redacted credential-like text]"
    text = re.sub(r"https?://\S+", "[link]", text)
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."
