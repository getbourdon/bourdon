"""Tests for adapters.base -- Protocol, dataclasses, visibility resolution."""

from __future__ import annotations

from datetime import datetime

import pytest

from adapters.base import (
    CONTRACT_VERSION,
    MAX_BATCH_EXPORT_LIMIT,
    SPEC_VERSION,
    AdapterCapabilities,
    AdapterCapabilitiesProvider,
    AdapterMetadata,
    AdapterMetadataProvider,
    AdapterSandboxPolicy,
    AdapterSandboxPolicyProvider,
    AgentInfo,
    AgentStore,
    AsyncBourdonAdapter,
    BatchExportAdapter,
    BatchExportOptions,
    BatchExportResult,
    BourdonAdapter,
    Entity,
    HealthStatus,
    L5Manifest,
    Session,
    Visibility,
    VisibilityPolicy,
    apply_visibility,
    filter_for_federation,
)

# -- Visibility enum -----------------------------------------------------------


def test_visibility_enum_values():
    assert Visibility.PUBLIC.value == "public"
    assert Visibility.TEAM.value == "team"
    assert Visibility.PRIVATE.value == "private"


def test_contract_and_spec_versions_are_separate():
    assert CONTRACT_VERSION == "0.2"
    assert SPEC_VERSION == "0.1"


# -- Dataclass shape -----------------------------------------------------------


def test_agent_info_minimal():
    agent = AgentInfo(id="test", type="code-assistant")
    assert agent.id == "test"
    assert agent.type == "code-assistant"
    assert agent.instance is None


def test_entity_defaults():
    e = Entity(name="ILTT")
    assert e.name == "ILTT"
    assert e.aliases == []
    assert e.tags == []
    assert e.visibility is None


def test_l5_manifest_required_fields():
    manifest = L5Manifest(
        spec_version="0.1",
        agent=AgentInfo(id="x", type="code-assistant"),
        last_updated="2026-04-15T12:00:00+00:00",
    )
    assert manifest.spec_version == "0.1"
    assert manifest.agent.id == "x"
    assert manifest.known_entities == []


def test_adapter_metadata_defaults_are_explicit():
    metadata = AdapterMetadata(display_name="Linear")

    assert metadata.display_name == "Linear"
    assert metadata.description == ""
    assert metadata.homepage_url is None
    assert metadata.docs_url is None
    assert metadata.icon is None
    assert metadata.tags == []


def test_adapter_capabilities_default_to_no_optional_extensions():
    capabilities = AdapterCapabilities()

    assert capabilities.supports_incremental is False
    assert capabilities.supports_batch_export is False
    assert capabilities.supports_async is False
    assert capabilities.supports_metadata is False
    assert capabilities.supports_sandbox_policy is False


def test_batch_export_options_defaults_to_bounded_first_page():
    options = BatchExportOptions()

    assert options.since is None
    assert options.limit == 100
    assert options.cursor is None


@pytest.mark.parametrize("bad_limit", [0, -1, MAX_BATCH_EXPORT_LIMIT + 1, True])
def test_batch_export_options_rejects_invalid_limit(bad_limit):
    with pytest.raises(ValueError):
        BatchExportOptions(limit=bad_limit)


def test_batch_export_result_defaults_to_complete_empty_page():
    result = BatchExportResult()

    assert result.known_entities == []
    assert result.recent_sessions == []
    assert result.next_cursor is None
    assert result.has_more is False


def test_batch_export_result_requires_cursor_when_more_pages_exist():
    with pytest.raises(ValueError):
        BatchExportResult(has_more=True)


def test_batch_export_result_rejects_cursor_without_more_flag():
    with pytest.raises(ValueError):
        BatchExportResult(next_cursor="cursor-2")


def test_adapter_sandbox_policy_defaults_to_no_access():
    policy = AdapterSandboxPolicy()

    assert policy.filesystem_read_roots == []
    assert policy.filesystem_write_roots == []
    assert policy.network_hosts == []
    assert policy.subprocess_commands == []


# -- Visibility resolution -----------------------------------------------------


def test_apply_visibility_defaults_to_public():
    """No policy, no entity setting -> PUBLIC."""
    e = Entity(name="thing")
    assert apply_visibility(e) == Visibility.PUBLIC


def test_apply_visibility_respects_entity_level_setting():
    e = Entity(name="thing", visibility=Visibility.TEAM)
    assert apply_visibility(e) == Visibility.TEAM


def test_apply_visibility_private_tag_wins_over_entity_setting():
    """PII-leak guardrail: private_tags override even explicit entity.visibility=PUBLIC."""
    policy = VisibilityPolicy(private_tags=["personal"])
    e = Entity(name="thing", tags=["personal"], visibility=Visibility.PUBLIC)
    assert apply_visibility(e, policy) == Visibility.PRIVATE


