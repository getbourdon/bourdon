"""Tests for core.l2 -- Episodic Memory async retrieval layer."""

from __future__ import annotations

import asyncio
import sys

import pytest
import yaml

from core import l2 as l2_module
from core.l2 import (
    FastMCPL2Client,
    L2Config,
    _format_l2_context,
    _parse_bool,
    query_l2,
)

# -- L2Config ------------------------------------------------------------------


def test_config_defaults_are_safe():
    """Default config must be disabled so a fresh install doesn't try to connect."""
    cfg = L2Config()
    assert cfg.enabled is False
    assert cfg.endpoint  # must have a placeholder, even if disabled
    assert cfg.top_k > 0
    assert cfg.timeout_seconds > 0


def test_config_from_missing_yaml_returns_defaults(tmp_path):
    cfg = L2Config.from_yaml(tmp_path / "nope.yaml")
    assert cfg.enabled is False


def test_config_from_yaml_loads_overrides(tmp_path):
    path = tmp_path / "l2.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "enabled": True,
                "endpoint": "http://rag.local:9000",
                "tool_name": "custom_search",
                "top_k": 10,
                "timeout_seconds": 3.5,
            }
        ),
        encoding="utf-8",
    )
    cfg = L2Config.from_yaml(path)
    assert cfg.enabled is True
    assert cfg.endpoint == "http://rag.local:9000"
    assert cfg.tool_name == "custom_search"
    assert cfg.top_k == 10
    assert cfg.timeout_seconds == pytest.approx(3.5)


def test_config_partial_yaml_uses_defaults_for_missing_fields(tmp_path):
    path = tmp_path / "l2.yaml"
    path.write_text(yaml.safe_dump({"enabled": True}), encoding="utf-8")
    cfg = L2Config.from_yaml(path)
    assert cfg.enabled is True
    # Other fields retain defaults
    assert cfg.tool_name == "retriever_search"
    assert cfg.top_k == 5


def test_config_malformed_yaml_falls_back_gracefully(tmp_path):
    path = tmp_path / "l2.yaml"
    path.write_text("::this is not: valid yaml: {{{{", encoding="utf-8")
    cfg = L2Config.from_yaml(path)
    # Malformed YAML should not crash -- returns defaults
    assert cfg.enabled is False


def test_config_env_var_overrides_yaml(tmp_path, monkeypatch):
    path = tmp_path / "l2.yaml"
    path.write_text(yaml.safe_dump({"enabled": False, "top_k": 5}), encoding="utf-8")
    monkeypatch.setenv("BOURDON_L2_ENABLED", "true")
    monkeypatch.setenv("BOURDON_L2_TOP_K", "20")
    cfg = L2Config.from_yaml(path)
    assert cfg.enabled is True
    assert cfg.top_k == 20


def test_config_env_var_invalid_int_falls_back(tmp_path, monkeypatch):
    path = tmp_path / "l2.yaml"
    path.write_text(yaml.safe_dump({"top_k": 5}), encoding="utf-8")
    monkeypatch.setenv("BOURDON_L2_TOP_K", "not-a-number")
    cfg = L2Config.from_yaml(path)
    # Bad env value should not crash; keep whatever the YAML/default said
    assert cfg.top_k == 5


def test_config_env_var_endpoint_override(tmp_path, monkeypatch):
    monkeypatch.setenv("BOURDON_L2_ENDPOINT", "http://override.local:1234")
    cfg = L2Config.from_yaml(tmp_path / "nope.yaml")
    assert cfg.endpoint == "http://override.local:1234"


# -- _parse_bool ---------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        (True, True),
        (False, False),
        (1, True),
        (0, False),
        ("true", True),
        ("True", True),
        ("YES", True),
        ("1", True),
        ("false", False),
        ("no", False),
        ("0", False),
        ("off", False),
    ],
)
def test_parse_bool_recognizes_common_forms(raw, expected):
    assert _parse_bool(raw) is expected


def test_parse_bool_returns_none_for_unrecognized():
    assert _parse_bool("maybe") is None
    assert _parse_bool(None) is None
    assert _parse_bool([]) is None


# -- _format_l2_context --------------------------------------------------------


def test_format_l2_context_none_returns_empty():
    assert _format_l2_context(None) == ""


def test_format_l2_context_string_passthrough():
    assert _format_l2_context("  hello  ") == "hello"


def test_format_l2_context_list_of_strings():
    result = _format_l2_context(["one", "two", "three"])
    assert "one" in result
    assert "two" in result
    assert "three" in result


def test_format_l2_context_list_of_dicts_with_content():
    result = _format_l2_context([{"content": "first"}, {"text": "second"}])
    assert "first" in result
    assert "second" in result


def test_format_l2_context_object_with_content_attribute():
    """Simulate an MCP CallToolResult-like object with .content = list[TextContent]."""

    class TextContent:
        def __init__(self, text: str) -> None:
            self.text = text

    class CallResult:
        def __init__(self) -> None:
            self.content = [TextContent("snippet A"), TextContent("snippet B")]

    result = _format_l2_context(CallResult())
    assert "snippet A" in result
    assert "snippet B" in result


# -- query_l2 ------------------------------------------------------------------


class _MockClient:
    """Simple mock that records the call + returns a canned result."""

    def __init__(self, response: str = "mock context", raise_exc: Exception = None) -> None:
        self.response = response
        self.raise_exc = raise_exc
        self.calls: list[tuple[str, int]] = []

    async def query(self, query: str, top_k: int) -> str:
        self.calls.append((query, top_k))
        if self.raise_exc is not None:
            raise self.raise_exc
        return self.response


