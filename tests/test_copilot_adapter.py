"""Tests for adapters.copilot -- GitHub Copilot external adapter."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

from adapters.base import (
    SPEC_VERSION,
    AdapterDiscoveryError,
    BourdonAdapter,
    HealthStatus,
    L5Manifest,
    Visibility,
)
from adapters.copilot import (
    AGENT_ID,
    AGENT_TYPE,
    CopilotAdapter,
    _build_entity,
    _build_session,
    _inspect_copilot_memory,
    _parse_frontmatter,
    default_copilot_memory_path,
    init_memory_file,
)

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

_POPULATED_MEMORY = """\
---
entities:
  - name: ILTT
    type: project
    summary: AI fitness business platform
    tags: [project, active]
    last_touched: "2026-05-01"
  - name: Bourdon
    type: concept
    summary: Cross-agent memory federation runtime
    tags: [project, active]
  - name: Ryan
    type: person
    summary: Founder
    tags: [personal]
sessions:
  - date: "2026-05-10"
    cwd: /projects/bourdon
    key_actions:
      - Implemented Copilot adapter
    files_touched:
      - adapters/copilot.py
  - date: "2026-04-28"
    cwd: /projects/iltt
    key_actions:
      - Reviewed PR for auth flow
---

# Copilot notes

Some freeform context for Copilot Chat.
"""

_PRIVATE_TAGGED_MEMORY = """\
---
entities:
  - name: MyBankAccount
    type: financial
    summary: Personal banking
    tags: [financial]
  - name: MyPassword
    type: credential
    tags: [credential]
  - name: ILTT
    type: project
    summary: Public project
    tags: [project]
