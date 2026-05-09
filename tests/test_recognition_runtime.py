"""Tests for core.recognition_runtime."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from core.inference_protocol import BackendCapabilities, Slot
from core.recognition_runtime import (
    DEFAULT_HYDRATION_TIMEOUT,
    RecognitionResult,
    build_recognition_string,
    build_splice_prompt,
    detect_entities,
    hydrate_l1,
    interrupt_first,
    recognition_first,
)

# ---- detect_entities --------------------------------------------------------


def test_detect_entities_case_insensitive_name():
    manifest = {
        "known_entities": [
            {"name": "OMNIvour", "type": "project"},
        ]
    }
    matches = detect_entities("tell me about omnivour", manifest)
    assert len(matches) == 1
    assert matches[0]["name"] == "OMNIvour"


def test_detect_entities_alias_match():
    manifest = {
        "known_entities": [
            {"name": "ILTT", "aliases": ["if_lift_then_that"], "type": "product"},
        ]
    }
    matches = detect_entities(
        "what is happening with if_lift_then_that today", manifest
    )
    assert len(matches) == 1
    assert matches[0]["name"] == "ILTT"


def test_detect_entities_multiple_matches():
    manifest = {
        "known_entities": [
            {"name": "Alpha"},
            {"name": "Beta"},
            {"name": "Gamma"},
        ]
    }
    matches = detect_entities("alpha and beta together", manifest)
    names = {m["name"] for m in matches}
    assert names == {"Alpha", "Beta"}


def test_detect_entities_no_match_returns_empty_list():
    manifest = {"known_entities": [{"name": "Alpha"}]}
    assert detect_entities("talking about the weather", manifest) == []


def test_detect_entities_handles_non_dict_manifest():
    """Defensive: if a caller passes a non-dict, return [] cleanly."""
    assert detect_entities("anything", "not a dict") == []  # type: ignore[arg-type]


def test_detect_entities_skips_entities_without_string_name():
    manifest = {
        "known_entities": [
            {"name": 12345},  # invalid
            {"name": "RealOne"},
        ]
    }
    matches = detect_entities("realone please", manifest)
    assert len(matches) == 1
    assert matches[0]["name"] == "RealOne"


def test_detect_entities_no_substring_false_positive():
    """Short entity names should not match when embedded in longer words.

    Regression: with the old substring matcher, 'bananas' matched 'NAS'.
    Token-based matching: 'NAS' must appear as its own token to match.
    """
    manifest = {"known_entities": [{"name": "NAS", "type": "project"}]}
    assert detect_entities("the bananas are ripe", manifest) == []
    assert detect_entities("set up the NAS today", manifest) != []


def test_detect_entities_handles_punctuation_and_slashes():
    """Punctuation in entity names is treated as token separators on both sides."""
    manifest = {
        "known_entities": [{"name": "DINOs Chess/Checkers", "type": "project"}]
    }
    # Full phrase, written as in the entity name -- punctuation differs but
    # tokens align.
    assert detect_entities("how is DINOs Chess/Checkers doing", manifest) != []
    assert detect_entities("dinos chess checkers status", manifest) != []
    # Single token from a multi-token entity should NOT match without alias
    assert detect_entities("just talking about checkers", manifest) == []


def test_detect_entities_alias_matches_when_full_name_does_not():
    """Aliases let multi-word entities respond to shorter user forms."""
    manifest = {
        "known_entities": [
            {
                "name": "DINOs Chess/Checkers",
                "aliases": ["checkers"],
                "type": "project",
            }
        ]
    }
    assert detect_entities("checkers status?", manifest) != []


def test_detect_entities_token_match_requires_contiguous_run():
    """Tokens of the candidate must appear *contiguous* in the message,
    not interleaved with unrelated words."""
    manifest = {
        "known_entities": [{"name": "Capova Connect", "type": "project"}]
    }
    # Same tokens, contiguous → match
    assert detect_entities("how's Capova Connect doing", manifest) != []
    # Tokens present but split by other words → NO match (partial intent)
    assert detect_entities("Capova ships and Connect comes later", manifest) == []


def test_detect_entities_empty_user_message():
    """Empty / whitespace / punctuation-only messages: no matches; no crash."""
    manifest = {"known_entities": [{"name": "Anything"}]}
    assert detect_entities("", manifest) == []
    assert detect_entities("   ", manifest) == []
    assert detect_entities("!@#$%", manifest) == []


# ---- build_recognition_string ----------------------------------------------


def test_recognition_string_empty_when_no_matches():
    assert build_recognition_string([]) == ""


def test_recognition_string_single_match_with_type():
    s = build_recognition_string([{"name": "OMNIvour", "type": "project"}])
    assert s == "Oh -- OMNIvour, the project."


def test_recognition_string_single_match_without_type():
    s = build_recognition_string([{"name": "OMNIvour"}])
    assert s == "Oh -- OMNIvour."


def test_recognition_string_archived_entity_with_valid_to():
    """valid_to date appears in the recognition suffix."""
    s = build_recognition_string(
        [{"name": "Cyndy", "type": "project", "valid_to": "2026-04-14"}]
    )
    assert "Cyndy" in s
    assert "2026-04-14" in s
    assert "archived" in s


def test_recognition_string_archived_entity_via_tag():
    """End-of-life tag without valid_to gets generic '(archived)' suffix."""
    s = build_recognition_string(
        [{"name": "Cyndy", "type": "project", "tags": ["archived"]}]
    )
    assert "(archived)" in s


def test_recognition_string_two_matches():
    s = build_recognition_string([{"name": "Alpha"}, {"name": "Beta"}])
    assert s == "You're asking about Alpha and Beta -- I have both."


def test_recognition_string_three_matches_uses_oxford_comma():
    s = build_recognition_string(
        [{"name": "Alpha"}, {"name": "Beta"}, {"name": "Gamma"}]
    )
    assert s == "You're asking about Alpha, Beta, and Gamma -- I have all of those."


# ---- hydrate_l1 -------------------------------------------------------------


@pytest.mark.asyncio
async def test_hydrate_l1_loads_matching_docs(tmp_path):
    l1_dir = tmp_path / "l1"
    l1_dir.mkdir()
    (l1_dir / "Alpha.md").write_text("# Alpha\nAlpha synopsis.", encoding="utf-8")
    (l1_dir / "Beta.md").write_text("# Beta\nBeta synopsis.", encoding="utf-8")
    matches = [{"name": "Alpha"}, {"name": "Beta"}]
    result = await hydrate_l1(matches, l1_dir=l1_dir)
    assert "Alpha synopsis" in result
    assert "Beta synopsis" in result
    assert "---" in result  # block separator


@pytest.mark.asyncio
async def test_hydrate_l1_empty_when_no_l1_dir():
    matches = [{"name": "Alpha"}]
    assert await hydrate_l1(matches, l1_dir=None) == ""


@pytest.mark.asyncio
async def test_hydrate_l1_empty_when_dir_missing(tmp_path):
    matches = [{"name": "Alpha"}]
    assert await hydrate_l1(matches, l1_dir=tmp_path / "nope") == ""


@pytest.mark.asyncio
async def test_hydrate_l1_case_insensitive_filename_match(tmp_path):
    l1_dir = tmp_path / "l1"
    l1_dir.mkdir()
    (l1_dir / "alpha.md").write_text("Alpha body", encoding="utf-8")
    matches = [{"name": "Alpha"}]  # uppercase request, lowercase file
    result = await hydrate_l1(matches, l1_dir=l1_dir)
    assert "Alpha body" in result


@pytest.mark.asyncio
async def test_hydrate_l1_skips_entity_without_name(tmp_path):
    l1_dir = tmp_path / "l1"
    l1_dir.mkdir()
    matches = [{"type": "project"}]  # no name field
    assert await hydrate_l1(matches, l1_dir=l1_dir) == ""


@pytest.mark.asyncio
async def test_hydrate_l1_returns_empty_when_no_matches():
    assert await hydrate_l1([], l1_dir=Path("/nonexistent")) == ""


# ---- recognition_first (full dispatch) --------------------------------------


def test_recognition_first_recognition_is_synchronous():
    """The recognition string must be available without awaiting anything."""
    manifest = {
        "known_entities": [{"name": "OMNIvour", "type": "project"}]
    }
    result = recognition_first("tell me about OMNIvour", manifest)
    # No event loop needed to read .recognition
    assert result.recognition == "Oh -- OMNIvour, the project."
    assert isinstance(result, RecognitionResult)
    # Close the unawaited hydration coroutine to silence the
    # "coroutine was never awaited" warning. In real use the caller
    # would either await it or close it after extracting recognition.
    if result.hydration is not None:
        result.hydration.close()


def test_recognition_first_no_matches_yields_no_hydration():
    """No-match path doesn't allocate a hydration coroutine."""
    manifest = {"known_entities": [{"name": "Alpha"}]}
    result = recognition_first("totally unrelated", manifest)
    assert result.recognition == ""
    assert result.matched_entities == []
    assert result.hydration is None


