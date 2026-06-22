"""
Bourdon recognition-first runtime.

Emits an immediate recognition response from the L0 / known-entities surface
and concurrently kicks off L1 hydration. The recognition string is computed
synchronously with no I/O and no model call -- it's a template populated from
matched-entity metadata, by deliberate design (see the project's POSITIONING.md
for the thesis grounding). This keeps L0 honest to its architectural role: the
recognition layer is *recognition*, not abbreviated retrieval.

Caller pattern (for an LLM-streaming context):

    result = recognition_first(user_msg, manifest, l1_dir=l1_dir)

    # 1. Emit the recognition immediately (no retrieval has happened yet)
    await stream.send(result.recognition)

    # 2. Start generating the model's main response in parallel with
    #    awaiting hydration:
    detail_future = asyncio.create_task(result.hydration) if result.hydration else None
    async for token in model.stream(user_msg, system_prompt=...):
        await stream.send(token)

    # 3. Inject hydrated detail when available (next turn or appended):
    if detail_future is not None:
        try:
            detail = await detail_future
        except asyncio.TimeoutError:
            detail = ""
        if detail:
            await stream.send_system_context(detail)

The recognition layer NEVER blocks on retrieval. Hydration NEVER blocks the
first response. Hydration NEVER raises -- timeouts and errors yield empty
string so the worst case degrades to "L0-only response" cleanly.

Future replacement for the template-based recognition string is straight-
forward: swap `build_recognition_string` for an LLM-based generator without
changing the public API. The Protocol-shaped boundary makes the swap safe.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Optional

from core.codex_context import filter_manifest_for_access
from core.inference_protocol import InferenceBackend
from core.recognition_contract import MatchTier, match_tier

logger = logging.getLogger(__name__)

DEFAULT_HYDRATION_TIMEOUT = 3.0
"""Seconds. Past this budget, hydration is dropped for the current turn.
Tuned for the thesis: the AI's first response sentence should be ready
in ~0-200ms, so hydration has 2-3 seconds of LLM-generation overlap to
land before the next turn cares."""


# -- Result type ---------------------------------------------------------------


@dataclass
class RecognitionResult:
    """Output of a recognition-first dispatch."""

    recognition: str
    """The immediate, no-retrieval acknowledgment string. Ready to emit as the
    first sentence of the response. Empty string when nothing in the user
    message matched the manifest's known_entities."""

    matched_entities: list[dict[str, Any]] = field(default_factory=list)
    """The entity dicts that triggered recognition. Lets the caller decide
    whether to follow up with hydrated detail."""

    hydration: Optional[Awaitable[str]] = None
    """An awaitable that resolves to the L1-hydrated detail string. The caller
    awaits this in parallel with their own streaming work. None when there are
    no matches (no point hydrating nothing)."""

    recommended_slot_id: Optional[int] = None
    """For interrupt-first dispatches: the slot ID the next ``stream_completion``
    should run on to inherit the cancelled slot's KV cache. ``None`` for
    plain ``recognition_first`` calls (no in-flight slot to continue from).

    When the backend supports KV-cache reuse (``cache_prompt: true`` on
    llama-server, set by ``LlamaCppBackend`` by default), passing this slot
    ID forward lets the new prompt reuse whatever prefix it shares with
    the cancelled prompt -- avoiding a full prompt re-encode.

    See :func:`build_splice_prompt` for the optional helper that constructs
    a "continuation" prompt acknowledging the interrupted context."""


# -- Entity detection (mirrors core.orchestrator but reads a manifest) --------


# Tokenizer + token-subsequence matching now come from the shared recognition
# contract (match_tier), so all surfaces share one match decision + short-name
# guard. See detect_entities below.


