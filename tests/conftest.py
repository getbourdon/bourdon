"""Shared pytest fixtures for the Bourdon test suite."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

# Make the project root importable so `from core.orchestrator import ...` works
# regardless of where pytest is invoked from.
_PROJECT_ROOT = Path(__file__).parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


@pytest.fixture(autouse=True)
def _isolated_federation_state(tmp_path, monkeypatch):
    """Point the v0.9.0 federation registry + audit log at tmp paths.

    Without this, any test that builds an L6 server would append audit
    entries to the developer's real ~/.bourdon/audit.jsonl and read their
    real federation.yaml.
    """
    monkeypatch.setenv(
        "BOURDON_FEDERATION_CONFIG", str(tmp_path / "federation.yaml")
    )
    monkeypatch.setenv("BOURDON_AUDIT_PATH", str(tmp_path / "audit.jsonl"))


@pytest.fixture
def isolated_memory_dirs(tmp_path, monkeypatch):
    """
    Create an isolated L0 + L1 directory structure for orchestrator tests.

    Monkeypatches core.orchestrator.L0_PATH and L1_DIR to point at tmp dirs
    so tests don't read the repo's actual hot_cache.yaml / l1/*.md.

    Returns a dict with:
        - l0_path: Path to the test hot_cache.yaml
        - l1_dir: Path to the test l1/ directory
        - write_l0(dict): helper to rewrite L0 content
        - write_l1(entity_name, body): helper to create an L1 synopsis
    """
    l0_dir = tmp_path / "l0"
    l0_dir.mkdir()
    l0_path = l0_dir / "hot_cache.yaml"

    l1_dir = tmp_path / "l1"
    l1_dir.mkdir()

    from core import orchestrator

    monkeypatch.setattr(orchestrator, "L0_PATH", l0_path)
    monkeypatch.setattr(orchestrator, "L1_DIR", l1_dir)

    def write_l0(data: dict) -> None:
        l0_path.write_text(yaml.safe_dump(data), encoding="utf-8")

    def write_l1(name: str, body: str) -> None:
        (l1_dir / f"{name}.md").write_text(body, encoding="utf-8")

    # Seed a minimal, valid L0 so Bourdon() can initialize without a TypeError.
    write_l0(
        {
            "identity": {
                "user": "Test User",
                "alias": "Tester",
                "company": "TestCo",
                "role": "QA",
            },
            "projects": [
                {"name": "Alpha", "priority": 1},
                {"name": "Beta", "priority": 3},
            ],
            "hardware": {"local_model": "Gemma", "inference": "Ollama"},
            "current_focus": {
                "primary": "Testing Bourdon",
                "last_session": "2026-04-15",
                "last_topic": "unit tests",
            },
            "entities": [
                {"keyword": "Alpha", "type": "project"},
                {"keyword": "Beta", "type": "project"},
                {"keyword": "Gamma", "type": "concept"},
            ],
        }
    )

    return {
        "l0_path": l0_path,
        "l1_dir": l1_dir,
        "write_l0": write_l0,
        "write_l1": write_l1,
    }
