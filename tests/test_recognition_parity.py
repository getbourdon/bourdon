"""Cross-engine recognition PARITY / characterization test (3-Star audit
tests-ci-parity-P1-1).

Bourdon's headline promise is "the same prompt is recognized the same way on
every surface". Three engines implement recognition independently:
  - codex   : core.codex_turn_compiler.compile_codex_turn   (rich weighted scorer)
  - cursor  : core.cursor_turn_compiler.compile_cursor_turn (flat additive)
  - runtime : core.recognition_runtime.recognition_first    (no-score boolean)

This test runs all three from ONE seeded library and is the MIGRATION LEDGER for
unifying them onto core.recognition_contract:

  * MUST-AGREE rows assert the engines already agree (and lock it in).
  * KNOWN-DIVERGENCE rows pin today's *disagreement* as the characterization
    baseline. Each is a gap the shared contract is meant to close; when the
    relevant dimension is wired (stopwords / match-tier / normalized confidence),
    flip the row's `agree=` to True and update the per-engine expectation — the
    test then proves the divergence is gone.

So a failure here means recognition behavior MOVED — which on a shipped product
must be a deliberate, reviewed change, not a silent drift.
"""

from __future__ import annotations

import pytest
import yaml

from core.codex_turn_compiler import compile_codex_turn
from core.cursor_turn_compiler import compile_cursor_turn
from core.l6_store import L6Store
from core.recognition_runtime import recognition_first

# -- fixture library -----------------------------------------------------------

_ENTITIES = [
    {"name": "Bourdon", "type": "project", "aliases": ["continuo"],
     "summary": "cross-agent memory federation substrate", "visibility": "public"},
    {"name": "ILTT", "type": "project", "summary": "fitness marketplace app",
     "visibility": "team"},
    {"name": "DINOs Chess", "type": "project", "aliases": ["checkers"],
     "summary": "a chess game", "visibility": "team"},
    {"name": "memory", "type": "topic", "summary": "generic memory notes",
     "visibility": "team"},
    {"name": "private-thing", "type": "topic", "summary": "secret roadmap",
     "visibility": "private"},
]


@pytest.fixture
def seeded(tmp_path):
    agents = tmp_path / "agent-library" / "agents"
    agents.mkdir(parents=True)
    (agents / "codex.l5.yaml").write_text(
        yaml.safe_dump({
            "spec_version": "0.1",
            "agent": {"id": "codex", "type": "code-assistant"},
            "last_updated": "2026-06-20T00:00:00+00:00",
            "known_entities": _ENTITIES,
            "recent_sessions": [],
        }),
        encoding="utf-8",
    )
    lib = tmp_path / "agent-library"
    empty_codex = tmp_path / "empty-codex"
    empty_codex.mkdir()
    return lib, empty_codex


def _record(names: list[str], top: str | None, confidence: str | None) -> dict:
    return {"names": set(names), "top": top, "confidence": confidence}


def _run_all(prompt: str, seeded) -> dict[str, dict]:
    lib, empty_codex = seeded
    store = L6Store(lib)
    manifest = store.build_recognition_manifest(access_level="team")

    cz = compile_codex_turn(prompt, library_path=lib, codex_home=empty_codex, access_level="team")
    cz_names = [i.name for i in cz.items]

    cu = compile_cursor_turn(prompt, library_path=lib, access_level="team")
    cu_names = [m["name"] for m in cu.matched_entities]

    rt = recognition_first(prompt, manifest, access_level="team")
    if getattr(rt, "hydration", None) is not None:
        rt.hydration.close()  # don't leak the un-awaited hydration coroutine
    rt_names = [e.get("name") for e in rt.matched_entities]

    def rec(names: list[str], confidence: str | None) -> dict:
        return _record(names, names[0] if names else None, confidence)

    return {
        "codex": rec(cz_names, cz.routing.get("confidence")),
        "cursor": rec(cu_names, cu.routing.get("confidence")),
        "runtime": rec(rt_names, None),  # runtime emits no confidence today
    }


# Characterization baseline — the CURRENT output of each engine. `anchor_agree`
# / `confidence_agree` mark whether that dimension is unified yet (the ledger).
CASES = [
    {
        "prompt": "tell me about Bourdon",
        "codex": _record(["Bourdon"], "Bourdon", "high"),
        "cursor": _record(["Bourdon"], "Bourdon", "medium"),
        "runtime": _record(["Bourdon"], "Bourdon", None),
        "anchor_agree": True,       # all pick Bourdon — locked in
        "confidence_agree": False,  # high/medium/none — closes when buckets unify (stage 4)
    },
    {
        "prompt": "how is ILTT going",
        "codex": _record(["ILTT"], "ILTT", "medium"),
        "cursor": _record(["ILTT"], "ILTT", "medium"),
        "runtime": _record(["ILTT"], "ILTT", None),
        "anchor_agree": True,
        "confidence_agree": False,
    },
    {
        "prompt": "checkers tonight",
        "codex": _record(["DINOs Chess"], "DINOs Chess", "medium"),
        "cursor": _record(["DINOs Chess"], "DINOs Chess", "medium"),
        "runtime": _record(["DINOs Chess"], "DINOs Chess", None),
        "anchor_agree": True,
        "confidence_agree": False,
    },
    {
        # SHORT-NAME GUARD: codex used to substring-match "iltt" inside "iltted";
        # cursor + runtime (token-level) did not. CLOSED in stage 3 (match_tier):
        # codex now also yields no match — all three agree on no-match.
        "prompt": "we ILTTed the build",
        "codex": _record([], None, "none"),
        "cursor": _record([], None, "none"),
        "runtime": _record([], None, None),
        "anchor_agree": True,  # CONVERGED (was the substring divergence)
        "confidence_agree": False,
    },
    {
        # SUMMARY-MATCH: cursor used to score against the summary haystack so
        # "memory" surfaced Bourdon (summary contains "memory") and ranked it
        # TOP. CLOSED in stage 3 (match on name+aliases only): all three now
        # anchor on the named entity "memory".
        "prompt": "pick a category for memory",
        "codex": _record(["memory"], "memory", "medium"),
        "cursor": _record(["memory"], "memory", "medium"),
        "runtime": _record(["memory"], "memory", None),
        "anchor_agree": True,  # CONVERGED (was the summary-match divergence)
        "confidence_agree": False,
    },
    {
        "prompt": "codex branch pr",
        "codex": _record([], None, "none"),
        "cursor": _record([], None, "none"),
        "runtime": _record([], None, None),
        "anchor_agree": True,   # all none — agree
        "confidence_agree": False,
    },
]