class _SlowClient:
    """Client that sleeps longer than the timeout so query_l2 times out."""

    async def query(self, query: str, top_k: int) -> str:
        await asyncio.sleep(5.0)
        return "never returned"


@pytest.mark.asyncio
async def test_query_l2_disabled_returns_empty_without_touching_client():
    client = _MockClient(response="should not see this")
    cfg = L2Config(enabled=False)
    result = await query_l2("any query", config=cfg, client=client)
    assert result == ""
    assert client.calls == []  # client was never invoked


@pytest.mark.asyncio
async def test_query_l2_enabled_delegates_to_client():
    client = _MockClient(response="retrieved context")
    cfg = L2Config(enabled=True, top_k=3)
    result = await query_l2("test query", config=cfg, client=client)
    assert result == "retrieved context"
    assert client.calls == [("test query", 3)]


@pytest.mark.asyncio
async def test_query_l2_client_exception_returns_empty():
    client = _MockClient(raise_exc=RuntimeError("backend down"))
    cfg = L2Config(enabled=True)
    result = await query_l2("test query", config=cfg, client=client)
    assert result == ""


@pytest.mark.asyncio
async def test_query_l2_timeout_returns_empty():
    cfg = L2Config(enabled=True, timeout_seconds=0.1)
    result = await query_l2("test query", config=cfg, client=_SlowClient())
    assert result == ""


@pytest.mark.asyncio
async def test_query_l2_uses_config_top_k():
    client = _MockClient()
    cfg = L2Config(enabled=True, top_k=17)
    await query_l2("q", config=cfg, client=client)
    assert client.calls[0][1] == 17


@pytest.mark.asyncio
async def test_query_l2_missing_fastmcp_returns_empty(monkeypatch):
    """When fastmcp is not installed, instantiating FastMCPL2Client raises
    ImportError. query_l2 must swallow it and return empty context."""

    class _FakeFastMCPL2Client:
        def __init__(self, endpoint, tool_name) -> None:
            raise ImportError("fastmcp not installed")

    monkeypatch.setattr(l2_module, "FastMCPL2Client", _FakeFastMCPL2Client)
    cfg = L2Config(enabled=True)
    result = await query_l2("q", config=cfg)  # no explicit client
    assert result == ""


@pytest.mark.asyncio
async def test_query_l2_with_no_config_loads_default_yaml(monkeypatch):
    """Without explicit config, query_l2 loads from bundled YAML (default disabled)."""
    # Ensure env vars don't accidentally flip it on
    for var in ("BOURDON_L2_ENABLED", "BOURDON_L2_TOP_K", "BOURDON_L2_ENDPOINT"):
        monkeypatch.delenv(var, raising=False)
    client = _MockClient(response="SHOULD NOT BE RETURNED")
    # Since bundled config has enabled=false, the default path should return "".
    result = await query_l2("q", client=client)
    assert result == ""
    assert client.calls == []


# -- FastMCPL2Client -----------------------------------------------------------


def test_fastmcp_client_raises_clear_error_when_fastmcp_missing(monkeypatch):
    """Instantiating FastMCPL2Client without fastmcp must raise a helpful ImportError."""
    # Pretend fastmcp isn't importable
    monkeypatch.setitem(sys.modules, "fastmcp", None)
    with pytest.raises(ImportError) as excinfo:
        FastMCPL2Client("http://localhost:8765", "retriever_search")
    assert "fastmcp" in str(excinfo.value).lower()
    assert "ultrarag" in str(excinfo.value).lower()


# -- Integration with orchestrator --------------------------------------------


@pytest.mark.asyncio
async def test_bourdon_prepare_uses_provided_l2_config(isolated_memory_dirs):
    """Bourdon() with an l2_config=enabled config should call the L2 layer."""
    from core.orchestrator import Bourdon

    calls = []

    class _RecordingClient:
        async def query(self, q: str, top_k: int) -> str:
            calls.append((q, top_k))
            return "L2 result text"

    # Monkey-patch the L2 entry point to use our recording client + enabled config
    async def _fake_query_l2_ultrarag(query: str, config=None):
        from core.l2 import query_l2

        cfg = L2Config(enabled=True, top_k=7)
        return await query_l2(query, config=cfg, client=_RecordingClient())

    import core.orchestrator as orch

    original = orch.query_l2_ultrarag
    orch.query_l2_ultrarag = _fake_query_l2_ultrarag
    try:
        memory = Bourdon()
        await memory.prepare("hello Alpha", "Base instructions.")
        # L2 task fires asynchronously; await completion before checking
        l2_result = await memory.get_l2_context()
        assert l2_result == "L2 result text"
        assert calls == [("hello Alpha", 7)]
    finally:
        orch.query_l2_ultrarag = original


@pytest.mark.asyncio
async def test_bourdon_get_l2_context_handles_unfinished_task(isolated_memory_dirs):
    """get_l2_context() should return empty when the L2 task is still running."""
    from core.orchestrator import Bourdon

    async def _slow_l2(query: str, config=None):
        await asyncio.sleep(1.0)
        return "late result"

    import core.orchestrator as orch

    original = orch.query_l2_ultrarag
    orch.query_l2_ultrarag = _slow_l2
    try:
        memory = Bourdon()
        await memory.prepare("msg", "Base")
        # L2 task is in flight; asking immediately should not block.
        l2_result_immediate = await memory.get_l2_context()
        assert l2_result_immediate == ""
    finally:
        orch.query_l2_ultrarag = original
