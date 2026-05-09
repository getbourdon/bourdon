"""
Live integration tests for adapters.llama_cpp_backend.

These run against an actual ``llama-server`` instance, exercising real SSE
streaming, real slot enumeration, and real connection-close cancellation
semantics that the unit tests can only approximate with ``MockTransport``.

Requirements
------------
- Explicit opt-in: set ``BOURDON_RUN_LIVE_LLAMA=1``.
- ``llama-server`` running on ``http://localhost:8080`` (override with
  ``BOURDON_LLAMA_URL`` env var) with ``--slots`` enabled.
- Any model loaded; tests do not depend on output quality.
- ``httpx`` installed (``pip install -e '.[llama-cpp]'``).

Skip behavior
-------------
Skipped from CI by default via the ``integration`` pytest marker
(see ``pyproject.toml``). Locally, run with::

    pytest -m integration tests/integration/

The whole module auto-skips unless explicitly enabled, and then also skips if
no ``llama-server`` is reachable. This avoids false failures when another local
service happens to listen on port 8080.
"""

from __future__ import annotations

import asyncio
import os
import socket
import time
from urllib.parse import urlparse

import pytest

from adapters.llama_cpp_backend import LlamaCppBackend
from core.inference_protocol import register_backend

pytestmark = pytest.mark.integration


# -- Configuration -----------------------------------------------------------


LLAMA_URL = os.environ.get("BOURDON_LLAMA_URL", "http://localhost:8080")
RUN_LIVE_LLAMA = os.environ.get("BOURDON_RUN_LIVE_LLAMA", "").strip().lower()


if RUN_LIVE_LLAMA not in {"1", "true", "yes", "on"}:
    pytest.skip(
        "Live llama integration tests are not explicitly enabled; "
        "set BOURDON_RUN_LIVE_LLAMA=1 to run them.",
        allow_module_level=True,
    )


def _server_reachable(url: str, timeout: float = 0.5) -> bool:
    """Cheap port-knock so the module can self-skip when the server is down."""
    parsed = urlparse(url)
    host = parsed.hostname or "localhost"
    port = parsed.port or 8080
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


if not _server_reachable(LLAMA_URL):
    pytest.skip(
        f"llama-server not reachable at {LLAMA_URL}; "
        "set BOURDON_LLAMA_URL or start the server. Skipping live tests.",
        allow_module_level=True,
    )


# -- Fixtures ----------------------------------------------------------------


@pytest.fixture
async def backend():
    """Per-test backend with auto-cleanup."""
    b = LlamaCppBackend(base_url=LLAMA_URL, request_timeout=120.0)
    try:
        yield b
    finally:
        await b.aclose()


# -- Tests -------------------------------------------------------------------


async def test_register_backend_against_live_server(backend):
    """Live backend conforms to the Protocol's required-capability set."""
    out = register_backend(backend, required_capabilities={"streaming", "cancel"})
    assert out is backend
    caps = out.capabilities()
    assert caps.streaming is True
    assert caps.cancel is True


async def test_slots_returns_at_least_one(backend):
    """A running llama-server with --slots enabled reports >=1 slot."""
    slots = await backend.slots()
    assert isinstance(slots, list)
    if not slots:
        pytest.skip(
            "llama-server reachable but /slots returned []. "
            "Start with --slots to enable this endpoint."
        )
    assert all(s.id is not None for s in slots)


async def test_stream_completion_yields_real_tokens(backend):
    """End-to-end: send a tiny prompt, receive one or more non-empty token chunks."""
    tokens: list[str] = []
    async for chunk in backend.stream_completion(
        "Reply with the single word: ok\n", slot_id=0
    ):
        tokens.append(chunk)
        if len(tokens) >= 50:
            break  # bounded; we only need to prove streaming works
    assert tokens, "expected at least one token chunk from llama-server"
    joined = "".join(tokens)
    assert joined, "expected non-empty content"


async def test_cancel_actually_stops_generation(backend):
    """
    Issue a long-generation request, cancel mid-stream, verify the consumer
    task exits well inside the timeout budget. This is the real-world
    interrupt-first contract the unit tests can only mock.
    """
    tokens: list[str] = []
    started_at = time.monotonic()
    cancel_at: float | None = None

    async def consume() -> None:
        nonlocal cancel_at
        async for chunk in backend.stream_completion(
            "Write a long detailed story about a dragon. Begin: 'Once upon a time, ",
            slot_id=0,
        ):
            tokens.append(chunk)
            if len(tokens) == 5:
                # Got enough tokens to know streaming is live; cancel now.
                cancel_at = time.monotonic()
                await backend.cancel(0)

    await asyncio.wait_for(consume(), timeout=30.0)
    elapsed_after_cancel = time.monotonic() - (cancel_at or started_at)

    assert tokens, "should have received tokens before cancel"
    # Generation should have stopped within ~5s of cancel call. Generous
    # bound; in practice this is sub-second for healthy servers.
    assert elapsed_after_cancel < 5.0, (
        f"cancel took {elapsed_after_cancel:.2f}s to stop generation "
        f"(token count: {len(tokens)})"
    )


async def test_concurrent_streams_on_separate_slots(backend):
    """
    When concurrent_slots > 1, a second request should slot in alongside
    the first. Skips when only one slot is configured.
    """
    slots = await backend.slots()
    if len(slots) < 2:
        pytest.skip(
            f"server has {len(slots)} slot(s); need >=2 for concurrent test "
            "(start llama-server with -np 2 or higher)"
        )

    async def small_run(slot_id: int) -> list[str]:
        out: list[str] = []
        async for chunk in backend.stream_completion(
            f"Reply briefly. Slot {slot_id}.", slot_id=slot_id
        ):
            out.append(chunk)
            if len(out) >= 10:
                break
        return out

    # Fire two streams concurrently. Both must yield tokens.
    a, b = await asyncio.gather(small_run(slots[0].id), small_run(slots[1].id))
    assert a and b, f"both streams should yield tokens (got len(a)={len(a)}, len(b)={len(b)})"