def test_apply_visibility_team_tag_resolves_to_team():
    policy = VisibilityPolicy(team_tags=["internal"])
    e = Entity(name="thing", tags=["internal"])
    assert apply_visibility(e, policy) == Visibility.TEAM


def test_apply_visibility_policy_default_when_no_tags():
    policy = VisibilityPolicy(default=Visibility.TEAM)
    e = Entity(name="thing")
    assert apply_visibility(e, policy) == Visibility.TEAM


def test_filter_for_federation_drops_private():
    policy = VisibilityPolicy(private_tags=["personal"])
    entities = [
        Entity(name="public_thing"),
        Entity(name="personal_thing", tags=["personal"]),
        Entity(name="team_thing", visibility=Visibility.TEAM),
    ]
    filtered = filter_for_federation(entities, policy)
    names = [e.name for e in filtered]
    assert "public_thing" in names
    assert "team_thing" in names
    assert "personal_thing" not in names


def test_filter_for_federation_no_policy_keeps_all_non_private():
    """Without a policy, only entities explicitly marked private are dropped."""
    entities = [
        Entity(name="a"),
        Entity(name="b", visibility=Visibility.PRIVATE),
        Entity(name="c", visibility=Visibility.TEAM),
    ]
    filtered = filter_for_federation(entities)
    names = {e.name for e in filtered}
    assert names == {"a", "c"}


# -- L5Manifest.to_dict() ------------------------------------------------------


def test_l5_to_dict_strips_none_and_empty():
    manifest = L5Manifest(
        spec_version="0.1",
        agent=AgentInfo(id="x", type="code-assistant"),
        last_updated="2026-04-15T12:00:00+00:00",
    )
    d = manifest.to_dict()
    assert "spec_version" in d
    assert "agent" in d
    # Empty lists and None values dropped
    assert "known_entities" not in d
    assert "recent_sessions" not in d


def test_agent_info_accepts_role_narrative():
    """role_narrative is optional and defaults to None."""
    a = AgentInfo(id="x", type="code-assistant")
    assert a.role_narrative is None
    b = AgentInfo(
        id="x",
        type="code-assistant",
        role_narrative="Lead code-assistant. Executes prime code.",
    )
    assert b.role_narrative == "Lead code-assistant. Executes prime code."


def test_l5_to_dict_emits_role_narrative_when_set():
    """role_narrative survives serialization to dict and lands under agent.role_narrative."""
    manifest = L5Manifest(
        spec_version="0.1",
        agent=AgentInfo(
            id="claude-code",
            type="code-assistant",
            role_narrative="Agentic manager and code-assistant.",
        ),
        last_updated="2026-04-22T12:00:00+00:00",
    )
    d = manifest.to_dict()
    assert d["agent"]["role_narrative"] == "Agentic manager and code-assistant."


def test_l5_to_dict_omits_role_narrative_when_none():
    """role_narrative is dropped from dict when not set (per to_dict's None-stripping rule)."""
    manifest = L5Manifest(
        spec_version="0.1",
        agent=AgentInfo(id="x", type="code-assistant"),
        last_updated="2026-04-22T12:00:00+00:00",
    )
    d = manifest.to_dict()
    assert "role_narrative" not in d["agent"]


def test_entity_temporal_validity_defaults():
    """valid_from / valid_to default to None and survive round-trip."""
    e = Entity(name="X")
    assert e.valid_from is None
    assert e.valid_to is None


def test_entity_temporal_validity_round_trip():
    """Setting valid_from/valid_to is preserved on the dataclass."""
    e = Entity(name="Cyndy", valid_from="2026-03-01", valid_to="2026-04-14")
    assert e.valid_from == "2026-03-01"
    assert e.valid_to == "2026-04-14"


def test_l5_to_dict_emits_valid_to_when_set():
    """An entity with valid_to surfaces it under the entity dict."""
    manifest = L5Manifest(
        spec_version="0.1",
        agent=AgentInfo(id="x", type="code-assistant"),
        last_updated="2026-04-27T12:00:00+00:00",
        known_entities=[
            Entity(name="Cyndy", valid_from="2026-03-01", valid_to="2026-04-14")
        ],
    )
    d = manifest.to_dict()
    assert d["known_entities"][0]["valid_from"] == "2026-03-01"
    assert d["known_entities"][0]["valid_to"] == "2026-04-14"