@pytest.mark.parametrize("case", CASES, ids=[c["prompt"] for c in CASES])
def test_recognition_characterization(case, seeded):
    """Pin each engine's CURRENT output. A change here = recognition behavior
    moved; update the baseline deliberately (and flip a ledger row if a contract
    dimension was wired)."""
    got = _run_all(case["prompt"], seeded)
    for engine in ("codex", "cursor", "runtime"):
        assert got[engine] == case[engine], (
            f"{engine} output for {case['prompt']!r} moved: {got[engine]} != {case[engine]}"
        )


@pytest.mark.parametrize("case", [c for c in CASES if c["anchor_agree"]], ids=lambda c: c["prompt"])
def test_top_anchor_parity_where_unified(case, seeded):
    """MUST-AGREE: for rows the engines already agree on, lock in that all three
    select the same top anchor. New drift fails here immediately."""
    got = _run_all(case["prompt"], seeded)
    tops = {got[e]["top"] for e in ("codex", "cursor", "runtime")}
    assert len(tops) == 1, f"top-anchor parity broke for {case['prompt']!r}: {tops}"


def test_anchor_divergences_all_closed_after_stage_3():
    """Stages 1-3 closed every top-anchor divergence: all CASES now agree on the
    selected anchor. If a future change re-opens one, mark that row
    anchor_agree=False and this assertion tells you the ledger moved."""
    assert all(c["anchor_agree"] for c in CASES), (
        "an anchor divergence re-opened — recognition drift; review the diff"
    )


def test_confidence_dimension_not_yet_unified(seeded):
    """Stage 4 ledger: confidence is the remaining un-unified dimension. runtime
    emits no bucket while codex/cursor do, so a matched prompt still disagrees
    on confidence. When normalized_confidence is wired so all three agree, flip
    confidence_agree=True on the CASES and update this test."""
    assert not any(c["confidence_agree"] for c in CASES)  # ledger still pre-stage-4
    got = _run_all("tell me about Bourdon", seeded)
    buckets = {got[e]["confidence"] for e in ("codex", "cursor", "runtime")}
    assert len(buckets) > 1, "confidence converged — wire the stage-4 ledger flip"


def test_private_never_surfaces_at_team_access_on_any_engine(seeded):
    """MUST-AGREE security parity: a private entity is invisible to every engine
    at team access, regardless of prompt."""
    got = _run_all("private-thing secret roadmap", seeded)
    for engine in ("codex", "cursor", "runtime"):
        assert "private-thing" not in got[engine]["names"], f"{engine} leaked private entity"


# -- stage 2: stopword-set convergence (codex/cursor list drift removed) -------

# Words that USED to differ between the engines' private stopword lists:
# cursor-only stopwords (codex kept them as real terms) ...
_WAS_CURSOR_ONLY = ["want", "this", "when", "where", "which", "would", "you", "our", "some", "us"]
# ... and codex-only stopwords (cursor kept them as real terms).
_WAS_CODEX_ONLY = ["did", "should", "tell", "then", "there", "whats"]


@pytest.mark.parametrize("word", _WAS_CURSOR_ONLY + _WAS_CODEX_ONLY)
def test_codex_and_cursor_agree_on_stopwords_now(word):
    """Stage 2: both engines now derive meaningful terms from the ONE canonical
    stopword set, so a word that used to be filtered by only one of them is now
    treated identically. (Both should drop all of these as stopwords.)"""
    from core.codex_turn_compiler import _meaningful_prompt_terms
    from core.cursor_turn_compiler import _extract_prompt_tokens

    prompt = f"the {word} thing here"
    codex_keeps = word in _meaningful_prompt_terms(prompt)
    cursor_keeps = word in _extract_prompt_tokens(prompt)
    assert codex_keeps == cursor_keeps, f"engines still disagree on stopword {word!r}"
    assert not codex_keeps, f"{word!r} should be a canonical stopword on both engines"


def test_codex_domain_stopwords_stay_codex_only():
    """The codex-prompt-shaped words (branch/pr/codex/...) remain stopwords on
    codex (it opts in) but NOT on cursor — they can be real anchor terms
    elsewhere, so they must not be globally suppressed."""
    from core.codex_turn_compiler import _meaningful_prompt_terms
    from core.cursor_turn_compiler import _extract_prompt_tokens

    assert "branch" not in _meaningful_prompt_terms("the release branch")
    assert "branch" in _extract_prompt_tokens("the release branch")
