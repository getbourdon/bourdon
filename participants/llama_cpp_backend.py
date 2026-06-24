"""
Bourdon llama.cpp inference backend -- participant for ``llama-server``.

Implements the ``InferenceBackend`` Protocol from
``core/inference_protocol.py`` against llama.cpp's ``llama-server`` HTTP
API: SSE-streaming completion, slot enumeration, and cancel-via-disconnect.

This is the only currently-shipping local inference server that supports
the full capability set Bourdon's recognition-first runtime needs --
streaming, mid-stream cancel, concurrent slots (``-np N``), and
KV-cache reuse (``cache_prompt: true``). Other backends (Ollama,
``transformers``) can implement this Protocol when they catch up.

Optional install:

    pip install 'bourdon[llama-cpp]'

Usage:

    from participants.llama_cpp_backend import LlamaCppBackend
    from core.inference_protocol import register_backend

    backend = register_backend(
        LlamaCppBackend(base_url="http://localhost:8080"),
        required_capabilities={"streaming", "cancel"},
    )

    async for token in backend.stream_completion("Hello"):
        print(token, end="", flush=True)

    # External cancellation of a specific slot:
    await backend.cancel(slot_id=0)

llama-server compatibility: tested against the SSE shape documented in
the ``examples/server`` README of upstream ``llama.cpp`` (the
``data: {json}\\n\\n`` envelope, with each event carrying ``content``,
``stop``, and ``id_slot`` fields). Endpoints used: ``POST /completion``,
``GET /slots``. Older builds without ``/slots`` (pre-flag) report no
slots; the participant degrades to a synthetic single-slot view in that
case rather than failing.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, AsyncIterator, Optional

try:
    import httpx
except ImportError:  # pragma: no cover -- import-time check, exercised at construction
    httpx = None  # type: ignore[assignment]

from core.inference_protocol import BackendCapabilities, Slot

logger = logging.getLogger(__name__)


DEFAULT_BASE_URL = "http://localhost:8080"
DEFAULT_REQUEST_TIMEOUT = 600.0
DEFAULT_CONCURRENT_SLOTS = 1


class LlamaCppBackend:
    """``llama-server`` inference backend.

    Construction is cheap and does no I/O. The first call to ``slots()``
    or ``stream_completion()`` performs network I/O. ``capabilities()``
    is static and side-effect-free.

    Parameters
    ----------
    base_url
        Base URL of the running ``llama-server`` (default ``http://localhost:8080``).
    api_key
        Optional bearer token for ``llama-server`` builds that require
        authentication. Sent as ``Authorization: Bearer <key>``.
    request_timeout
        Per-request timeout in seconds. Generation can take many seconds
        for long prompts; default is generous (10 minutes).
    concurrent_slots
        Number of concurrent slots ``llama-server`` was launched with
        (i.e. its ``-np`` flag). Used in ``capabilities().concurrent_slots``.
        Defaults to 1; pass the actual configured value for backends with
        ``-np N`` so the runtime can route in-flow requests correctly.
    kv_cache_reuse
        Whether to set ``cache_prompt: true`` on completion requests.
        Default True; enables KV-cache reuse when the new prompt is a
        continuation of a previous slot's prompt.
    client
        Optional pre-constructed ``httpx.AsyncClient``. Useful for tests
        (inject a ``MockTransport``) or for sharing a client across
        multiple participants. When provided, ``base_url`` and
        ``request_timeout`` are ignored.
    """

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        *,
        api_key: Optional[str] = None,
        request_timeout: float = DEFAULT_REQUEST_TIMEOUT,
        concurrent_slots: int = DEFAULT_CONCURRENT_SLOTS,
        kv_cache_reuse: bool = True,
        client: Optional["httpx.AsyncClient"] = None,
    ) -> None:
        if httpx is None:
            raise ImportError(
                "httpx is required for LlamaCppBackend. "
                "Install with: pip install 'bourdon[llama-cpp]'"
            )
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._kv_cache_reuse = kv_cache_reuse
        if client is not None:
            self._client = client
            self._owns_client = False
        else:
            self._client = httpx.AsyncClient(
                base_url=self._base_url,
                timeout=httpx.Timeout(request_timeout),
            )
            self._owns_client = True
        self._caps = BackendCapabilities(
            streaming=True,
            cancel=True,
            concurrent_slots=concurrent_slots,
            kv_cache_reuse=kv_cache_reuse,
        )
        # Track in-flight responses per slot so cancel() can disconnect them.
        self._active_responses: dict[int, "httpx.Response"] = {}
        # Stop events allow cancel() to break the SSE consumer loop on the
        # next event boundary even when the underlying connection close is
        # delayed by a transport layer (mocked transports, slow proxies).
        self._stop_events: dict[int, asyncio.Event] = {}

    # -- Protocol surface ------------------------------------------------------

    def capabilities(self) -> BackendCapabilities:
        return self._caps

    async def slots(self) -> list[Slot]:
        try:
            resp = await self._client.get("/slots", headers=self._auth_headers())
            resp.raise_for_status()
            data = resp.json()
        except (httpx.HTTPError, json.JSONDecodeError, ValueError) as exc:
            logger.warning("LlamaCppBackend.slots() failed: %s", exc)
            return []
        if not isinstance(data, list):
            logger.warning(
                "LlamaCppBackend.slots(): expected list, got %s", type(data).__name__
            )
            return []
        slots: list[Slot] = []
        for raw_slot in data:
            if not isinstance(raw_slot, dict):
                continue
            try:
                slots.append(self._parse_slot(raw_slot))
            except (TypeError, ValueError, OverflowError) as exc:
                logger.warning("LlamaCppBackend.slots() failed to parse slot payload: %s", exc)
        return slots

    async def stream_completion(
        self,
        prompt: str,
        *,
        slot_id: Optional[int] = None,
    ) -> AsyncIterator[str]:
        body: dict[str, Any] = {
            "prompt": prompt,
            "stream": True,
            "n_predict": -1,
            "cache_prompt": self._kv_cache_reuse,
        }
        if slot_id is not None:
            body["id_slot"] = slot_id

        headers = self._auth_headers()
        headers["Accept"] = "text/event-stream"

        # The slot a stream is bound to may be unknown until the first event
        # arrives (when slot_id=None). Track tentatively under the requested
        # id, and re-key once observed.
        tracking_key: Optional[int] = slot_id
        stop_event = asyncio.Event()
        try:
            if tracking_key is not None:
                self._stop_events[tracking_key] = stop_event

            async with self._client.stream(
                "POST", "/completion", json=body, headers=headers
            ) as resp:
                resp.raise_for_status()
                if tracking_key is not None:
                    self._active_responses[tracking_key] = resp
                try:
                    async for line in resp.aiter_lines():
                        if stop_event.is_set():
                            return
                        event = _parse_sse_line(line)
                        if event is None:
                            continue
                        if event.get("error"):
                            raise RuntimeError(
                                f"llama-server error: {event['error']}"
                            )
                        # If we did not know the slot at request time, learn it
                        # from the first event that carries one and start tracking.
                        observed = event.get("id_slot")
                        if (
                            tracking_key is None
                            and isinstance(observed, int)
                            and not isinstance(observed, bool)
                        ):
                            tracking_key = observed
                            self._active_responses[tracking_key] = resp
                            self._stop_events[tracking_key] = stop_event
                        content = event.get("content", "")
                        if content:
                            yield content
                        if event.get("stop"):
                            return
                except (
                    httpx.ReadError,
                    httpx.RemoteProtocolError,
                    httpx.StreamClosed,
                ) as exc:
                    # Connection closed mid-read. If cancel() set the stop
                    # event, this is a graceful cancellation and we exit
                    # cleanly. Otherwise it's an unexpected stream failure
                    # and the caller deserves to know.
                    if stop_event.is_set():
                        logger.debug("stream cancelled mid-read: %s", exc)
                        return
                    raise
        finally:
            if tracking_key is not None:
                self._active_responses.pop(tracking_key, None)
                self._stop_events.pop(tracking_key, None)

    async def cancel(self, slot_id: int) -> None:
        """Stop generation on ``slot_id`` via stop-event + connection close.

        Two-pronged: setting the stop event causes the SSE consumer loop to
        return on the next boundary (works even when the upstream emits
        events but is no longer wanted); closing the response detaches the
        HTTP connection (which causes ``llama-server`` to abort generation
        server-side). Idempotent. Logged at debug level on unknown slot.
        """
        evt = self._stop_events.get(slot_id)
        if evt is not None:
            evt.set()
        resp = self._active_responses.get(slot_id)
        if resp is None:
            logger.debug("cancel(%d): no active stream", slot_id)
            return
        try:
            await resp.aclose()
        except Exception as exc:  # noqa: BLE001 -- never raise from cancel
            logger.debug("cancel(%d): connection close raised %s", slot_id, exc)

    # -- Lifecycle -------------------------------------------------------------

    async def aclose(self) -> None:
        """Close the underlying HTTP client when the participant owns it.

        Safe to call multiple times. No-op when the client was injected
        externally (the caller owns lifecycle in that case).
        """
        if self._owns_client:
            await self._client.aclose()

    # -- Internal --------------------------------------------------------------

    def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._api_key}"} if self._api_key else {}

    @staticmethod
    def _parse_slot(raw: dict[str, Any]) -> Slot:
        prompt = raw.get("prompt")
        prefix_hash: Optional[str] = None
        if isinstance(prompt, str) and prompt:
            # llama-server does not expose a content-hash; truncate as a stable
            # routing key. Participants with real hashes can override this.
            prefix_hash = prompt[:64]
        return Slot(
            id=int(raw.get("id", 0)),
            busy=bool(raw.get("is_processing", False)),
            prompt_prefix_hash=prefix_hash,
        )


# -- Module-level helpers ------------------------------------------------------


def _parse_sse_line(line: str) -> Optional[dict[str, Any]]:
    """Parse a single SSE line into an event dict, or None if not data.

    Accepts both ``data: {...}`` and ``data:{...}`` (no space). Returns
    None for empty lines, comment lines (``: ...``), retry directives,
    and malformed JSON. Logs malformed JSON at debug level.
    """
    if not line or line.startswith(":"):
        return None
    if not line.startswith("data:"):
        return None
    payload = line[5:].lstrip()
    if not payload:
        return None
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError:
        logger.debug("Skipping malformed SSE line: %r", line)
        return None
    if not isinstance(parsed, dict):
        return None
    return parsed
