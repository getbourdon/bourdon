"""
Bourdon participant base -- Protocol + dataclasses + exceptions.

See spec/PARTICIPANT_CONTRACT.md for the full participant contract.
See spec/L5_schema.json for the normative L5 manifest schema.

Version: contract v0.1 (tied to Bourdon spec v0.1)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Optional, Protocol, runtime_checkable

CONTRACT_VERSION = "0.1"
SPEC_VERSION = "0.1"


# -- Exceptions ----------------------------------------------------------------

class ParticipantError(Exception):
    """Base class for participant errors."""


class ParticipantDiscoveryError(ParticipantError):
    """Raised by discover() when the native store cannot be found or read."""


class ParticipantExportError(ParticipantError):
    """Raised by export_l5() or export_sessions() when export fails mid-operation."""


class ParticipantVersionMismatchError(ParticipantDiscoveryError):
    """Raised when the native store's version is outside the participant's supported range."""


# -- Enums ---------------------------------------------------------------------

class Visibility(str, Enum):
    """Where an entity is allowed to appear in federated stores."""

    PUBLIC = "public"      # all L6 stores
    TEAM = "team"          # team L6 only
    PRIVATE = "private"    # local L6 only, never federated


# -- Dataclasses ---------------------------------------------------------------

@dataclass
class AgentInfo:
    """The `agent` block of an L5 manifest."""

    id: str
    type: str
    instance: Optional[str] = None
    spec_version_compat: Optional[str] = None
    role_narrative: Optional[str] = None


@dataclass
class Entity:
    """A single known-entity row in the L5 manifest's known_entities list."""

    name: str
    type: Optional[str] = None
    aliases: list[str] = field(default_factory=list)
    summary: Optional[str] = None
    last_touched: Optional[str] = None
    tags: list[str] = field(default_factory=list)
    visibility: Optional[Visibility] = None
    # ISO 8601 dates (YYYY-MM-DD). When valid_to is None the entity is
    # still considered active as of the manifest's last_updated time.
    # Inspired by Zep Graphiti's temporal-validity model.
    valid_from: Optional[str] = None
    valid_to: Optional[str] = None


@dataclass
class Session:
    """A single recent-sessions row in the L5 manifest."""

    date: str
    cwd: Optional[str] = None
    project_focus: list[str] = field(default_factory=list)
    key_actions: list[str] = field(default_factory=list)
    files_touched: list[str] = field(default_factory=list)
    visibility: Optional[Visibility] = None


@dataclass
class VisibilityPolicy:
    """Default visibility rules applied when an entity does not declare its own."""

    default: Visibility = Visibility.PUBLIC
    private_tags: list[str] = field(default_factory=list)
    team_tags: list[str] = field(default_factory=list)


@dataclass
class L5Manifest:
    """Complete L5 manifest. Validates against spec/L5_schema.json."""

    spec_version: str
    agent: AgentInfo
    last_updated: str  # ISO 8601 UTC
    capabilities: list[str] = field(default_factory=list)
    recent_sessions: list[Session] = field(default_factory=list)
    known_entities: list[Entity] = field(default_factory=list)
    visibility_policy: Optional[VisibilityPolicy] = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to a JSON-Schema-compatible dict (for serialization + validation)."""

        def _dict_from(obj: Any) -> Any:
            if obj is None:
                return None
            if isinstance(obj, Visibility):
                return obj.value
            if isinstance(obj, list):
                return [_dict_from(i) for i in obj]
            if hasattr(obj, "__dataclass_fields__"):
                # Skip None + empty-list fields for cleaner output
                out: dict[str, Any] = {}
                for k, v in obj.__dict__.items():
                    if v is None:
                        continue
                    if isinstance(v, list) and not v:
                        continue
                    out[k] = _dict_from(v)
                return out
            return obj

        return _dict_from(self)


@dataclass
class AgentStore:
    """Metadata describing the native agent store. Returned by discover()."""

    path: str
    version: str = "unknown"
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class HealthStatus:
    """Returned by health_check(). Used by `bourdon doctor` CLI."""

    status: str  # "ok" | "degraded" | "blocked"
    reason: Optional[str] = None
    details: dict[str, Any] = field(default_factory=dict)
    proposed_fix: Optional[str] = None  # Human-runnable command to remedy a non-ok status


# -- Protocol ------------------------------------------------------------------

@runtime_checkable
class BourdonParticipant(Protocol):
    """
    Protocol that every participant (native publisher or external participant) must satisfy.

    See spec/PARTICIPANT_CONTRACT.md for semantic requirements beyond the Protocol
    shape (visibility enforcement, idempotency, error handling).
    """

    agent_id: str
    agent_type: str
    native_path: str

    def discover(self) -> AgentStore:
        """Check that the native store exists; return metadata. Raises ParticipantDiscoveryError if missing."""
        ...

    def export_l5(self, since: Optional[datetime] = None) -> L5Manifest:
        """Build L5 manifest from native memory. Applies visibility filter before return."""
        ...

    def export_sessions(
        self, since: datetime, limit: int = 100
    ) -> list[Session]:
        """Export recent sessions in normalized schema."""
        ...

    def health_check(self) -> HealthStatus:
        """Return ok / degraded / blocked with reason. Must not raise."""
        ...


# -- Helpers -------------------------------------------------------------------

def apply_visibility(
    entity: Entity, policy: Optional[VisibilityPolicy] = None
) -> Visibility:
    """
    Resolve an entity's effective visibility, applying policy tag rules.

    Precedence (highest first):
        1. private_tags match  -> PRIVATE (cannot be overridden)
        2. entity.visibility set explicitly
        3. team_tags match     -> TEAM
        4. policy.default (or PUBLIC if no policy)
    """
    policy = policy or VisibilityPolicy()
    tag_set = set(entity.tags or [])

    # Private tags win unconditionally -- this is the PII-leak guardrail
    if tag_set & set(policy.private_tags):
        return Visibility.PRIVATE

    # Explicit entity-level setting
    if entity.visibility is not None:
        return entity.visibility

    # Team tags
    if tag_set & set(policy.team_tags):
        return Visibility.TEAM

    return policy.default or Visibility.PUBLIC


def filter_for_federation(
    entities: list[Entity], policy: Optional[VisibilityPolicy] = None
) -> list[Entity]:
    """Return only entities whose resolved visibility is not PRIVATE."""
    return [e for e in entities if apply_visibility(e, policy) != Visibility.PRIVATE]