def detect_entities(user_msg: str, manifest: dict) -> list[dict]:
    """
    Find entities from the manifest's known_entities mentioned in user_msg.

    Matching is case-insensitive and operates on **tokenized** runs of
    alphanumeric characters -- punctuation and whitespace are treated as
    separators. The candidate (entity name or alias) must appear as a
    contiguous token subsequence within the user message.

    Why token-based rather than substring-based: substring matching had
    short-name false-positives ("ba**NAS**" matched a "NAS" entity, "**ILTT**ed"
    would match "ILTT" inside any longer word, etc.). Tokenizing and
    requiring contiguous-token alignment avoids that without restricting
    multi-word entity names. Aliases remain the right tool for shorter
    forms: an entity called "DINOs Chess/Checkers" with alias "checkers"
    will match a user message of "checkers" *only* via the alias, never
    via partial-name match.
    """
    if not isinstance(manifest, dict):
        return []
    matches: list[dict] = []
    for entity in manifest.get("known_entities") or []:
        if not isinstance(entity, dict):
            continue
        name = entity.get("name") or ""
        if not isinstance(name, str):
            continue
        aliases = entity.get("aliases") or []
        candidates: list[str] = [name]
        for alias in aliases:
            if isinstance(alias, str):
                candidates.append(alias)
        # Shared match decision: a candidate must appear as a contiguous token
        # subsequence (or stronger tier) in the prompt — the contract's ladder,
        # which carries the short-name guard ("ILTTed" never matches "ILTT").
        if any(
            match_tier(user_msg, c) >= MatchTier.TOKEN_SUBSEQUENCE for c in candidates
        ):
            matches.append(entity)
    return matches


# -- Recognition string templates (deliberately deterministic) -----------------


def build_recognition_string(matches: list[dict]) -> str:
    """
    Build the immediate recognition string from matched entities.

    Format rules:
      - 0 matches               -> ""
      - 1 match, with type      -> "Oh -- {name}, the {type}{suffix}."
      - 1 match, no type        -> "Oh -- {name}{suffix}."
      - 2 matches               -> "You're asking about {a} and {b} -- I have both."
      - 3+ matches              -> "You're asking about {a}, {b}, and {c} -- I have all of those."

    The {suffix} appends temporal context when the entity is end-of-life:
      - valid_to set            -> " (archived {valid_to})"
      - archived/canceled tag   -> " (archived)"
      - otherwise               -> ""

    The format is honest about scope: the recognition string acknowledges
    *that* the entity is known, not *what* is known about it. The hydration
    layer carries the actual content. This is the architectural separation
    POSITIONING.md stakes: recognition first, hydration second, archive
    descent third.
    """
    if not matches:
        return ""

    if len(matches) == 1:
        return _single_match_recognition(matches[0])

    names = [str(e.get("name") or "?") for e in matches]
    if len(names) == 2:
        joined = " and ".join(names)
        return f"You're asking about {joined} -- I have both."
    joined = ", ".join(names[:-1]) + f", and {names[-1]}"
    return f"You're asking about {joined} -- I have all of those."


def _single_match_recognition(entity: dict) -> str:
    name = str(entity.get("name") or "this")
    type_str = entity.get("type") or ""
    suffix = _temporal_suffix(entity)
    if type_str:
        return f"Oh -- {name}, the {type_str}{suffix}."
    return f"Oh -- {name}{suffix}."


_END_OF_LIFE_TAGS = frozenset({"archived", "canceled"})


def _temporal_suffix(entity: dict) -> str:
    """Generate the end-of-life suffix for the recognition string."""
    valid_to = entity.get("valid_to")
    if isinstance(valid_to, str) and valid_to:
        return f" (archived {valid_to})"
    tags = entity.get("tags") or []
    if isinstance(tags, list) and any(t in _END_OF_LIFE_TAGS for t in tags):
        return " (archived)"
    return ""


# -- Hydration -----------------------------------------------------------------