def test_l5_to_dict_omits_validity_when_unset():
    """None values for valid_from/valid_to are dropped from the serialized dict."""
    manifest = L5Manifest(
        spec_version="0.1",
        agent=AgentInfo(id="x", type="code-assistant"),
        last_updated="2026-04-27T12:00:00+00:00",
        known_entities=[Entity(name="ActiveProject")],
    )
    d = manifest.to_dict()
    entry = d["known_entities"][0]
    assert "valid_from" not in entry
    assert "valid_to" not in entry


def test_l5_to_dict_includes_populated_entities():
    manifest = L5Manifest(
        spec_version="0.1",
        agent=AgentInfo(id="x", type="code-assistant"),
        last_updated="2026-04-15T12:00:00+00:00",
        known_entities=[
            Entity(
                name="ILTT",
                type="product",
                summary="Fitness platform",
                visibility=Visibility.PUBLIC,
            )
        ],
    )
    d = manifest.to_dict()
    assert len(d["known_entities"]) == 1
    assert d["known_entities"][0]["name"] == "ILTT"
    assert d["known_entities"][0]["visibility"] == "public"  # enum serialized to str


# -- Protocol conformance ------------------------------------------------------


class _MinimalAdapter:
    agent_id = "minimal"
    agent_type = "code-assistant"
    native_path = "/tmp/nothing"

    def discover(self) -> AgentStore:
        return AgentStore(path=self.native_path)

    def export_l5(self, since=None) -> L5Manifest:
        return L5Manifest(
            spec_version=SPEC_VERSION,
            agent=AgentInfo(id=self.agent_id, type=self.agent_type),
            last_updated="2026-04-15T12:00:00+00:00",
        )

    def export_sessions(self, since: datetime, limit: int = 100) -> list[Session]:
        return []

    def health_check(self) -> HealthStatus:
        return HealthStatus(status="ok")


class _MetadataAwareAdapter:
    def adapter_metadata(self) -> AdapterMetadata:
        return AdapterMetadata(display_name="Metadata Aware")


class _CapabilitiesAwareAdapter:
    def adapter_capabilities(self) -> AdapterCapabilities:
        return AdapterCapabilities(supports_metadata=True)


class _SandboxAwareAdapter:
    def adapter_sandbox_policy(self) -> AdapterSandboxPolicy:
        return AdapterSandboxPolicy(filesystem_read_roots=["/tmp/example"])


class _BatchAdapter(_MinimalAdapter):
    def export_l5_batch(self, options: BatchExportOptions) -> BatchExportResult:
        return BatchExportResult(
            known_entities=[Entity(name="Bourdon")],
            next_cursor="cursor-2",
            has_more=True,
        )


class _AsyncAdapter:
    agent_id = "async"
    agent_type = "code-assistant"
    native_path = "https://api.example.test"

    async def adiscover(self) -> AgentStore:
        return AgentStore(path=self.native_path)

    async def aexport_l5(self, since=None) -> L5Manifest:
        return L5Manifest(
            spec_version=SPEC_VERSION,
            agent=AgentInfo(id=self.agent_id, type=self.agent_type),
            last_updated="2026-04-15T12:00:00+00:00",
        )

    async def aexport_sessions(self, since: datetime, limit: int = 100) -> list[Session]:
        return []

    async def ahealth_check(self) -> HealthStatus:
        return HealthStatus(status="ok")


def test_minimal_adapter_satisfies_protocol():
    assert isinstance(_MinimalAdapter(), BourdonAdapter)


def test_broken_adapter_fails_protocol():
    class NotAnAdapter:
        pass

    assert not isinstance(NotAnAdapter(), BourdonAdapter)


def test_optional_metadata_provider_protocol_is_runtime_checkable():
    assert isinstance(_MetadataAwareAdapter(), AdapterMetadataProvider)
    assert not isinstance(_MinimalAdapter(), AdapterMetadataProvider)


def test_optional_capabilities_provider_protocol_is_runtime_checkable():
    assert isinstance(_CapabilitiesAwareAdapter(), AdapterCapabilitiesProvider)
    assert not isinstance(_MinimalAdapter(), AdapterCapabilitiesProvider)


def test_optional_sandbox_policy_provider_protocol_is_runtime_checkable():
    assert isinstance(_SandboxAwareAdapter(), AdapterSandboxPolicyProvider)
    assert not isinstance(_MinimalAdapter(), AdapterSandboxPolicyProvider)


def test_batch_export_adapter_protocol_extends_sync_adapter_protocol():
    adapter = _BatchAdapter()

    assert isinstance(adapter, BourdonAdapter)
    assert isinstance(adapter, BatchExportAdapter)


def test_async_adapter_protocol_uses_distinct_method_names():
    assert isinstance(_AsyncAdapter(), AsyncBourdonAdapter)
    assert not isinstance(_MinimalAdapter(), AsyncBourdonAdapter)