@pytest.mark.asyncio
async def test_recognition_first_hydrates_l1_docs_in_parallel(tmp_path):
    l1_dir = tmp_path / "l1"
    l1_dir.mkdir()
    (l1_dir / "OMNIvour.md").write_text("OMNIvour synopsis", encoding="utf-8")
    manifest = {
        "known_entities": [{"name": "OMNIvour", "type": "project"}]
    }
    result = recognition_first(
        "tell me about omnivour", manifest, l1_dir=l1_dir
    )
    assert result.recognition.startswith("Oh -- OMNIvour")
    assert result.hydration is not None
    detail = await result.hydration
    assert "OMNIvour synopsis" in detail


@pytest.mark.asyncio
async def test_recognition_first_hydration_timeout_yields_empty(tmp_path):
    """Slow hydration past the timeout returns "" instead of raising."""
    # Build a manifest that matches; we'll force a slow read by replacing
    # hydrate_l1 with one that sleeps longer than the timeout.
    manifest = {"known_entities": [{"name": "Alpha"}]}

    async def slow_hydrate(*args, **kwargs):
        await asyncio.sleep(2.0)
        return "should never be returned"

    import core.recognition_runtime as rr_module

    original = rr_module.hydrate_l1
    rr_module.hydrate_l1 = slow_hydrate  # type: ignore[assignment]
    try:
        result = recognition_first(
            "alpha please", manifest, hydration_timeout=0.05
        )
        assert result.recognition == "Oh -- Alpha."
        detail = await result.hydration
        assert detail == ""
    finally:
        rr_module.hydrate_l1 = original  # type: ignore[assignment]


