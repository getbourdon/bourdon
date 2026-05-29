"""
Bourdon adapter base -- Protocol + dataclasses + exceptions.

See spec/ADAPTER_CONTRACT.md for the full adapter contract.
See spec/L5_schema.json for the normative L5 manifest schema.

Version: contract v0.2 (L5 spec v0.1)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Protocol, runtime_checkable

CONTRACT_VERSION = "0.2"
SPEC_VERSION = "0.1"


# -- Exceptions ----------------------------------------------------------------

class AdapterError(Exception):
    """Base class for adapter errors."""


class AdapterDiscoveryError(AdapterError):
    """Raised by discover() when the native store cannot be found or read."""


class AdapterExportError(AdapterError):
    """Raised by export_l5() or export_sessions() when export fails mid-operation."""


class AdapterVersionMismatchError(AdapterDiscoveryError):
    """Raised when the native store's version is outside the adapter's supported range."""


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
    instance: str | None = None
    spec_version_compat: str | None = None
    role_narrative: str | None = None


@dataclass
class Entity:
    """A single known-entity row in the L5 manifest's known_entities list."""

    name: str
    type: str | None = None
    aliases: list[str] = field(default_factory=list)
    summary: str | None = None
    last_touched: str | None = None
    tags: list[str] = field(default_factory=list)
    visibility: Visibility | None = None
    # ISO 8601 dates (YYYY-MM-DD). When valid_to is None the entity is
    # still considered active as of the manifest's last_updated time.
    # Inspired by Zep Graphiti's temporal-validity model.
    valid_from: str | None = None
    valid_to: str | None = None


@dataclass
class Session:
    """A single recent-sessions row in the L5 manifest."""

    date: str
    cwd: str | None = None
    project_focus: list[str] = field(default_factory=list)
    key_actions: list[str] = field(default_factory=list)
    files_touched: list[str] = field(default_factory=list)
    visibility: Visibility | None = None


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
    visibility_policy: VisibilityPolicy | None = None

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
    reason: str | None = None
    details: dict[str, Any] = field(default_factory=dict)
    proposed_fix: str | None = None  # Human-runnable command to remedy a non-ok status


MAX_BATCH_EXPORT_LIMIT = 1000


@dataclass
class AdapterMetadata:
    """Human-facing adapter metadata for discovery UIs and documentation."""

    display_name: str
    description: str = ""
    homepage_url: str | None = None
    docs_url: str | None = None
    icon: str | None = None
    tags: list[str] = field(default_factory=list)


@dataclass
class AdapterCapabilities:
    """Optional contract extensions an adapter explicitly supports."""

    supports_incremental: bool = False
    supports_batch_export: bool = False
    supports_async: bool = False
    supports_metadata: bool = False
    supports_sandbox_policy: bool = False


@dataclass
class BatchExportOptions:
    """Cursor-style request options for adapters that page large native stores."""

    since: datetime | None = None
    limit: int = 100
    cursor: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.limit, int) or isinstance(self.limit, bool):
            raise ValueError("Batch export limit must be an integer.")
        if self.limit < 1:
            raise ValueError("Batch export limit must be at least 1.")
        if self.limit > MAX_BATCH_EXPORT_LIMIT:
            raise ValueError(
                f"Batch export limit must be <= {MAX_BATCH_EXPORT_LIMIT}."
            )


@dataclass
class BatchExportResult:
    """One page of normalized adapter output."""

    known_entities: list[Entity] = field(default_factory=list)
    recent_sessions: list[Session] = field(default_factory=list)
    next_cursor: str | None = None
    has_more: bool = False

    def __post_init__(self) -> None:
        if self.has_more and not self.next_cursor:
            raise ValueError("Batch export results with has_more require next_cursor.")
        if self.next_cursor and not self.has_more:
            raise ValueError("Batch export next_cursor requires has_more=True.")


@dataclass
class AdapterSandboxPolicy:
    """
    Descriptive access requirements for future adapter isolation.

    This is advisory in contract v0.2 foundation work. Runtime enforcement is a
    separate layer; adapters declare the narrowest filesystem, network, and
    process surface they need so a host can enforce it later.
    """

    filesystem_read_roots: list[str] = field(default_factory=list)
    filesystem_write_roots: list[str] = field(default_factory=list)
    network_hosts: list[str] = field(default_factory=list)
    subprocess_commands: list[str] = field(default_factory=list)


# -- Protocol ------------------------------------------------------------------

@runtime_checkable
class BourdonAdapter(Protocol):
    """
    Protocol that every adapter (native publisher or external adapter) must satisfy.

    See spec/ADAPTER_CONTRACT.md for semantic requirements beyond the Protocol
    shape (visibility enforcement, idempotency, error handling).
    """

    agent_id: str
    agent_type: str
    native_path: str

    def discover(self) -> AgentStore:
        """Check that the native store exists; return metadata."""
        ...

    def export_l5(self, since: datetime | None = None) -> L5Manifest:
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


@runtime_checkable
class AdapterMetadataProvider(Protocol):
    """Optional extension for adapters that expose UI/documentation metadata."""

    def adapter_metadata(self) -> AdapterMetadata:
        """Return human-facing metadata. Must not read native user data."""
        ...


@runtime_checkable
class AdapterCapabilitiesProvider(Protocol):
    """Optional extension for adapters that declare supported contract features."""

    def adapter_capabilities(self) -> AdapterCapabilities:
        """Return optional feature support flags."""
        ...


@runtime_checkable
class AdapterSandboxPolicyProvider(Protocol):
    """Optional extension for adapters that declare isolation requirements."""

    def adapter_sandbox_policy(self) -> AdapterSandboxPolicy:
        """Return the narrowest access surface required by the adapter."""
        ...


@runtime_checkable
class BatchExportAdapter(BourdonAdapter, Protocol):
    """Optional extension for adapters that export very large stores in pages."""

    def export_l5_batch(self, options: BatchExportOptions) -> BatchExportResult:
        """Export one page of normalized L5 rows."""
        ...


@runtime_checkable
class AsyncBourdonAdapter(Protocol):
    """
    Optional async adapter surface for network-backed native stores.

    Async methods use an ``a`` prefix so runtime protocol checks do not confuse
    sync ``BourdonAdapter`` implementations with async adapters.
    """

    agent_id: str
    agent_type: str
    native_path: str

    async def adiscover(self) -> AgentStore:
        """Async equivalent of discover()."""
        ...

    async def aexport_l5(self, since: datetime | None = None) -> L5Manifest:
        """Async equivalent of export_l5()."""
        ...

    async def aexport_sessions(
        self, since: datetime, limit: int = 100
    ) -> list[Session]:
        """Async equivalent of export_sessions()."""
        ...

    async def ahealth_check(self) -> HealthStatus:
        """Async equivalent of health_check(). Must not raise."""
        ...


# -- Helpers -------------------------------------------------------------------

def apply_visibility(
    entity: Entity, policy: VisibilityPolicy | None = None
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
    entities: list[Entity], policy: VisibilityPolicy | None = None
) -> list[Entity]:
    """Return only entities whose resolved visibility is not PRIVATE."""
    return [e for e in entities if apply_visibility(e, policy) != Visibility.PRIVATE]