async def hydrate_l1(
    matches: list[dict],
    l1_dir: Optional[Path] = None,
) -> str:
    """
    Load L1 synopsis documents for matched entities, in parallel.

    Returns a formatted multi-section string. Empty when no l1_dir is set, the
    directory is missing, or no matching files are present. Never raises --
    the recognition runtime contract is that hydration failures degrade to
    L0-only behavior, never crash the response path.
    """
    if not matches or l1_dir is None:
        return ""
    if not l1_dir.is_dir():
        return ""

    loop = asyncio.get_event_loop()

    def _read_one(entity: dict) -> str:
        name = entity.get("name")
        if not isinstance(name, str) or not name:
            return ""
        # Try exact filename, then case-insensitive scan
        path = l1_dir / f"{name}.md"
        if not path.is_file():
            target = name.lower()
            for alt in l1_dir.glob("*.md"):
                if alt.stem.lower() == target:
                    path = alt
                    break
            else:
                return ""
        try:
            return path.read_text(encoding="utf-8")
        except OSError as exc:
            logger.debug("L1 hydration: failed to read %s: %s", path, exc)
            return ""

    try:
        docs = await asyncio.gather(
            *[loop.run_in_executor(None, _read_one, e) for e in matches]
        )
    except Exception as exc:  # noqa: BLE001 -- never crash hydration
        logger.warning("L1 hydration crashed: %s", exc)
        return ""

    blocks = [d.strip() for d in docs if d.strip()]
    if not blocks:
        return ""
    return "\n\n---\n\n".join(blocks)


# -- Public entry --------------------------------------------------------------


def recognition_first(
    user_msg: str,
    manifest: Any,
    *,
    l1_dir: Optional[Path] = None,
    access_level: str = "team",
    hydration_timeout: float = DEFAULT_HYDRATION_TIMEOUT,
) -> RecognitionResult:
    """
    Compose an immediate recognition response and an awaitable hydration call.

    The recognition string is computed synchronously (no I/O, no model call,
    no retrieval). The hydration coroutine, when present, runs in parallel
    with whatever the caller does next (model generation, response streaming,
    UI updates) up to ``hydration_timeout`` seconds.

    Visibility-filtered: entities marked private at the manifest's
    ``access_level`` are not considered for matching. So a recognition
    response can never accidentally surface a private entity name.

    Returns
    -------
    RecognitionResult
        With:
        - .recognition  -- ready-to-emit recognition string (may be "")
        - .matched_entities  -- the entities that triggered recognition
        - .hydration  -- awaitable that resolves to L1 detail (or None
          when there were no matches)
    """
    filtered = filter_manifest_for_access(manifest, access_level=access_level)
    matches = detect_entities(user_msg, filtered)
    recognition = build_recognition_string(matches)

    if not matches:
        return RecognitionResult(
            recognition=recognition,
            matched_entities=[],
            hydration=None,
        )

    async def _hydration_with_timeout() -> str:
        try:
            return await asyncio.wait_for(
                hydrate_l1(matches, l1_dir=l1_dir),
                timeout=hydration_timeout,
            )
        except asyncio.TimeoutError:
            logger.info(
                "L1 hydration timed out after %.1fs (matches=%d)",
                hydration_timeout,
                len(matches),
            )
            return ""

    return RecognitionResult(
        recognition=recognition,
        matched_entities=matches,
        hydration=_hydration_with_timeout(),
    )