def test_recognition_first_visibility_filter_excludes_private_entity():
    """Private-tagged entities are not surfaced even if their name appears in
    the user message at the default 'team' access level."""
    manifest = {
        "known_entities": [
            {"name": "PublicProject", "type": "project"},
            {"name": "SecretSauce", "type": "project", "visibility": "private"},
        ]
    }
    result = recognition_first(
        "tell me about SecretSauce and PublicProject",
        manifest,
        access_level="team",
    )
    matched_names = {e["name"] for e in result.matched_entities}
    # PublicProject must match; SecretSauce must NOT
    assert "PublicProject" in matched_names
    assert "SecretSauce" not in matched_names
    # Close unawaited coroutine for clean test output
    if result.hydration is not None:
        result.hydration.close()


def test_default_hydration_timeout_is_three_seconds():
    """Module-level constant should be 3.0s, the documented thesis budget."""
    assert DEFAULT_HYDRATION_TIMEOUT == 3.0


# ---- interrupt_first --------------------------------------------------------


class _CancelTrackingBackend:
    """Minimal InferenceBackend test double that records cancel() calls.

    Implements only the structural surface needed by interrupt_first; the
    streaming and slot-enumeration paths are no-ops since interrupt_first
    only invokes cancel().
    """

    def __init__(self) -> None:
        self.cancel_calls: list[int] = []
        # Order tracker: append "cancel" or "post-cancel" to verify ordering.
        self.cancel_completed_at: float | None = None

    def capabilities(self) -> BackendCapabilities:
        return BackendCapabilities(
            streaming=True, cancel=True, concurrent_slots=1, kv_cache_reuse=True
        )

    async def slots(self) -> list[Slot]:
        return []

    async def stream_completion(self, prompt, *, slot_id=None):
        for token in []:  # noqa: B007 -- empty async generator
            yield token

    async def cancel(self, slot_id: int) -> None:
        # Tiny await so concurrent ordering is observable in tests.
        await asyncio.sleep(0)
        self.cancel_calls.append(slot_id)


