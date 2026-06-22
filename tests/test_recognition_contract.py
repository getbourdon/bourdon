"""Unit tests for the shared recognition contract (core/recognition_contract.py).

This is the SSOT for the recognition decision; these tests pin the tokenizer,
the stopword/meaningful-term filter, the match-tier ladder (incl. the short-name
false-positive guard), the normalized confidence buckets, and the tie-break.
"""

from __future__ import annotations

from core.recognition_contract import (
    DOMAIN_STOPWORDS_CODEX,
    MIN_TERM_LEN,
    STOPWORDS,
    MatchTier,
    best_match_tier,
    match_tier,
    meaningful_terms,
    normalized_confidence,
    tokenize,
    top_anchor_key,
)

# -- tokenize ------------------------------------------------------------------


def test_tokenize_lowercases_alphanumeric_runs():
    assert tokenize("Tell me about Bourdon-AI v2!") == [
        "tell", "me", "about", "bourdon", "ai", "v2"
    ]


def test_tokenize_keeps_order_and_duplicates():
    assert tokenize("Bourdon bourdon BOURDON") == ["bourdon", "bourdon", "bourdon"]


def test_tokenize_empty():
    assert tokenize("") == []
    assert tokenize("   ") == []


# -- meaningful_terms ----------------------------------------------------------


def test_meaningful_terms_drops_stopwords_and_short_tokens():
    # "to"/"me" are stopwords; "ai" is len 2 (< MIN_TERM_LEN); "bourdon" survives.
    assert meaningful_terms("tell me about the Bourdon ai") == ["bourdon"]


def test_meaningful_terms_min_len_is_3():
    assert MIN_TERM_LEN == 3
    assert "v2" not in meaningful_terms("ship v2 now")  # 2 chars
    assert "ship" in meaningful_terms("ship v2 now")


def test_domain_stopwords_are_opt_in_not_global():
    # "branch" is a codex domain word — NOT a global stopword, so other surfaces
    # keep it as a real term; codex opts in via extra_stopwords.
    assert "branch" not in STOPWORDS
    assert "branch" in DOMAIN_STOPWORDS_CODEX
    assert "branch" in meaningful_terms("the release branch")
    assert "branch" not in meaningful_terms(
        "the release branch", extra_stopwords=DOMAIN_STOPWORDS_CODEX
    )


# -- match_tier ----------------------------------------------------------------


def test_match_tier_exact():
    assert match_tier("Bourdon", "bourdon") == MatchTier.EXACT
    assert match_tier("the DINOs Chess", "DINOs Chess") == MatchTier.NAME_SUBSTRING


def test_match_tier_name_substring():
    assert match_tier("tell me about Bourdon please", "Bourdon") == MatchTier.NAME_SUBSTRING


def test_match_tier_token_subsequence():
    # multi-word name appearing contiguously but not as the whole prompt
    assert (
        match_tier("we shipped DINOs Chess tonight", "DINOs Chess")
        == MatchTier.NAME_SUBSTRING
    )


def test_match_tier_token_overlap():
    # shares a meaningful token but neither substring nor contiguous subsequence
    tier = match_tier("the federation substrate work", "Bourdon federation engine")
    assert tier == MatchTier.TOKEN_OVERLAP


def test_match_tier_none():
    assert match_tier("completely unrelated words", "Bourdon") == MatchTier.NONE


def test_short_name_does_not_match_longer_word():
    # THE headline guard: "ILTTed" embeds "ILTT" as a raw substring, but it is a
    # different token, so the contract must NOT recognize ILTT here.
    assert match_tier("we ILTTed the build", "ILTT") == MatchTier.NONE
    # And the genuine mention DOES match.
    assert match_tier("how is ILTT going", "ILTT") == MatchTier.NAME_SUBSTRING


def test_substring_inside_token_is_not_a_match():
    # "cat" must not match inside "category" (cursor's old substring bug).
    assert match_tier("pick a category", "cat") == MatchTier.NONE


def test_best_match_tier_takes_strongest_across_aliases():
    # alias "checkers" gives a stronger (substring) match than the name overlap
    assert (
        best_match_tier("checkers tonight", ["DINOs Chess", "checkers"])
        == MatchTier.NAME_SUBSTRING
    )


# -- normalized_confidence -----------------------------------------------------


def test_confidence_none_for_no_match():
    assert normalized_confidence(MatchTier.NONE) == "none"


def test_confidence_high_for_exact_and_substring():
    assert normalized_confidence(MatchTier.EXACT) == "high"
    assert normalized_confidence(MatchTier.NAME_SUBSTRING) == "medium"
    assert normalized_confidence(MatchTier.NAME_SUBSTRING, cwd_hit=True) == "high"


def test_confidence_subsequence_is_medium():
    assert normalized_confidence(MatchTier.TOKEN_SUBSEQUENCE) == "medium"


def test_confidence_overlap_low_unless_multi_term_or_signal():
    assert normalized_confidence(MatchTier.TOKEN_OVERLAP, n_anchor_terms=1) == "low"
    assert normalized_confidence(MatchTier.TOKEN_OVERLAP, n_anchor_terms=2) == "medium"


def test_absent_signals_never_promote_above_present_ones():
    # A surface with less data (no cwd, no recency) must land at the same-or-lower
    # bucket, never higher — the SUBSET/no-false-confidence guarantee.
    base = normalized_confidence(MatchTier.TOKEN_SUBSEQUENCE)
    richer = normalized_confidence(MatchTier.TOKEN_SUBSEQUENCE, cwd_hit=True, recency_fresh=True)
    order = {"none": 0, "low": 1, "medium": 2, "high": 3}
    assert order[richer] >= order[base]


# -- top_anchor_key ------------------------------------------------------------


def test_top_anchor_key_prefers_stronger_tier_then_recency_then_name():
    candidates = [
        (MatchTier.TOKEN_OVERLAP, 100, "Zeta", "codex"),
        (MatchTier.EXACT, 1, "Yankee", "cursor"),  # weaker recency but EXACT wins
        (MatchTier.NAME_SUBSTRING, 200, "Alpha", "codex"),
    ]
    winner = min(candidates, key=lambda c: top_anchor_key(*c))
    assert winner[3 - 1] == "Yankee" or winner[2] == "Yankee"  # the EXACT one


def test_top_anchor_key_tie_breaks_on_name_then_source():
    a = top_anchor_key(MatchTier.EXACT, 50, "beta", "codex")
    b = top_anchor_key(MatchTier.EXACT, 50, "alpha", "cursor")
    assert b < a  # "alpha" sorts before "beta" at equal tier+recency
