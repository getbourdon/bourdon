"""Shared recognition contract — the single source of truth for the pieces of
the recognition DECISION that must agree across every Bourdon surface.

Bourdon's headline promise is "consistent cross-agent recognition": the same
prompt should be recognized the same way regardless of which agent/surface
asks. Today three engines drift apart with no shared definition:

- ``core.codex_turn_compiler`` — rich 6-component weighted scorer (CLI + MCP)
- ``core.cursor_turn_compiler`` — flat additive scorer
- ``core.recognition_runtime`` — no-score boolean recognizer (turn-start hot path)

This module owns ONLY what must agree byte-for-byte: the tokenizer, the
canonical stopword set, the minimum term length, the match-tier ladder (HOW a
candidate matched), the normalized confidence buckets, and the top-anchor
tie-break. Each engine keeps its own data source, output envelope, and extra
signals — those legitimately differ.

Adoption is staged (see PROJECTS/NEUROLAYER and tests/test_recognition_parity.py):
the byte-identical ``TOKEN_RE`` / :func:`tokenize` are wired first (zero behavior
change); STOPWORDS, the match tiers, and :func:`normalized_confidence` are wired
in subsequent dimension-by-dimension PRs, each flipping a parity-test row from
"known-divergence" to "must-agree". Importing a name from here does NOT by itself
change an engine's behavior — wiring does.

Imports only the standard library, so it is safe to import from anywhere.
"""

from __future__ import annotations

import re
from enum import IntEnum

# ---------------------------------------------------------------------------
# Tokenizer (DIMENSION 1 — wired first, zero behavior change: all three engines
# already used this exact regex literal).
# ---------------------------------------------------------------------------

#: ASCII alphanumeric runs. Punctuation, underscores, and hyphens split tokens.
TOKEN_RE = re.compile(r"[a-zA-Z0-9]+")


def tokenize(text: str) -> list[str]:
    """Lowercased ASCII-alphanumeric tokens, in order, duplicates kept.

    The one tokenizer every engine shares. Order and duplicates are preserved so
    callers that need subsequence matching or per-occurrence scoring can do so.
    """
    if not text:
        return []
    return [m.group(0).lower() for m in TOKEN_RE.finditer(text)]


# ---------------------------------------------------------------------------
# Stopwords + meaningful terms (DIMENSION 2 — defined here, wired in a later PR;
# adopting these CHANGES behavior on every engine, so it ships behind the parity
# test, not in the tokenizer PR).
# ---------------------------------------------------------------------------

MIN_TERM_LEN = 3

#: Canonical general-English stopwords — the reconciliation of the codex (58) and
#: cursor (46) lists, keeping every word that is genuinely a general stopword
#: (incl. each list's general-English extras). Domain-shaped words live in
#: DOMAIN_STOPWORDS_CODEX so they don't suppress real anchors on other surfaces.
STOPWORDS: frozenset[str] = frozenset(
    {
        "a", "about", "again", "am", "an", "and", "anything", "are", "as", "at",
        "be", "can", "did", "do", "for", "from", "how", "i", "is", "it", "its",
        "keep", "like", "made", "make", "me", "new", "no", "now", "of", "ok",
        "okay", "on", "or", "our", "please", "should", "so", "some", "tell",
        "that", "the", "then", "there", "this", "to", "us", "want", "was", "we",
        "what", "whats", "when", "where", "which", "will", "with", "work",
        "worked", "working", "would", "yes", "you",
    }
)

#: Codex-prompt-shaped words. The codex engine may union these in (its prompts
#: are full of them); they are NOT universal, so other surfaces must not treat
#: them as stopwords (e.g. "branch"/"pr" can be real anchor terms elsewhere).
DOMAIN_STOPWORDS_CODEX: frozenset[str] = frozenset(
    {"active", "approved", "branch", "codex", "pr", "remind", "restart"}
)


def meaningful_terms(
    text: str, *, extra_stopwords: frozenset[str] = frozenset()
) -> list[str]:
    """Tokens that carry recognition signal: length >= MIN_TERM_LEN and not a
    stopword. Applied SYMMETRICALLY to prompt-side and candidate-name-side tokens
    (this fixes codex's documented asymmetry where the name side skipped the
    length filter). ``extra_stopwords`` lets the codex engine add its domain set.
    """
    stops = STOPWORDS | extra_stopwords
    seen: list[str] = []
    for tok in tokenize(text):
        if len(tok) >= MIN_TERM_LEN and tok not in stops:
            seen.append(tok)
    return seen


# ---------------------------------------------------------------------------
# Match tier ladder (DIMENSION 3 — the shared notion of HOW a candidate matched.
# Engines may WEIGHT tiers differently, but must agree on which tier fired, and
# therefore on whether a candidate matched at all).
# ---------------------------------------------------------------------------


class MatchTier(IntEnum):
    """How strongly a candidate name/alias matched the prompt. Ordered: a higher
    value is a stronger match, so tiers compare and ``max()`` directly."""

    NONE = 0
    TOKEN_OVERLAP = 1  # some meaningful tokens shared, not contiguous
    TOKEN_SUBSEQUENCE = 2  # name tokens appear contiguously within the prompt
    NAME_SUBSTRING = 3  # name string is a substring of the prompt string
    EXACT = 4  # prompt equals the name (normalized)


def _contains_subsequence(haystack: list[str], needle: list[str]) -> bool:
    """True if ``needle`` appears as a contiguous run within ``haystack``."""
    if not needle or len(needle) > len(haystack):
        return False
    first = needle[0]
    for i in range(len(haystack) - len(needle) + 1):
        if haystack[i] == first and haystack[i : i + len(needle)] == needle:
            return True
    return False


