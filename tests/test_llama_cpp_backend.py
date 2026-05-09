"""Tests for adapters.llama_cpp_backend."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterable

import httpx
import pytest

from adapters.llama_cpp_backend import LlamaCppBackend, _parse_sse_line
from core.inference_protocol import InferenceBackend, Slot

# ---- Test helpers -----------------------------------------------------------


def _sse(events: Iterable[dict]) -> bytes:
    """Serialize event dicts into llama-server-style SSE byte body."""
    return b"".join(
        f"data: {json.dumps(e)}\n\n".encode() for e in events
    )


def _make_backend(handler, **kwargs) -> LlamaCppBackend:
    """Construct a backend with a MockTransport-backed httpx client."""
    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport, base_url="http://test")
    return LlamaCppBackend(client=client, **kwargs)


# ---- _parse_sse_line --------------------------------------------------------


def test_parse_sse_line_empty_returns_none():
    assert _parse_sse_line("") is None


def test_parse_sse_line_comment_returns_none():
    assert _parse_sse_line(": keepalive") is None


def test_parse_sse_line_non_data_returns_none():
    assert _parse_sse_line("event: ping") is None
    assert _parse_sse_line("retry: 1000") is None


def test_parse_sse_line_data_no_payload_returns_none():
    assert _parse_sse_line("data:") is None
    assert _parse_sse_line("data: ") is None


def test_parse_sse_line_malformed_json_returns_none():
    assert _parse_sse_line("data: {not json") is None


def test_parse_sse_line_non_dict_json_returns_none():
    assert _parse_sse_line("data: 42") is None
    assert _parse_sse_line('data: "hello"') is None
    assert _parse_sse_line("data: [1,2,3]") is None


def test_parse_sse_line_valid_event():
    out = _parse_sse_line('data: {"content":"hi","stop":false}')
    assert out == {"content": "hi", "stop": False}


def test_parse_sse_line_handles_no_space_after_colon():
    out = _parse_sse_line('data:{"content":"hi","stop":false}')
    assert out == {"content": "hi", "stop": False}


# ---- Construction & capabilities --------------------------------------------


def test_construction_default_capabilities():
    backend = _make_backend(lambda r: httpx.Response(200, json=[]))
    caps = backend.capabilities()
    assert caps.streaming is True
    assert caps.cancel is True
    assert caps.concurrent_slots == 1
    assert caps.kv_cache_reuse is True


def test_construction_custom_concurrent_slots():
    backend = _make_backend(
        lambda r: httpx.Response(200, json=[]),
        concurrent_slots=4,
    )
    assert backend.capabilities().concurrent_slots == 4


def test_construction_kv_cache_reuse_disabled():
    backend = _make_backend(
        lambda r: httpx.Response(200, json=[]),
        kv_cache_reuse=False,
    )
    assert backend.capabilities().kv_cache_reuse is False


def test_satisfies_inference_backend_protocol():
    backend = _make_backend(lambda r: httpx.Response(200, json=[]))
    assert isinstance(backend, InferenceBackend)


# ---- slots() ----------------------------------------------------------------


async def test_slots_parses_response():
    def handler(request):
        assert request.url.path == "/slots"
        return httpx.Response(
            200,
            json=[
                {"id": 0, "is_processing": False, "prompt": "hi there"},
                {"id": 1, "is_processing": True, "prompt": "another"},
            ],
        )

    backend = _make_backend(handler)
    out = await backend.slots()
    assert out == [
        Slot(id=0, busy=False, prompt_prefix_hash="hi there"),
        Slot(id=1, busy=True, prompt_prefix_hash="another"),
    ]


async def test_slots_returns_empty_list_on_http_error():
    def handler(request):
        return httpx.Response(500, json={"error": "kaboom"})

    backend = _make_backend(handler)
    assert await backend.slots() == []


async def test_slots_returns_empty_list_on_non_list_json():
    def handler(request):
        return httpx.Response(200, json={"not": "a list"})

    backend = _make_backend(handler)
    assert await backend.slots() == []


async def test_slots_skips_non_dict_entries():
    def handler(request):
        return httpx.Response(
            200,
            json=[
                {"id": 0, "is_processing": False},
                "garbage",
                {"id": 1, "is_processing": True},
            ],
        )

    backend = _make_backend(handler)
    out = await backend.slots()
    assert [s.id for s in out] == [0, 1]


async def test_slots_handles_missing_prompt():
    def handler(request):
        return httpx.Response(200, json=[{"id": 0, "is_processing": False}])

    backend = _make_backend(handler)
    out = await backend.slots()
    assert out == [Slot(id=0, busy=False, prompt_prefix_hash=None)]


async def test_slots_preserves_valid_slots_when_one_entry_is_malformed():
    def handler(request):
        return httpx.Response(
            200,
            json=[
                {"id": 0, "is_processing": False},
                {"id": None, "is_processing": True},
                {"id": 2, "is_processing": True},
            ],
        )

    backend = _make_backend(handler)
    out = await backend.slots()
    assert out == [
        Slot(id=0, busy=False, prompt_prefix_hash=None),
        Slot(id=2, busy=True, prompt_prefix_hash=None),
    ]


@pytest.mark.parametrize("raw_id", [None, "abc"])
async def test_slots_returns_empty_list_on_malformed_slot_id(raw_id):
    def handler(request):
        return httpx.Response(
            200,
            json=[{"id": raw_id, "is_processing": False}],
        )

    backend = _make_backend(handler)
    assert await backend.slots() == []


async def test_slots_returns_empty_list_on_infinite_slot_id():
    def handler(request):
        return httpx.Response(
            200,
            content=b'[{"id": 1e309, "is_processing": false}]',
            headers={"content-type": "application/json"},
        )

    backend = _make_backend(handler)
    assert await backend.slots() == []


async def test_slots_sends_auth_header_when_api_key_set():
    captured: dict[str, str | None] = {}

    def handler(request):
        captured["auth"] = request.headers.get("Authorization")
        return httpx.Response(200, json=[])

    backend = _make_backend(handler, api_key="secret-token")
    await backend.slots()
    assert captured["auth"] == "Bearer secret-token"


# ---- stream_completion() ----------------------------------------------------


async def test_stream_completion_yields_tokens_in_order():
    def handler(request):
        body = json.loads(request.content)
        assert body["prompt"] == "Hello"
        assert body["stream"] is True
        return httpx.Response(
            200,
            content=_sse(
                [
                    {"content": "Hi", "stop": False, "id_slot": 0},
                    {"content": " there", "stop": False, "id_slot": 0},
                    {"content": "", "stop": True, "id_slot": 0},
                ]
            ),
            headers={"content-type": "text/event-stream"},
        )

    backend = _make_backend(handler)
    tokens = [t async for t in backend.stream_completion("Hello")]
    assert tokens == ["Hi", " there"]


async def test_stream_completion_skips_empty_content():
    def handler(request):
        return httpx.Response(
            200,
            content=_sse(
                [
                    {"content": "", "stop": False},
                    {"content": "real", "stop": False},
                    {"content": "", "stop": True},
                ]
            ),
        )

    backend = _make_backend(handler)
    tokens = [t async for t in backend.stream_completion("test")]
    assert tokens == ["real"]


async def test_stream_completion_sends_id_slot_when_provided():
    captured: dict[str, dict] = {}

    def handler(request):
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200, content=_sse([{"content": "", "stop": True}])
        )

    backend = _make_backend(handler)
    async for _ in backend.stream_completion("test", slot_id=2):
        pass
    assert captured["body"]["id_slot"] == 2


async def test_stream_completion_omits_id_slot_when_none():
    captured: dict[str, dict] = {}

    def handler(request):
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200, content=_sse([{"content": "", "stop": True}])
        )

    backend = _make_backend(handler)
    async for _ in backend.stream_completion("test"):
        pass
    assert "id_slot" not in captured["body"]


async def test_stream_completion_sets_cache_prompt_per_kv_setting():
    bodies: list[dict] = []

    def handler(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(
            200, content=_sse([{"content": "", "stop": True}])
        )

    on = _make_backend(handler, kv_cache_reuse=True)
    async for _ in on.stream_completion("test"):
        pass
    off = _make_backend(handler, kv_cache_reuse=False)
    async for _ in off.stream_completion("test"):
        pass

    assert bodies[0]["cache_prompt"] is True
    assert bodies[1]["cache_prompt"] is False


async def test_stream_completion_raises_on_error_event():
    def handler(request):
        return httpx.Response(
            200, content=_sse([{"error": "out of memory"}])
        )

    backend = _make_backend(handler)
    with pytest.raises(RuntimeError, match="out of memory"):
        async for _ in backend.stream_completion("test"):
            pass


async def test_stream_completion_skips_malformed_lines():
    def handler(request):
        body = (
            b"data: {malformed\n\n"
            b'data: {"content":"good","stop":false}\n\n'
            b'data: {"content":"","stop":true}\n\n'
        )
        return httpx.Response(200, content=body)

    backend = _make_backend(handler)
    tokens = [t async for t in backend.stream_completion("test")]
    assert tokens == ["good"]


async def test_stream_completion_sends_accept_event_stream_header():
    captured: dict[str, str | None] = {}

    def handler(request):
        captured["accept"] = request.headers.get("Accept")
        return httpx.Response(
            200, content=_sse([{"content": "", "stop": True}])
        )

    backend = _make_backend(handler)
    async for _ in backend.stream_completion("test"):
        pass
    assert captured["accept"] == "text/event-stream"


async def test_stream_completion_ignores_boolean_id_slot_values():
    def handler(request):
        return httpx.Response(
            200,
            content=_sse(
                [
                    {"content": "hello", "stop": False, "id_slot": True},
                    {"content": "", "stop": True, "id_slot": True},
                ]
            ),
        )

    backend = _make_backend(handler)
    sentinel_event = asyncio.Event()
    sentinel_response = httpx.Response(204)
    backend._stop_events[1] = sentinel_event
    backend._active_responses[1] = sentinel_response

    tokens = [t async for t in backend.stream_completion("test")]

    assert tokens == ["hello"]
    assert backend._stop_events == {1: sentinel_event}
    assert backend._active_responses == {1: sentinel_response}


async def test_stream_completion_cleans_stop_event_on_http_error():
    def handler(request):
        return httpx.Response(500, json={"error": "kaboom"})

    backend = _make_backend(handler)
    with pytest.raises(httpx.HTTPStatusError):
        async for _ in backend.stream_completion("test", slot_id=2):
            pass
    assert backend._stop_events == {}


# ---- cancel() ---------------------------------------------------------------


async def test_cancel_unknown_slot_is_noop():
    backend = _make_backend(lambda r: httpx.Response(200, json=[]))
    await backend.cancel(99)  # must not raise


async def test_cancel_breaks_loop_before_next_event():
    """Once cancel(slot) is called, the SSE loop stops yielding even when
    upstream emits more events."""
    proceed = asyncio.Event()

    async def chunks():
        yield b'data: {"content":"hello","stop":false,"id_slot":0}\n\n'
        await proceed.wait()
        yield b'data: {"content":"WORLD","stop":true,"id_slot":0}\n\n'

    def handler(request):
        return httpx.Response(200, content=chunks())

    backend = _make_backend(handler)
    tokens: list[str] = []

    async def consume():
        async for tok in backend.stream_completion("test", slot_id=0):
            tokens.append(tok)
            if tok == "hello":
                await backend.cancel(0)
                proceed.set()

    await asyncio.wait_for(consume(), timeout=2.0)
    assert tokens == ["hello"]


async def test_cancel_swallows_stream_closed_when_reader_is_mid_read():
    """Cancelling from another task should end the stream cleanly."""

    class _BlockingStream(httpx.AsyncByteStream):
        def __init__(self) -> None:
            self.first_chunk_sent = asyncio.Event()
            self.closed = asyncio.Event()

        async def __aiter__(self):
            yield b'data: {"content":"hello","stop":false,"id_slot":0}\n\n'
            self.first_chunk_sent.set()
            await self.closed.wait()
            raise httpx.StreamClosed()

        async def aclose(self) -> None:
            self.closed.set()

    stream = _BlockingStream()

    def handler(request):
        return httpx.Response(200, stream=stream)

    backend = _make_backend(handler)
    tokens: list[str] = []

    async def consume() -> None:
        async for tok in backend.stream_completion("test", slot_id=0):
            tokens.append(tok)

    task = asyncio.create_task(consume())
    await stream.first_chunk_sent.wait()
    await asyncio.sleep(0)
    await backend.cancel(0)
    await asyncio.wait_for(task, timeout=2.0)

    assert tokens == ["hello"]


# ---- aclose() lifecycle ----------------------------------------------------


async def test_aclose_owned_client_closes_it():
    backend = LlamaCppBackend(base_url="http://localhost:9999")
    assert backend._client.is_closed is False
    await backend.aclose()
    assert backend._client.is_closed is True


async def test_aclose_injected_client_does_not_close():
    transport = httpx.MockTransport(lambda r: httpx.Response(200))
    client = httpx.AsyncClient(transport=transport)
    backend = LlamaCppBackend(client=client)
    await backend.aclose()
    assert client.is_closed is False  # caller owns it
    await client.aclose()