sessions: []
---
"""

_MALFORMED_YAML_MEMORY = """\
---
entities: [unclosed
---
"""

_EMPTY_FRONTMATTER_MEMORY = """\
---
entities: []
sessions: []
---
"""


def _make_copilot_dir(tmp_path: Path) -> Path:
    """Set up a bare copilot-bourdon directory (no memory.md yet)."""
    d = tmp_path / ".copilot-bourdon"
    d.mkdir()
    return d


def _make_copilot_dir_with_memory(tmp_path: Path, content: str = _POPULATED_MEMORY) -> Path:
    d = _make_copilot_dir(tmp_path)
    (d / "memory.md").write_text(content, encoding="utf-8")
    return d


# ---------------------------------------------------------------------------
# _parse_frontmatter
# ---------------------------------------------------------------------------


def test_parse_frontmatter_empty_on_no_delimiter():
    assert _parse_frontmatter("no front-matter here") == {}


def test_parse_frontmatter_empty_on_unclosed_delimiter():
    assert _parse_frontmatter("---\nentities: []\n") == {}


def test_parse_frontmatter_returns_dict_on_valid_yaml():
    result = _parse_frontmatter("---\nentities: []\nsessions: []\n---\n")
    assert result == {"entities": [], "sessions": []}


def test_parse_frontmatter_returns_empty_on_bad_yaml():
    result = _parse_frontmatter("---\n[unclosed\n---\n")
    assert result == {}


def test_parse_frontmatter_ignores_body_below_closing_fence():
    text = "---\nentities: []\n---\n\n# body\nstuff here\n"
    result = _parse_frontmatter(text)
    assert "entities" in result
    assert "body" not in result


# ---------------------------------------------------------------------------
# _build_entity
# ---------------------------------------------------------------------------


def test_build_entity_minimal():
    e = _build_entity({"name": "ILTT"})
    assert e is not None
    assert e.name == "ILTT"
    assert e.type is None
    assert e.summary is None


def test_build_entity_full():
    e = _build_entity({
        "name": "Bourdon",
        "type": "concept",
        "summary": "Memory architecture",
        "aliases": ["continuo"],
        "tags": ["active"],
        "last_touched": "2026-05-01",
        "valid_from": "2026-04-14",
    })
    assert e is not None
    assert e.name == "Bourdon"
    assert e.type == "concept"
    assert e.summary == "Memory architecture"
    assert "continuo" in e.aliases
    assert "active" in e.tags
    assert e.last_touched == "2026-05-01"
    assert e.valid_from == "2026-04-14"


def test_build_entity_returns_none_on_non_dict():
    assert _build_entity("not a dict") is None
    assert _build_entity(None) is None


def test_build_entity_returns_none_on_missing_name():
    assert _build_entity({"type": "project"}) is None
    assert _build_entity({"name": ""}) is None


def test_build_entity_redacts_credential_in_summary():
    e = _build_entity({"name": "Sec", "summary": "My api_key is abc123"})
    assert e is not None
    assert "api_key" not in (e.summary or "")
    assert "redacted" in (e.summary or "").lower()


# ---------------------------------------------------------------------------
# _build_session
# ---------------------------------------------------------------------------


def test_build_session_minimal():
    s = _build_session({"date": "2026-05-10"})
    assert s is not None
    assert s.date == "2026-05-10"
    assert s.cwd is None
    assert s.key_actions == []


def test_build_session_full():
    s = _build_session({
        "date": "2026-05-10T12:00:00Z",  # extra timestamp portion stripped
        "cwd": "/projects/bourdon",
        "key_actions": ["Wrote tests"],
        "files_touched": ["tests/test_copilot_adapter.py"],
        "project_focus": ["bourdon"],
    })
    assert s is not None
    assert s.date == "2026-05-10"
    assert s.cwd == "/projects/bourdon"
    assert "Wrote tests" in s.key_actions
    assert "tests/test_copilot_adapter.py" in s.files_touched
    assert "bourdon" in s.project_focus


def test_build_session_returns_none_on_non_dict():
    assert _build_session("bad") is None


def test_build_session_returns_none_on_missing_date():
    assert _build_session({"cwd": "/somewhere"}) is None


# ---------------------------------------------------------------------------
# discover()
# ---------------------------------------------------------------------------


def test_discover_raises_when_dir_missing(tmp_path):
    adapter = CopilotAdapter(copilot_dir=tmp_path / "does-not-exist")
    with pytest.raises(AdapterDiscoveryError):
        adapter.discover()


def test_discover_returns_agent_store_when_dir_exists(tmp_path):
    d = _make_copilot_dir(tmp_path)
    adapter = CopilotAdapter(copilot_dir=d)
    store = adapter.discover()
    assert store.path == str(d)
    assert "memory_file" in store.metadata
    assert store.metadata["memory_file_present"] is False  # no memory.md yet


def test_discover_reports_memory_file_present(tmp_path):
    d = _make_copilot_dir_with_memory(tmp_path)
    adapter = CopilotAdapter(copilot_dir=d)
    store = adapter.discover()
    assert store.metadata["memory_file_present"] is True


# ---------------------------------------------------------------------------
# export_l5() -- schema conformance
# ---------------------------------------------------------------------------


def test_export_l5_returns_l5manifest(tmp_path):
    d = _make_copilot_dir_with_memory(tmp_path)
    manifest = CopilotAdapter(copilot_dir=d).export_l5()
    assert isinstance(manifest, L5Manifest)


def test_export_l5_agent_fields(tmp_path):
    d = _make_copilot_dir_with_memory(tmp_path)
    manifest = CopilotAdapter(copilot_dir=d).export_l5()
    assert manifest.agent.id == AGENT_ID
    assert manifest.agent.type == AGENT_TYPE
    assert manifest.agent.role_narrative is not None
    assert manifest.spec_version == SPEC_VERSION


def test_export_l5_validates_against_json_schema(tmp_path):
    jsonschema = pytest.importorskip("jsonschema")
    import json

    schema_path = Path(__file__).parent.parent / "spec" / "L5_schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))

    d = _make_copilot_dir_with_memory(tmp_path)
    manifest = CopilotAdapter(copilot_dir=d).export_l5()
    data = manifest.to_dict()
    jsonschema.validate(instance=data, schema=schema)


def test_export_l5_extracts_entities(tmp_path):
    d = _make_copilot_dir_with_memory(tmp_path)
    manifest = CopilotAdapter(copilot_dir=d).export_l5()
    names = {e.name for e in manifest.known_entities}
    assert "ILTT" in names
    assert "Bourdon" in names


def test_export_l5_extracts_sessions(tmp_path):
    d = _make_copilot_dir_with_memory(tmp_path)
    manifest = CopilotAdapter(copilot_dir=d).export_l5()
    assert len(manifest.recent_sessions) == 2
    dates = [s.date for s in manifest.recent_sessions]
    assert "2026-05-10" in dates
    assert "2026-04-28" in dates


def test_export_l5_empty_when_no_memory_file(tmp_path):
    d = _make_copilot_dir(tmp_path)
    manifest = CopilotAdapter(copilot_dir=d).export_l5()
    assert manifest.known_entities == []
    assert manifest.recent_sessions == []


def test_export_l5_empty_when_no_dir(tmp_path):
    adapter = CopilotAdapter(copilot_dir=tmp_path / "missing")
    manifest = adapter.export_l5()
    assert manifest.known_entities == []
    assert manifest.recent_sessions == []


def test_export_l5_capabilities_present(tmp_path):
    d = _make_copilot_dir_with_memory(tmp_path)
    manifest = CopilotAdapter(copilot_dir=d).export_l5()
    assert "inline-completion" in manifest.capabilities
    assert "chat" in manifest.capabilities


# ---------------------------------------------------------------------------
# export_l5() -- visibility filtering (the PII guardrail)
# ---------------------------------------------------------------------------


def test_export_l5_strips_private_tagged_entities(tmp_path):
    d = _make_copilot_dir_with_memory(tmp_path, _PRIVATE_TAGGED_MEMORY)
    manifest = CopilotAdapter(copilot_dir=d).export_l5()
    names = {e.name for e in manifest.known_entities}
    # financial and credential tags should be filtered out
    assert "MyBankAccount" not in names
    assert "MyPassword" not in names
    # public project should survive
    assert "ILTT" in names


def test_export_l5_strips_person_tagged_entities(tmp_path):
    # "personal" tag is in private_tags
    d = _make_copilot_dir_with_memory(tmp_path)
    manifest = CopilotAdapter(copilot_dir=d).export_l5()
    names = {e.name for e in manifest.known_entities}
    assert "Ryan" not in names


# ---------------------------------------------------------------------------
# export_l5() -- since filter
# ---------------------------------------------------------------------------


def test_export_l5_filters_sessions_by_since(tmp_path):
    d = _make_copilot_dir_with_memory(tmp_path)
    cutoff = datetime(2026, 5, 1, tzinfo=timezone.utc)
    manifest = CopilotAdapter(copilot_dir=d).export_l5(since=cutoff)
    for session in manifest.recent_sessions:
        assert session.date >= "2026-05-01"


# ---------------------------------------------------------------------------
# export_sessions()
# ---------------------------------------------------------------------------


def test_export_sessions_respects_limit(tmp_path):
    d = _make_copilot_dir_with_memory(tmp_path)
    sessions = CopilotAdapter(copilot_dir=d).export_sessions(
        since=datetime(2000, 1, 1, tzinfo=timezone.utc), limit=1
    )
    assert len(sessions) == 1


def test_export_sessions_respects_since(tmp_path):
    d = _make_copilot_dir_with_memory(tmp_path)
    sessions = CopilotAdapter(copilot_dir=d).export_sessions(
        since=datetime(2026, 5, 5, tzinfo=timezone.utc)
    )
    for s in sessions:
        assert s.date >= "2026-05-05"


# ---------------------------------------------------------------------------
# health_check()
# ---------------------------------------------------------------------------


def test_health_check_blocked_when_no_dir(tmp_path):
    adapter = CopilotAdapter(copilot_dir=tmp_path / "missing")
    health = adapter.health_check()
    assert health.status == "blocked"
    assert "expected_path" in health.details


def test_health_check_degraded_when_no_memory_file(tmp_path):
    d = _make_copilot_dir(tmp_path)
    health = CopilotAdapter(copilot_dir=d).health_check()
    assert health.status == "degraded"
    assert "memory" in (health.reason or "").lower()


def test_health_check_ok_when_memory_file_present(tmp_path):
    d = _make_copilot_dir_with_memory(tmp_path)
    health = CopilotAdapter(copilot_dir=d).health_check()
    assert health.status == "ok"
    assert health.details["entity_count"] >= 2
    assert health.details["session_count"] == 2


def test_health_check_does_not_raise_on_malformed_yaml(tmp_path):
    d = _make_copilot_dir_with_memory(tmp_path, _MALFORMED_YAML_MEMORY)
    health = CopilotAdapter(copilot_dir=d).health_check()
    assert health.status in {"ok", "degraded", "blocked"}


# ---------------------------------------------------------------------------
# Round-trip via L6Store
# ---------------------------------------------------------------------------


def test_round_trip_through_l6store(tmp_path):
    from core.l5_io import write_l5
    from core.l6_store import L6Store

    d = _make_copilot_dir_with_memory(tmp_path)
    manifest = CopilotAdapter(copilot_dir=d).export_l5()

    agent_library = tmp_path / "agent-library" / "agents"
    agent_library.mkdir(parents=True)
    l5_path = agent_library / "copilot.l5.yaml"
    write_l5(manifest, l5_path)

    store = L6Store(tmp_path / "agent-library")
    agents = store.list_agents()
    assert "copilot" in agents

    result = store.find_entity("ILTT", access_level="team")
    assert result is not None


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------


def test_redaction_in_entity_summary(tmp_path):
    content = """\
---
entities:
  - name: SecretProject
    type: project
    summary: "Has api_key=sk_live_abc123 embedded"
---
"""
    d = _make_copilot_dir_with_memory(tmp_path, content)
    manifest = CopilotAdapter(copilot_dir=d).export_l5()
    for entity in manifest.known_entities:
        assert "sk_live_abc123" not in (entity.summary or "")
        assert "api_key" not in (entity.summary or "").lower()


# ---------------------------------------------------------------------------
# _inspect_copilot_memory
# ---------------------------------------------------------------------------


def test_inspect_reports_missing_when_no_file(tmp_path):
    report = _inspect_copilot_memory(tmp_path / "missing")
    assert report["present"] is False
    assert report["error"] == "missing"


def test_inspect_reports_entity_and_session_counts(tmp_path):
    d = _make_copilot_dir_with_memory(tmp_path)
    report = _inspect_copilot_memory(d)
    assert report["present"] is True
    assert report["readable"] is True
    assert report["frontmatter_valid"] is True
    assert report["entity_count"] == 3  # ILTT, Bourdon, Ryan
    assert report["session_count"] == 2


# ---------------------------------------------------------------------------
# init_memory_file
# ---------------------------------------------------------------------------


def test_init_creates_file_with_template(tmp_path):
    d = tmp_path / ".copilot-bourdon"
    path = init_memory_file(copilot_dir=d)
    assert path.is_file()
    text = path.read_text(encoding="utf-8")
    assert "---" in text
    assert "entities:" in text
    assert "sessions:" in text


def test_init_raises_when_file_exists(tmp_path):
    d = _make_copilot_dir_with_memory(tmp_path)
    with pytest.raises(FileExistsError):
        init_memory_file(copilot_dir=d)


def test_init_force_overwrites(tmp_path):
    d = _make_copilot_dir_with_memory(tmp_path)
    path = init_memory_file(copilot_dir=d, force=True)
    assert path.is_file()
    text = path.read_text(encoding="utf-8")
    assert "Copilot Bourdon Memory" in text


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


def test_copilot_adapter_class_attrs():
    assert CopilotAdapter.agent_id == "copilot"
    assert CopilotAdapter.agent_type == "code-assistant"


def test_native_path_resolves(tmp_path):
    adapter = CopilotAdapter(copilot_dir=tmp_path / ".copilot-bourdon")
    assert adapter.native_path == str(tmp_path / ".copilot-bourdon")


def test_copilot_adapter_satisfies_protocol(tmp_path):
    adapter = CopilotAdapter(copilot_dir=tmp_path / ".copilot-bourdon")
    assert isinstance(adapter, BourdonAdapter)