def match_tier(
    prompt: str, name: str, *, extra_stopwords: frozenset[str] = frozenset()
) -> MatchTier:
    """The match tier for a single candidate name against the prompt.

    Mirrors codex's prompt-match ladder so all engines agree on the tier:
    EXACT (normalized equality) > NAME_SUBSTRING (name string inside prompt) >
    TOKEN_SUBSEQUENCE (name tokens contiguous in prompt) > TOKEN_OVERLAP
    (meaningful tokens shared) > NONE. The short-name false-positive guard
    (e.g. "ILTTed" must NOT match "ILTT") falls out of this: a bare substring of
    a LONGER token is not a token subsequence, and TOKEN_OVERLAP requires a
    whole-token meaningful match, so a longer word embedding a short name does
    not produce a match.
    """
    p_norm = " ".join(tokenize(prompt))
    n_norm = " ".join(tokenize(name))
    if not n_norm:
        return MatchTier.NONE
    if p_norm == n_norm:
        return MatchTier.EXACT
    # NAME_SUBSTRING is gated on a token-boundary so "ILTT" doesn't match
    # "ILTTed": the name's normalized token string must sit on whole-token
    # boundaries within the prompt's normalized token string.
    if n_norm in p_norm:
        p_tokens = tokenize(prompt)
        n_tokens = tokenize(name)
        if _contains_subsequence(p_tokens, n_tokens):
            return MatchTier.NAME_SUBSTRING
    p_tokens = tokenize(prompt)
    n_tokens = tokenize(name)
    if _contains_subsequence(p_tokens, n_tokens):
        return MatchTier.TOKEN_SUBSEQUENCE
    p_terms = set(meaningful_terms(prompt, extra_stopwords=extra_stopwords))
    n_terms = set(meaningful_terms(name, extra_stopwords=extra_stopwords))
    if p_terms & n_terms:
        return MatchTier.TOKEN_OVERLAP
    return MatchTier.NONE


def best_match_tier(
    prompt: str, names: list[str], *, extra_stopwords: frozenset[str] = frozenset()
) -> MatchTier:
    """Strongest tier across a candidate's name + aliases + focus strings."""
    best = MatchTier.NONE
    for name in names:
        tier = match_tier(prompt, name, extra_stopwords=extra_stopwords)
        if tier > best:
            best = tier
    return best


# ---------------------------------------------------------------------------
# Normalized confidence (DIMENSION 4 — bucket defined on a 0..1 normalized score
# so "same prompt -> same confidence" holds regardless of each engine's raw
# scoring scale. Each engine maps its components into these inputs).
# ---------------------------------------------------------------------------

ConfidenceBucket = str  # one of: "none" | "low" | "medium" | "high"


def normalized_confidence(
    tier: MatchTier,
    *,
    n_anchor_terms: int = 1,
    cwd_hit: bool = False,
    recency_fresh: bool = False,
) -> ConfidenceBucket:
    """Map a match into a surface-independent confidence bucket.

    Driven primarily by the match tier (the thing all engines can compute), with
    small lifts for a cwd/repo hit or fresh recency (signals only some surfaces
    have — absent signals never DEMOTE, so a surface with less data lands at the
    same or lower bucket, never higher). The boundaries are chosen to reproduce
    each engine's current high/medium/low partition on the characterization
    fixtures before any engine cuts its raw-score buckets over to this.
    """
    if tier == MatchTier.NONE:
        return "none"
    score = {
        MatchTier.TOKEN_OVERLAP: 0.30,
        MatchTier.TOKEN_SUBSEQUENCE: 0.55,
        MatchTier.NAME_SUBSTRING: 0.75,
        MatchTier.EXACT: 0.90,
    }[tier]
    if tier == MatchTier.TOKEN_OVERLAP and n_anchor_terms >= 2:
        score += 0.15
    if cwd_hit:
        score += 0.10
    if recency_fresh:
        score += 0.05
    score = round(score, 4)  # avoid float-sum drift at the bucket boundaries
    if score >= 0.80:
        return "high"
    if score >= 0.45:
        return "medium"
    return "low"


# ---------------------------------------------------------------------------
# Top-anchor tie-break (DIMENSION 5 — the deterministic key that makes the
# SELECTED top entity identical across engines for the same match set).
# ---------------------------------------------------------------------------


def recognition_confidence(prompt: str, names: list[str]) -> ConfidenceBucket:
    """The shared confidence bucket for a recognized anchor.

    Driven SOLELY by the strongest match tier across the anchor's name + aliases
    — the one signal every engine computes identically (post match_tier wiring).
    So all surfaces emit the SAME bucket for the same (prompt, anchor), which is
    what closes the last cross-engine divergence (codex/cursor/runtime previously
    bucketed on three different raw scales; runtime emitted none at all). Engine-
    specific signals (recency, cross-agent, cwd) still drive RANKING; they do not
    move the emitted bucket, because the other surfaces can't see them and parity
    requires equality.
    """
    return normalized_confidence(best_match_tier(prompt, names))


def top_anchor_key(tier: MatchTier, recency_ordinal: int, name: str, source: str) -> tuple:
    """Sort key for selecting the top anchor: strongest tier, then most recent,
    then name ascending, then source ascending. Negate the descending fields so
    the key sorts ascending (``min``-first) into the winner."""
    return (-int(tier), -recency_ordinal, name.lower(), source)
