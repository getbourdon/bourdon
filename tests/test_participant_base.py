"""Tests for participants.base -- Protocol, dataclasses, visibility resolution."""

from __future__ import annotations

from datetime import datetime

from participants.base import (
    SPEC_VERSION,
    AgentInfo,
    AgentStore,
    BourdonParticipant,
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


class _MinimalParticipant:
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


def test_minimal_participant_satisfies_protocol():
    assert isinstance(_MinimalParticipant(), BourdonParticipant)


def test_broken_participant_fails_protocol():
    class NotAnParticipant:
        pass

    assert not isinstance(NotAnParticipant(), BourdonParticipant)