@pytest.mark.asyncio
async def test_interrupt_first_cancels_specified_slot():
    backend = _CancelTrackingBackend()
    manifest = {"known_entities": [{"name": "Apex", "type": "project"}]}
    result = await interrupt_first(
        "tell me about Apex",
        manifest,
        backend=backend,
        slot_to_cancel=3,
    )
    assert backend.cancel_calls == [3]
    assert result.recognition  # entity matched, recognition emitted
    if result.hydration is not None:
        result.hydration.close()


@pytest.mark.asyncio
async def test_interrupt_first_returns_recognition_for_new_message_not_old():
    """The recognition is computed from the NEW message, not whatever was
    being said before the interrupt."""
    backend = _CancelTrackingBackend()
    manifest = {
        "known_entities": [
            {"name": "Foo", "type": "project"},
            {"name": "Bar", "type": "project"},
        ]
    }
    result = await interrupt_first(
        "actually wait, tell me about Bar",
        manifest,
        backend=backend,
        slot_to_cancel=0,
    )
    matched_names = {e["name"] for e in result.matched_entities}
    assert matched_names == {"Bar"}
    assert "Bar" in result.recognition
    if result.hydration is not None:
        result.hydration.close()


@pytest.mark.asyncio
async def test_interrupt_first_returns_no_hydration_when_no_match():
    backend = _CancelTrackingBackend()
    manifest = {"known_entities": [{"name": "Foo", "type": "project"}]}
    result = await interrupt_first(
        "this matches nothing",
        manifest,
        backend=backend,
        slot_to_cancel=0,
    )
    assert result.recognition == ""
    assert result.matched_entities == []
    assert result.hydration is None


@pytest.mark.asyncio
async def test_interrupt_first_provides_hydration_when_match(tmp_path):
    l1_dir = tmp_path / "l1"
    l1_dir.mkdir()
    (l1_dir / "Apex.md").write_text("# Apex\n\nDetailed L1 synopsis.", encoding="utf-8")

    backend = _CancelTrackingBackend()
    manifest = {"known_entities": [{"name": "Apex", "type": "project"}]}
    result = await interrupt_first(
        "tell me about Apex",
        manifest,
        backend=backend,
        slot_to_cancel=0,
        l1_dir=l1_dir,
    )
    assert result.hydration is not None
    detail = await result.hydration
    assert "Detailed L1 synopsis" in detail


@pytest.mark.asyncio
async def test_interrupt_first_respects_access_level():
    """Visibility filter should apply identically to recognition_first."""
    backend = _CancelTrackingBackend()
    manifest = {
        "known_entities": [
            {"name": "PublicProject", "type": "project"},
            {"name": "SecretSauce", "type": "project", "visibility": "private"},
        ]
    }
    result = await interrupt_first(
        "tell me about SecretSauce and PublicProject",
        manifest,
        backend=backend,
        slot_to_cancel=0,
        access_level="team",
    )
    matched_names = {e["name"] for e in result.matched_entities}
    assert "PublicProject" in matched_names
    assert "SecretSauce" not in matched_names
    if result.hydration is not None:
        result.hydration.close()


@pytest.mark.asyncio
async def test_interrupt_first_returns_recognition_result_type():
    """Sanity: same shape as recognition_first."""
    backend = _CancelTrackingBackend()
    manifest = {"known_entities": []}
    result = await interrupt_first(
        "anything", manifest, backend=backend, slot_to_cancel=0
    )
    assert isinstance(result, RecognitionResult)


