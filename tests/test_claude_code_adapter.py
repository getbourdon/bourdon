"""Tests for adapters.claude_code -- the Claude Code external adapter (v0.0.3)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

from adapters import claude_code as cc_module
from adapters.base import (
    SPEC_VERSION,
    AdapterDiscoveryError,
    BourdonAdapter,
    Entity,
    HealthStatus,
    L5Manifest,
    Visibility,
)
from adapters.claude_code import (
    ClaudeCodeAdapter,
    _contains_credential_pattern,
    _dedupe_entities,
    _extract_h1_title,
    _extract_status_tag,
    _is_private_type,
    _parse_frontmatter,
    _parse_log_file,
    _parse_project_overview,
    _resolve_auto_memory_path,
)

# -- Fixture: isolated filesystem tree -----------------------------------------


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    """
    Redirect the adapter's path-resolution helpers at a tmp directory tree.
    Returns a helpers dict that tests use to create the three source stores
    with realistic content.
    """
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.delenv("CLAUDE_BRAIN", raising=False)
    monkeypatch.setattr(Path, "home", lambda: fake_home)

    def create_brain():
        brain = fake_home / "claude-brain"
        brain.mkdir()
        (brain / "CURRENT.md").write_text("# Current focus\n", encoding="utf-8")
        (brain / "PROJECTS").mkdir()
        (brain / "LOG").mkdir()
        return brain

    def add_project(brain: Path, name: str, body: str):
        proj = brain / "PROJECTS" / name
        proj.mkdir(parents=True, exist_ok=True)
        (proj / "OVERVIEW.md").write_text(body, encoding="utf-8")

    def add_log(brain: Path, filename: str, body: str):
        (brain / "LOG" / filename).write_text(body, encoding="utf-8")

    def create_auto_memory():
        mem_base = fake_home / ".claude" / "projects" / "C--Users-test"
        mem_dir = mem_base / "memory"
        mem_dir.mkdir(parents=True)
        (mem_dir / "MEMORY.md").write_text("# Memory index\n", encoding="utf-8")
        return mem_dir

    def add_auto_memory_entity(mem_dir: Path, name: str, frontmatter: dict, body: str):
        fm_text = yaml.safe_dump(frontmatter, sort_keys=False).strip()
        content = f"---\n{fm_text}\n---\n{body}"
        (mem_dir / f"{name}.md").write_text(content, encoding="utf-8")

    def create_knowledge_graph():
        kg_dir = fake_home / "claude-memory"
        kg_dir.mkdir()
        return kg_dir / "memory.jsonl"

    def add_graph_entity(kg_path: Path, record: dict):
        with open(kg_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")

    return {
        "home": fake_home,
        "create_brain": create_brain,
        "add_project": add_project,
        "add_log": add_log,
        "create_auto_memory": create_auto_memory,
        "add_auto_memory_entity": add_auto_memory_entity,
        "create_knowledge_graph": create_knowledge_graph,
        "add_graph_entity": add_graph_entity,
    }


# -- Adapter shape + discovery (preserved from v0.0.2) -------------------------


def test_adapter_satisfies_protocol(isolated_home):
    adapter = ClaudeCodeAdapter()
    assert isinstance(adapter, BourdonAdapter)


def test_resolve_auto_memory_path_returns_none_on_permission_error(tmp_path, monkeypatch):
    """Guards #77: the CLI must stay importable when HOME points at an unreadable dir.

    Reproduces the sudo-without-`-H` scenario by pointing Path.home() at a real
    directory but making iterdir() raise PermissionError. The function must
    swallow the error and return None instead of crashing module import.
    """
    fake_home = tmp_path / "unreadable_home"
    (fake_home / ".claude" / "projects").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", lambda: fake_home)

    def boom(self):
        raise PermissionError(13, "Permission denied", str(self))

    monkeypatch.setattr(Path, "iterdir", boom)

    assert _resolve_auto_memory_path() is None


def test_resolve_auto_memory_path_returns_none_on_oserror(tmp_path, monkeypatch):
    """Defense in depth: any OSError reading the projects dir must not crash."""
    fake_home = tmp_path / "broken_home"
    (fake_home / ".claude" / "projects").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", lambda: fake_home)

    def boom(self):
        raise OSError(5, "I/O error", str(self))

    monkeypatch.setattr(Path, "iterdir", boom)

    assert _resolve_auto_memory_path() is None


def test_adapter_exposes_expected_constants():
    assert cc_module.AGENT_ID == "claude-code"
    assert cc_module.AGENT_TYPE == "code-assistant"
    assert "credential" in cc_module.DEFAULT_POLICY.private_tags
    assert "financial" in cc_module.DEFAULT_POLICY.private_tags
    # role_narrative differentiates Claude Code from sibling code-assistants
    assert isinstance(cc_module.ROLE_NARRATIVE, str)
    assert len(cc_module.ROLE_NARRATIVE) <= 500
    assert "manager" in cc_module.ROLE_NARRATIVE.lower()


def test_export_l5_populates_role_narrative(isolated_home):
    isolated_home["create_brain"]()
    manifest = ClaudeCodeAdapter().export_l5()
    assert manifest.agent.role_narrative == cc_module.ROLE_NARRATIVE


def test_discover_raises_when_no_sources(isolated_home):
    adapter = ClaudeCodeAdapter()
    with pytest.raises(AdapterDiscoveryError):
        adapter.discover()


def test_discover_succeeds_with_just_claude_brain(isolated_home):
    isolated_home["create_brain"]()
    adapter = ClaudeCodeAdapter()
    store = adapter.discover()
    assert store.metadata["sources"]["claude_brain"] is not None
    assert store.metadata["sources"]["auto_memory"] is None
    assert store.metadata["sources"]["knowledge_graph"] is None


def test_discover_succeeds_with_all_three_sources(isolated_home):
    isolated_home["create_brain"]()
    isolated_home["create_auto_memory"]()
    kg = isolated_home["create_knowledge_graph"]()
    kg.write_text("", encoding="utf-8")
    adapter = ClaudeCodeAdapter()
    store = adapter.discover()
    assert all(v is not None for v in store.metadata["sources"].values())


def test_discover_env_override_takes_precedence(tmp_path, monkeypatch):
    override_dir = tmp_path / "explicit-brain"
    override_dir.mkdir()
    monkeypatch.setenv("CLAUDE_BRAIN", str(override_dir))
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: fake_home)

    adapter = ClaudeCodeAdapter()
    store = adapter.discover()
    assert store.metadata["sources"]["claude_brain"] == str(override_dir)


# -- health_check (preserved) --------------------------------------------------


def test_health_check_ok_with_all_sources(isolated_home):
    isolated_home["create_brain"]()
    isolated_home["create_auto_memory"]()
    kg = isolated_home["create_knowledge_graph"]()
    kg.write_text("", encoding="utf-8")
    adapter = ClaudeCodeAdapter()
    assert adapter.health_check().status == "ok"


def test_health_check_degraded_with_partial_sources(isolated_home):
    isolated_home["create_brain"]()
    adapter = ClaudeCodeAdapter()
    health = adapter.health_check()
    assert health.status == "degraded"
    assert "1/3" in health.reason


def test_health_check_blocked_with_no_sources(isolated_home):
    adapter = ClaudeCodeAdapter()
    assert adapter.health_check().status == "blocked"


def test_health_check_never_raises(isolated_home):
    adapter = ClaudeCodeAdapter()
    result = adapter.health_check()
    assert isinstance(result, HealthStatus)


# -- Helper: frontmatter + credential + type ------------------------------------


def test_parse_frontmatter_returns_fm_and_body():
    content = "---\nname: Foo\ntype: project\n---\n# Foo\nBody text\n"
    fm, body = _parse_frontmatter(content)
    assert fm == {"name": "Foo", "type": "project"}
    assert body == "# Foo\nBody text\n"


def test_parse_frontmatter_no_frontmatter_returns_empty_dict():
    content = "# No frontmatter here\nJust body.\n"
    fm, body = _parse_frontmatter(content)
    assert fm == {}
    assert body == content


def test_parse_frontmatter_malformed_returns_empty_gracefully():
    content = "---\n{{not yaml{{\n---\n# body\n"
    fm, body = _parse_frontmatter(content)
    # Malformed -> returns empty dict + original text, does not raise
    assert fm == {}


def test_extract_h1_title_strips_separators():
    assert _extract_h1_title("# Clyde -- AI Assistant\n") == "Clyde"
    assert _extract_h1_title("# ILTT: if_lift then_that\n") == "ILTT"
    assert _extract_h1_title("# Simple\n") == "Simple"


def test_extract_status_tag_detects_archived():
    body = "# Project\n\n## Status\n\nArchived (2026-04-14)\n"
    assert "archived" in _extract_status_tag(body)


def test_extract_status_tag_normalizes_cancelled_spelling():
    body = "# Project\n\n## Status\n\nCancelled\n"
    assert "canceled" in _extract_status_tag(body)


def test_extract_status_tag_no_status_returns_empty():
    body = "# Project\n\nNo status section here.\n"
    assert _extract_status_tag(body) == []


def test_contains_credential_pattern_matches_stripe_key():
    assert _contains_credential_pattern("using sk_live_abc123xyz")
    assert _contains_credential_pattern("stripe key is rotated")


def test_contains_credential_pattern_matches_api_key():
    assert _contains_credential_pattern("API_KEY=xyz")
    assert _contains_credential_pattern("api-key in the header")


def test_contains_credential_pattern_matches_env_file():
    assert _contains_credential_pattern("check the .env file for secrets")


def test_contains_credential_pattern_no_false_positive_on_product_description():
    # These should NOT match -- they describe credential-related products, not secrets.
    assert not _contains_credential_pattern("encrypted credential vault")
    assert not _contains_credential_pattern("Stripe integration library")


def test_is_private_type_covers_person_slug_variants():
    assert _is_private_type("person")
    assert _is_private_type("entity/person")
    assert _is_private_type("user")
    assert not _is_private_type("project")
    assert not _is_private_type(None)


# -- Parser: PROJECTS ----------------------------------------------------------


def test_parse_project_overview_extracts_name_and_summary(tmp_path):
    proj_dir = tmp_path / "CLYDE"
    proj_dir.mkdir()
    overview = proj_dir / "OVERVIEW.md"
    overview.write_text(
        "# Clyde -- AI Assistant\n\nAI assistant for Ry. General-purpose agent.\n",
        encoding="utf-8",
    )
    entity = _parse_project_overview(overview)
    assert entity is not None
    assert entity.name == "Clyde"
    assert entity.type == "project"
    assert "AI assistant" in entity.summary


def test_parse_project_overview_extracts_archived_status(tmp_path):
    proj_dir = tmp_path / "CYNDY"
    proj_dir.mkdir()
    overview = proj_dir / "OVERVIEW.md"
    overview.write_text(
        "# Cyndy -- Content Worker\n\n## Status: Archived (2026-04-14)\n\nBuild preserved.\n",
        encoding="utf-8",
    )
    entity = _parse_project_overview(overview)
    assert entity is not None
    assert "archived" in entity.tags


def test_parse_project_overview_archived_sets_valid_to_from_status_date(tmp_path):
    """Archived entity with date in status section: valid_to picks up that date."""
    proj_dir = tmp_path / "CYNDY"
    proj_dir.mkdir()
    overview = proj_dir / "OVERVIEW.md"
    overview.write_text(
        "# Cyndy\n\n## Status: Archived (2026-04-14)\n\nBuild preserved.\n",
        encoding="utf-8",
    )
    entity = _parse_project_overview(overview)
    assert entity is not None
    assert entity.valid_to == "2026-04-14"


def test_parse_project_overview_canceled_picks_up_date(tmp_path):
    """Canceled entity: same valid_to extraction, normalized spelling."""
    proj_dir = tmp_path / "CRAMER"
    proj_dir.mkdir()
    overview = proj_dir / "OVERVIEW.md"
    overview.write_text(
        "# Cramer\n\n## Status\n\nCanceled 2026-04-14 -- never built.\n",
        encoding="utf-8",
    )
    entity = _parse_project_overview(overview)
    assert entity is not None
    assert "canceled" in entity.tags
    assert entity.valid_to == "2026-04-14"


def test_parse_project_overview_archived_without_date_falls_back_to_mtime(tmp_path):
    """Archived entity with no date in status section falls back to file mtime."""
    proj_dir = tmp_path / "OLD"
    proj_dir.mkdir()
    overview = proj_dir / "OVERVIEW.md"
    overview.write_text(
        "# Old\n\n## Status\n\nArchived -- ancient project.\n",
        encoding="utf-8",
    )
    entity = _parse_project_overview(overview)
    assert entity is not None
    assert entity.valid_to is not None
    # mtime fallback => valid ISO date
    from datetime import date as _date

    _date.fromisoformat(entity.valid_to)  # raises if not ISO date


def test_parse_project_overview_active_entity_has_no_valid_to(tmp_path):
    """Active (non-archived/canceled) entity should leave valid_to as None."""
    proj_dir = tmp_path / "ACTIVE"
    proj_dir.mkdir()
    overview = proj_dir / "OVERVIEW.md"
    overview.write_text(
        "# Active\n\n## Status\n\nActive and shipping.\n",
        encoding="utf-8",
    )
    entity = _parse_project_overview(overview)
    assert entity is not None
    assert entity.valid_to is None


# -- Parser: LOG ---------------------------------------------------------------


def test_parse_log_file_extracts_date_and_action(tmp_path):
    log_path = tmp_path / "2026-04-15-pc.md"
    log_path.write_text(
        "# Session Log -- 2026-04-15 (PC)\n\n## Headline\n**SHIPPED v1.0.**\n",
        encoding="utf-8",
    )
    session = _parse_log_file(log_path)
    assert session is not None
    assert session.date == "2026-04-15"
    assert session.key_actions
    assert "SHIPPED" in session.key_actions[0]


def test_parse_log_file_handles_session_suffix(tmp_path):
    log_path = tmp_path / "2026-04-11-pc-session3.md"
    log_path.write_text("# Session Log\n\nContent.\n", encoding="utf-8")
    session = _parse_log_file(log_path)
    assert session is not None
    assert session.date == "2026-04-11"


def test_parse_log_file_ignores_non_matching_filename(tmp_path):
    # File without the YYYY-MM-DD prefix should be rejected
    log_path = tmp_path / "notes.md"
    log_path.write_text("# Not a log\n", encoding="utf-8")
    assert _parse_log_file(log_path) is None


# -- Dedupe --------------------------------------------------------------------


def test_dedupe_merges_by_case_insensitive_name():
    a = [Entity(name="ILTT", type="project", summary="short")]
    b = [Entity(name="iltt", type=None, summary="a much longer summary with more info")]
    merged = _dedupe_entities([a, b])
    assert len(merged) == 1
    # First source wins on name casing
    assert merged[0].name == "ILTT"
    # Longer summary wins
    assert "longer summary" in merged[0].summary


def test_dedupe_unions_tags():
    a = [Entity(name="X", tags=["alpha"])]
    b = [Entity(name="X", tags=["beta"])]
    merged = _dedupe_entities([a, b])
    assert set(merged[0].tags) == {"alpha", "beta"}


def test_dedupe_takes_more_restrictive_visibility():
    a = [Entity(name="X", visibility=Visibility.PUBLIC)]
    b = [Entity(name="X", visibility=Visibility.PRIVATE)]
    merged = _dedupe_entities([a, b])
    assert merged[0].visibility == Visibility.PRIVATE


def test_dedupe_deterministic_ordering():
    a = [Entity(name="Zebra"), Entity(name="Apple")]
    merged = _dedupe_entities([a])
    assert [e.name for e in merged] == ["Apple", "Zebra"]


# -- Full export_l5 ------------------------------------------------------------


def _build_rich_fixture(helpers):
    """Populate all three sources with realistic content for integration tests."""
    brain = helpers["create_brain"]()
    helpers["add_project"](
        brain,
        "ILTT",
        "# ILTT -- if_lift then_that\n\nAI-powered fitness automation.\n",
    )
    helpers["add_project"](
        brain,
        "CYNDY",
        "# Cyndy -- Content Worker\n\n## Status: Archived (2026-04-14)\n\nPreserved.\n",
    )
    helpers["add_log"](
        brain,
        "2026-04-15-pc.md",
        "# Session Log -- 2026-04-15 (PC)\n\n## Headline\n**SHIPPED v0.0.2.**\n",
    )
    helpers["add_log"](
        brain,
        "2026-04-13-mac.md",
        "# Session Log -- 2026-04-13 (Mac)\n\n## Headline\nTheme alignment.\n",
    )

    mem = helpers["create_auto_memory"]()
    helpers["add_auto_memory_entity"](
        mem,
        "iltt",
        {"name": "ILTT", "description": "Fitness platform", "type": "project"},
        "# ILTT\nBody.\n",
    )
    helpers["add_auto_memory_entity"](
        mem,
        "ry_guy",
        {"name": "Ry Guy", "description": "Owner", "type": "person"},
        "# Ry Guy\nBody.\n",
    )

    kg = helpers["create_knowledge_graph"]()
    helpers["add_graph_entity"](
        kg,
        {
            "type": "entity",
            "name": "ILTT",
            "entityType": "entity/product",
            "observations": ["iOS + Android + Web fitness app"],
        },
    )
    helpers["add_graph_entity"](
        kg,
        {
            "type": "entity",
            "name": "SecretEntity",
            "entityType": "entity/infrastructure",
            "observations": ["Uses sk_live_abc123xyz for payments"],
        },
    )
    helpers["add_graph_entity"](
        kg,
        {
            "type": "relation",
            "from": "ILTT",
            "to": "Ry Guy",
            "relationType": "owned_by",
        },
    )
    return brain, mem, kg


def test_export_l5_populates_entities_from_all_sources(isolated_home):
    _build_rich_fixture(isolated_home)
    manifest = ClaudeCodeAdapter().export_l5()
    names = {e.name.lower() for e in manifest.known_entities}
    # ILTT should appear (from all 3 sources, deduped to one entry)
    assert "iltt" in names


def test_export_l5_dedupes_iltt_across_three_sources(isolated_home):
    _build_rich_fixture(isolated_home)
    manifest = ClaudeCodeAdapter().export_l5()
    iltt_matches = [e for e in manifest.known_entities if e.name.lower() == "iltt"]
    assert len(iltt_matches) == 1, f"Expected one ILTT entity, got {len(iltt_matches)}"


def test_export_l5_filters_person_entity(isolated_home):
    _build_rich_fixture(isolated_home)
    manifest = ClaudeCodeAdapter().export_l5()
    names = {e.name.lower() for e in manifest.known_entities}
    assert "ry guy" not in names, "person-typed entity leaked into federated L5"


def test_export_l5_filters_credential_mention_entity(isolated_home):
    _build_rich_fixture(isolated_home)
    manifest = ClaudeCodeAdapter().export_l5()
    names = {e.name.lower() for e in manifest.known_entities}
    assert "secretentity" not in names, "entity with sk_live_* credential leaked"


def test_export_l5_includes_recent_sessions(isolated_home):
    _build_rich_fixture(isolated_home)
    manifest = ClaudeCodeAdapter().export_l5()
    assert len(manifest.recent_sessions) == 2
    dates = {s.date for s in manifest.recent_sessions}
    assert "2026-04-15" in dates
    assert "2026-04-13" in dates


def test_export_l5_sessions_sorted_newest_first(isolated_home):
    _build_rich_fixture(isolated_home)
    manifest = ClaudeCodeAdapter().export_l5()
    dates = [s.date for s in manifest.recent_sessions]
    assert dates == sorted(dates, reverse=True)


def test_export_l5_missing_sources_degrade_gracefully(isolated_home):
    # Only claude-brain, no auto-memory, no knowledge graph
    isolated_home["create_brain"]()
    manifest = ClaudeCodeAdapter().export_l5()
    # Manifest still builds; sessions empty (no LOG entries); entities empty (no PROJECTS entries)
    assert isinstance(manifest, L5Manifest)
    assert manifest.known_entities == []
    assert manifest.recent_sessions == []


def test_export_l5_raises_when_nothing_discovered(isolated_home):
    adapter = ClaudeCodeAdapter()
    with pytest.raises(AdapterDiscoveryError):
        adapter.export_l5()


def test_export_l5_agent_info_has_host_and_spec(isolated_home):
    isolated_home["create_brain"]()
    manifest = ClaudeCodeAdapter().export_l5()
    assert manifest.agent.id == "claude-code"
    assert manifest.agent.type == "code-assistant"
    assert manifest.spec_version == SPEC_VERSION


def test_export_l5_last_updated_is_iso_timestamp(isolated_home):
    isolated_home["create_brain"]()
    manifest = ClaudeCodeAdapter().export_l5()
    # Parse the timestamp to verify it's a valid ISO 8601 string
    parsed = datetime.fromisoformat(manifest.last_updated)
    assert parsed.tzinfo is not None  # tz-aware


def test_export_sessions_respects_since_filter(isolated_home):
    _build_rich_fixture(isolated_home)
    cutoff = datetime(2026, 4, 14, tzinfo=timezone.utc)
    sessions = ClaudeCodeAdapter().export_sessions(since=cutoff)
    # Only 2026-04-15 should be >= cutoff
    assert all(s.date >= "2026-04-14" for s in sessions)
    assert len(sessions) == 1


def test_malformed_jsonl_skipped_not_crashed(isolated_home):
    isolated_home["create_brain"]()
    kg = isolated_home["create_knowledge_graph"]()
    kg.write_text(
        '{"type":"entity","name":"Valid","observations":[]}\n'
        "not json at all\n"
        '{"type":"entity","name":"AlsoValid","observations":[]}\n',
        encoding="utf-8",
    )
    manifest = ClaudeCodeAdapter().export_l5()
    names = {e.name for e in manifest.known_entities}
    # Both valid records should appear; malformed line skipped without crashing
    assert "Valid" in names
    assert "AlsoValid" in names


# -- JSON Schema conformance ---------------------------------------------------


def test_exported_manifest_validates_against_json_schema(isolated_home):
    _build_rich_fixture(isolated_home)
    import jsonschema

    schema_path = Path(__file__).parent.parent / "spec" / "L5_schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))

    manifest = ClaudeCodeAdapter().export_l5()
    serialized = manifest.to_dict()

    # Raises jsonschema.ValidationError on failure
    jsonschema.validate(instance=serialized, schema=schema)
