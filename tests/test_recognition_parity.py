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
        # SHORT-NAME GUARD divergence: codex's substring tier matches "iltt"
        # inside "iltted"; cursor + runtime (token-level) do not. The contract's
        # match_tier closes this — when wired, codex should also yield no match.
        "prompt": "we ILTTed the build",
        "codex": _record(["ILTT"], "ILTT", "medium"),
        "cursor": _record([], None, "none"),
        "runtime": _record([], None, None),
        "anchor_agree": False,  # KNOWN-DIVERGENCE -> flip when match_tier wired (stage 3)
        "confidence_agree": False,
    },
    {
        # SUMMARY-MATCH divergence: cursor scores against the summary haystack so
        # "memory" surfaces Bourdon (summary contains "memory") and ranks it
        # TOP; codex/runtime anchor on the named entity "memory".
        "prompt": "pick a category for memory",
        "codex": _record(["memory"], "memory", "medium"),
        "cursor": _record(["Bourdon", "memory"], "Bourdon", "medium"),
        "runtime": _record(["memory"], "memory", None),
        "anchor_agree": False,  # KNOWN-DIVERGENCE (top anchor differs)
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


def test_known_divergences_are_still_divergent(seeded):
    """Guards the ledger itself: the KNOWN-DIVERGENCE rows must STILL diverge
    until their contract dimension is wired. When you close one, this test tells
    you to move it to the must-agree set (delete its entry here)."""
    divergent = {c["prompt"] for c in CASES if not c["anchor_agree"]}
    for prompt in divergent:
        got = _run_all(prompt, seeded)
        tops = {got[e]["top"] for e in ("codex", "cursor", "runtime")}
        assert len(tops) > 1, (
            f"{prompt!r} now AGREES across engines — a contract dimension closed "
            f"it. Move it to the must-agree set and update CASES."
        )


def test_private_never_surfaces_at_team_access_on_any_engine(seeded):
    """MUST-AGREE security parity: a private entity is invisible to every engine
    at team access, regardless of prompt."""
    got = _run_all("private-thing secret roadmap", seeded)
    for engine in ("codex", "cursor", "runtime"):
        assert "private-thing" not in got[engine]["names"], f"{engine} leaked private entity"
