"""
Bourdon inference backend protocol -- the contract every local-inference
participant must implement.

This module is deliberately backend-neutral. It exists because the
recognition-first runtime (``core/recognition_runtime.py``) needs to drive
token streaming concurrent with hydration, and a future interrupt-first
primitive will need mid-stream cancel and concurrent slot routing.
Today only llama.cpp's ``llama-server`` supports the full capability set;
Ollama and ``transformers`` cannot. Hard-coding llama.cpp into the runtime
would make every other backend a second-class citizen forever, even after
they catch up. This Protocol prevents that.

Participants live under ``participants/`` and implement this contract. Callers
(``recognition_runtime``, future ``interrupt_runtime``, user orchestrators)
import only from here -- never from ``participants/``. The capability surface
is checked at registration time, so a backend that does not support a
required primitive fails loudly with a structured error -- it never
silently degrades.

Example registration::

    from core.inference_protocol import register_backend

    backend = MyParticipant()  # implements InferenceBackend
    register_backend(backend, required_capabilities={"streaming", "cancel"})

See ``spec/RELATED_WORK.md`` for how this contract relates to other
inference-server APIs (llama.cpp slots, vLLM continuous batching,
TGI sequence groups).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

# -- Errors --------------------------------------------------------------------


class BackendUnsupported(RuntimeError):
    """Raised when a backend lacks a capability the caller required.

    The error message and the ``missing`` attribute both list every missing
    capability so the caller can decide whether to swap backends, drop the
    requirement, or surface a clear error to the user.
    """

    def __init__(self, missing: Iterable[str]) -> None:
        missing_list = tuple(sorted(set(missing)))
        plural = "capability" if len(missing_list) == 1 else "capabilities"
        super().__init__(
            f"Backend missing required {plural}: {', '.join(missing_list)}"
        )
        self.missing: tuple[str, ...] = missing_list


# -- Value types ---------------------------------------------------------------


@dataclass(frozen=True)
class Slot:
    """A backend-side generation slot.

    Backends with a single global generation context (naive transformers
    loops, single-stream Ollama) report a single Slot with ``id=0``.
    Backends with concurrent slots (llama.cpp's ``-np N``, vLLM, TGI)
    report one entry per slot.

    Attributes
    ----------
    id : int
        Stable slot identifier within this backend session. Pass back
        to ``cancel(slot_id)`` and ``stream_completion(slot_id=...)``.
    busy : bool
        True if the slot is currently generating.
    prompt_prefix_hash : str | None
        Backend-specific identifier for the cached prompt prefix on
        this slot. Used by future routing logic to prefer slots that
        already have a useful KV cache. None when the backend does
        not expose this information.
    """

    id: int
    busy: bool
    prompt_prefix_hash: str | None = None


@dataclass(frozen=True)
class BackendCapabilities:
    """Static description of what a backend can and cannot do.

    Returned by ``InferenceBackend.capabilities()``. Constant for the
    lifetime of the backend instance -- if a backend's capabilities
    change at runtime (a model is unloaded, slots are reconfigured),
    it should construct a new instance rather than mutate this struct.

    Capability semantics
    --------------------
    streaming
        ``stream_completion`` yields tokens incrementally. Required for
        any Bourdon use -- recognition-first depends on model response
        streaming concurrent with L1 hydration.
    cancel
        ``cancel(slot_id)`` actually stops generation on the named slot.
        Required for the future ``interrupt_first`` primitive. Backends
        that can only stop by closing the SSE connection should still
        report ``cancel=True`` and implement that path in ``cancel()``.
    concurrent_slots
        Number of slots that can run simultaneously. 1 means serialized;
        >1 means a new request can begin while another is mid-generation.
    kv_cache_reuse
        ``stream_completion(slot_id=...)`` honours the slot's cached
        prompt prefix when the new prompt is a continuation of the
        previous one. Enables the splice case for ``interrupt_first``
        without restarting from scratch.
    """

    streaming: bool
    cancel: bool
    concurrent_slots: int
    kv_cache_reuse: bool

    def supports(self, name: str) -> bool:
        """Boolean view of a named capability.

        ``concurrent_slots`` is treated as supported when value > 1.
        Unknown names return False rather than raising, so callers can
        safely query forward-compatible capability strings against
        older capability structs.
        """
        if name == "concurrent_slots":
            return self.concurrent_slots > 1
        if name == "streaming":
            return self.streaming
        if name == "cancel":
            return self.cancel
        if name == "kv_cache_reuse":
            return self.kv_cache_reuse
        return False


# -- Protocol -----------------------------------------------------------------


@runtime_checkable
class InferenceBackend(Protocol):
    """Backend-neutral inference contract.

    Implementors live under ``participants/`` and provide the four methods
    below. This is a structural Protocol -- participants do not need to
    inherit from anything; matching method signatures is sufficient.

    All async methods must be cancellable via standard asyncio
    cancellation. ``stream_completion`` must be safely re-entrant when
    ``slot_id`` differs across calls (concurrent calls on different
    slots are allowed when capabilities permit).
    """

    def capabilities(self) -> BackendCapabilities:
        """Return this backend's static capability surface.

        Pure function. Cheap. Must not perform I/O.
        """
        ...

    async def slots(self) -> list[Slot]:
        """Return the current state of all slots.

        Implementations with a single context return a list of one.
        Implementations talking to a remote inference server may cache
        this briefly to avoid hammering the server. Must not raise --
        return an empty list if the backend is unreachable and log at
        WARNING. Callers treat an empty result as "backend not ready"
        and degrade rather than crash.
        """
        ...

    async def stream_completion(
        self,
        prompt: str,
        *,
        slot_id: int | None = None,
    ) -> AsyncIterator[str]:
        """Yield tokens from the model, one at a time.

        Parameters
        ----------
        prompt : str
            Full prompt string. Backends are responsible for any
            chat-template wrapping; callers pass already-formatted text.
        slot_id : int | None
            Specific slot to use. None means the backend picks any
            available slot. Backends with ``capabilities.concurrent_slots
            == 1`` must accept None and ignore any non-None value
            (there is only one slot to pick).

        Cancellation: when the consumer stops iterating (drops the
        generator) or the surrounding task is cancelled, the backend
        must stop generation server-side. Failure to do so wastes
        slot capacity and breaks the interrupt-first contract.
        """
        ...

    async def cancel(self, slot_id: int) -> None:
        """Stop generation on the named slot.

        Idempotent: calling cancel on an idle slot is a no-op. Must not
        raise on transient backend errors -- log and return. The caller's
        recovery is to retry or to call ``slots()`` to confirm state.
        """
        ...


# -- Registry -----------------------------------------------------------------


def register_backend(
    backend: object,
    *,
    required_capabilities: Iterable[str] = ("streaming",),
) -> InferenceBackend:
    """Validate ``backend`` against the protocol and required capabilities.

    Returns the backend typed as ``InferenceBackend`` so callers can
    chain registration with use::

        runtime = recognition_first(
            ...,
            inference=register_backend(MyParticipant(), required_capabilities={"streaming"}),
        )

    The default required set is ``{"streaming"}`` -- the minimum any
    Bourdon caller needs. Callers requiring more (e.g. interrupt-first
    needs ``"cancel"``; in-flow splice needs ``"kv_cache_reuse"``) pass
    the extended set.

    Raises
    ------
    TypeError
        If the object does not structurally satisfy ``InferenceBackend``.
    BackendUnsupported
        If any required capability is missing from the backend's
        ``capabilities()`` result.
    """
    if not isinstance(backend, InferenceBackend):
        raise TypeError(
            f"{type(backend).__name__} does not implement InferenceBackend "
            "(missing one or more of: capabilities, slots, stream_completion, cancel)"
        )
    if isinstance(required_capabilities, str):
        raise TypeError(
            "required_capabilities must be an iterable of capability names, "
            "not a bare string"
        )
    caps = backend.capabilities()
    required = set(required_capabilities)
    missing = sorted(name for name in required if not caps.supports(name))
    if missing:
        raise BackendUnsupported(missing)
    return backend
