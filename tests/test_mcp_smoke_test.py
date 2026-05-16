"""Tests for scripts/mcp_smoke_test.py argument handling."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "mcp_smoke_test.py"


def _load_mcp_smoke_module():
    spec = importlib.util.spec_from_file_location("mcp_smoke_test", SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_federation_write_roundtrip_requires_explicit_library_path(
    monkeypatch,
    capsys,
):
    module = _load_mcp_smoke_module()
    monkeypatch.setattr(sys, "argv", ["mcp_smoke_test.py", "--federation-write-roundtrip"])

    with pytest.raises(SystemExit) as exc_info:
        module._parse_args()

    assert exc_info.value.code == 2
    assert "require --library-path" in capsys.readouterr().err


def test_isolated_write_smoke_requires_explicit_library_path(monkeypatch, capsys):
    module = _load_mcp_smoke_module()
    monkeypatch.setattr(
        sys,
        "argv",
        ["mcp_smoke_test.py", "--isolate-federation-write-smoke"],
    )

    with pytest.raises(SystemExit) as exc_info:
        module._parse_args()

    assert exc_info.value.code == 2
    assert "require --library-path" in capsys.readouterr().err


def test_read_only_smoke_defaults_to_home_agent_library(monkeypatch, tmp_path):
    module = _load_mcp_smoke_module()
    monkeypatch.setattr(module.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(sys, "argv", ["mcp_smoke_test.py"])

    args = module._parse_args()

    assert args.library_path == str(tmp_path / "agent-library")