@pytest.mark.asyncio
async def test_interrupt_first_calls_cancel_before_computing_recognition():
    """Ordering invariant: cancel must complete before recognition is
    composed. If the order flipped, recognition would already be ms behind
    when cancel fires -- which is exactly what the primitive is supposed
    to prevent."""

    order: list[str] = []

    class _OrderingBackend:
        def capabilities(self) -> BackendCapabilities:
            return BackendCapabilities(
                streaming=True, cancel=True, concurrent_slots=1, kv_cache_reuse=False
            )

        async def slots(self) -> list[Slot]:
            return []

        async def stream_completion(self, prompt, *, slot_id=None):
            for t in []:  # noqa: B007
                yield t

        async def cancel(self, slot_id: int) -> None:
            await asyncio.sleep(0.01)  # forced async gap
            order.append("cancel-done")

    # Manifest with an entity so build_recognition_string actually runs.
    manifest = {"known_entities": [{"name": "Z", "type": "project"}]}

    result = await interrupt_first(
        "tell me about Z",
        manifest,
        backend=_OrderingBackend(),
        slot_to_cancel=0,
    )
    order.append("recognition-built")
    assert order == ["cancel-done", "recognition-built"]
    if result.hydration is not None:
        result.hydration.close()


# ---- Layer C: recommended_slot_id + build_splice_prompt --------------------


def test_recognition_first_leaves_recommended_slot_id_none():
    """Plain recognition has no in-flight slot to recommend."""
    manifest = {"known_entities": []}
    result = recognition_first("anything", manifest)
    assert result.recommended_slot_id is None


@pytest.mark.asyncio
async def test_interrupt_first_populates_recommended_slot_id():
    """Interrupt-first dispatches signal which slot the next stream should use."""
    backend = _CancelTrackingBackend()
    manifest = {"known_entities": [{"name": "Beta", "type": "project"}]}
    result = await interrupt_first(
        "tell me about Beta",
        manifest,
        backend=backend,
        slot_to_cancel=2,
    )
    assert result.recommended_slot_id == 2
    if result.hydration is not None:
        result.hydration.close()


@pytest.mark.asyncio
async def test_interrupt_first_recommended_slot_id_matches_cancelled_slot():
    """Whatever slot the caller cancelled is exactly what gets recommended back."""
    backend = _CancelTrackingBackend()
    manifest = {"known_entities": []}
    for slot in [0, 1, 7, 99]:
        result = await interrupt_first(
            "anything",
            manifest,
            backend=backend,
            slot_to_cancel=slot,
        )
        assert result.recommended_slot_id == slot


def test_build_splice_prompt_default_template():
    """Default template threads cancelled context + new request narratively."""
    out = build_splice_prompt(
        cancelled_prompt="Once upon a time, ",
        cancelled_partial="in a land far away there ",
        new_user_msg="actually, tell me about ILTT",
    )
    assert "Once upon a time" in out
    assert "in a land far away" in out
    assert "actually, tell me about ILTT" in out
    assert "Interrupted at this point" in out


def test_build_splice_prompt_handles_empty_partial():
    """Cancel before the first token: cancelled_partial is empty; no crash."""
    out = build_splice_prompt(
        cancelled_prompt="explain something complex",
        cancelled_partial="",
        new_user_msg="never mind, what's ILTT",
    )
    assert "explain something complex" in out
    assert "never mind, what's ILTT" in out


def test_build_splice_prompt_handles_empty_cancelled_prompt():
    """No prior context (e.g. WS first message interrupt): empty prompt is fine."""
    out = build_splice_prompt(
        cancelled_prompt="",
        cancelled_partial="",
        new_user_msg="hi there",
    )
    assert "hi there" in out


def test_build_splice_prompt_custom_template():
    """Caller can override with their own template string."""
    custom = "PRIOR={cancelled_prompt}|GEN={cancelled_partial}|NEW={new_user_msg}"
    out = build_splice_prompt(
        cancelled_prompt="P",
        cancelled_partial="G",
        new_user_msg="N",
        template=custom,
    )
    assert out == "PRIOR=P|GEN=G|NEW=N"


def test_build_splice_prompt_handles_none_args_gracefully():
    """Defensive: None inputs are coerced to empty strings."""
    out = build_splice_prompt(
        cancelled_prompt=None,  # type: ignore[arg-type]
        cancelled_partial=None,  # type: ignore[arg-type]
        new_user_msg=None,  # type: ignore[arg-type]
    )
    # Still returns a (mostly empty) formatted prompt; doesn't crash.
    assert isinstance(out, str)
    assert "Interrupted" in out