async def interrupt_first(
    new_user_msg: str,
    manifest: Any,
    *,
    backend: InferenceBackend,
    slot_to_cancel: int,
    l1_dir: Optional[Path] = None,
    access_level: str = "team",
    hydration_timeout: float = DEFAULT_HYDRATION_TIMEOUT,
) -> RecognitionResult:
    """
    Cancel an in-flight generation and return a fresh recognition for a new
    user message.

    Symmetric companion to :func:`recognition_first` for the
    speaker-still-talking case: the model is mid-generation on
    ``slot_to_cancel`` and a new user message arrives. This primitive:

      1. Cancels the in-flight generation on ``slot_to_cancel``
         (best-effort, idempotent, never raises -- per the
         ``InferenceBackend.cancel`` contract).
      2. Computes recognition for the new message synchronously.
      3. Sets up the parallel hydration awaitable for the new message.

    The returned :class:`RecognitionResult` has the same shape as
    ``recognition_first``'s, so the caller's downstream pattern is
    identical: emit the recognition string, start a fresh
    ``stream_completion`` for the new message, await hydration in
    parallel.

    KV-cache reuse: when the backend supports it (e.g. ``llama-server``
    with ``cache_prompt: true``, which the bundled
    ``LlamaCppBackend`` enables by default), the next
    ``stream_completion`` call on the same slot will automatically reuse
    any cached prefix that overlaps with the new prompt. No special
    handling is required here; just pass ``slot_id=slot_to_cancel`` to
    the next ``stream_completion`` call.

    Why this is its own primitive rather than ``await backend.cancel(...);
    return recognition_first(...)`` at the call site:

      * **Names the operation.** "Interrupt first" is a first-class
        primitive in the timing thesis, symmetric to "recognition first."
        Downstream code expresses intent.
      * **Locks the cancel-then-recognize order.** Recognizing first then
        cancelling adds milliseconds to the recognition emit; that's the
        entire latency budget the thesis is built on. This wrapper makes
        the right order the only order.
      * **Provides a place to evolve sophistication.** Layer C may add
        KV-cache splice (continue the slot's prompt rather than restart),
        prompt merging (acknowledge the interrupted thread), or history
        threading. Caller code that uses this primitive will not change
        when those land.

    Parameters
    ----------
    new_user_msg
        The interrupting message.
    manifest
        L5 manifest (same shape as :func:`recognition_first`).
    backend
        The :class:`~core.inference_protocol.InferenceBackend` whose
        generation is being interrupted. Used only for ``cancel``.
    slot_to_cancel
        Slot ID of the in-flight generation to stop. Caller is
        responsible for tracking which slot belongs to this session.
    l1_dir, access_level, hydration_timeout
        Forwarded to :func:`recognition_first`.

    Returns
    -------
    RecognitionResult
        For the *new* message. Identical in shape to ``recognition_first``'s
        output, with one addition: ``recommended_slot_id`` is populated
        with ``slot_to_cancel`` so the caller can route the next
        ``stream_completion`` to the same slot for KV-cache reuse.
    """
    await backend.cancel(slot_to_cancel)
    result = recognition_first(
        new_user_msg,
        manifest,
        l1_dir=l1_dir,
        access_level=access_level,
        hydration_timeout=hydration_timeout,
    )
    # Layer C: signal which slot the caller should continue on. The cancelled
    # slot still holds its KV cache server-side; reusing it lets the new
    # prompt share whatever prefix it has with the cancelled one.
    result.recommended_slot_id = slot_to_cancel
    return result


# -- Splice prompt helper (Layer C) --------------------------------------------


_DEFAULT_SPLICE_TEMPLATE = (
    "{cancelled_prompt}{cancelled_partial}\n\n"
    "[Interrupted at this point. New request: {new_user_msg}]\n\n"
)


def build_splice_prompt(
    cancelled_prompt: str,
    cancelled_partial: str,
    new_user_msg: str,
    *,
    template: Optional[str] = None,
) -> str:
    """Compose a continuation prompt that acknowledges the interrupted turn.

    For the speaker-still-talking case, two prompt strategies are valid:

      1. **Hard reset** -- new prompt is just ``new_user_msg``, the model
         starts fresh. KV-cache reuse still applies via the system prompt
         + L0 prefix (substantial).
      2. **Splice** (this function) -- new prompt threads the cancelled
         context into the new turn so the model sees the interruption
         narratively. Useful when the cancelled generation was producing
         signal the new turn should be aware of.

    The default template is::

        {cancelled_prompt}{cancelled_partial}

        [Interrupted at this point. New request: {new_user_msg}]

    Pass ``template`` (a Python format string with ``{cancelled_prompt}``,
    ``{cancelled_partial}``, ``{new_user_msg}`` placeholders) to override.

    Parameters
    ----------
    cancelled_prompt
        The prompt that was being processed when interruption occurred.
        May be empty.
    cancelled_partial
        Tokens emitted before cancellation, joined into a single string.
        May be empty (cancel before first token).
    new_user_msg
        The interrupting message.
    template
        Optional override format string.

    Returns
    -------
    str
        The spliced prompt. Pass to ``backend.stream_completion`` on the
        same slot the cancelled turn ran on (per
        ``RecognitionResult.recommended_slot_id``) for KV-cache reuse.
    """
    tmpl = template if template is not None else _DEFAULT_SPLICE_TEMPLATE
    return tmpl.format(
        cancelled_prompt=cancelled_prompt or "",
        cancelled_partial=cancelled_partial or "",
        new_user_msg=new_user_msg or "",
    )
