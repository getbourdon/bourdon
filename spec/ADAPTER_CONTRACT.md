# Bourdon Adapter Contract v0.2

An **adapter** is the bridge between an agent's native memory store and Bourdon's standardized L5 manifest. Adapters are how Bourdon federates across agents it does not control.

Contract v0.2 is additive. The synchronous `BourdonAdapter` protocol remains
the required base surface; the new v0.2 types are optional extensions for large
stores, network-backed stores, UI metadata, and future sandbox enforcement.

## Two Kinds of Adapters

**Native publisher** — the agent itself writes its L5 directly. Used when we control the agent (Clyde, Clair, any agent built on OpenAI Agents SDK with Bourdon as a dependency). L5 is written at session close from the agent's internal state.

**External adapter** — code that reads an agent's native memory store (files, SQLite, JSONL, proprietary APIs) and normalizes it into L5. Used when we do not control the agent. Examples in Bourdon v1: Claude Code, Codex. Community adapters fill the rest (Cursor, Copilot, Obsidian, etc.).

Both kinds implement the same interface.

## The Protocol

```python
from typing import Protocol
from datetime import datetime

class BourdonAdapter(Protocol):
    """Every adapter (native or external) implements this."""

    agent_id: str          # unique slug, matches L5 schema agent.id
    agent_type: str        # one of the L5 agent.type enum values
    native_path: str       # filesystem path or URI of native store

    def discover(self) -> AgentStore:
        """
        Check the native store exists + return metadata.
        Raises AdapterDiscoveryError if not found.
        """
        ...

    def export_l5(self, since: datetime | None = None) -> L5Manifest:
        """
        Build L5 manifest from native memory.
        Applies visibility_policy filter before returning.
        """
        ...

    def export_sessions(self, since: datetime, limit: int = 100) -> list[Session]:
        """
        Export recent sessions in normalized schema.
        Called by export_l5; exposed separately for incremental updates.
        """
        ...

    def health_check(self) -> HealthStatus:
        """
        Return ok | degraded | blocked with reason.
        Used by `bourdon doctor` CLI.
        """
        ...
```

## Registration

Adapters register via Python entry points in `pyproject.toml`:

```toml
[project.entry-points."bourdon.adapters"]
my-agent = "my_package.adapter:MyAgentAdapter"
```

Bourdon's CLI and L6 server discover adapters by iterating the `bourdon.adapters` entry point group. No central registry needed.

## Data Contract

See `L5_schema.json` for the normative schema. Adapters MUST produce manifests that validate against it. The CI pipeline validates every emitted manifest before L6 accepts it.

## Error Semantics

- **`AdapterDiscoveryError`** — the native store does not exist or cannot be read. Raised from `discover()`. Non-fatal; L6 skips this adapter and logs.
- **`AdapterExportError`** — the native store exists but something went wrong during export. Raised from `export_l5()` or `export_sessions()`. Non-fatal but surfaced in `bourdon doctor`.
- **`AdapterVersionMismatchError`** — the native store is a newer/older format than this adapter supports. Specific case of discovery error. Surfaces upgrade guidance.
- **Everything else** must be caught inside the adapter and converted to a `HealthStatus.degraded` with a reason — adapters never propagate unknown exceptions to the L6 server.

## Visibility Enforcement Is the Adapter's Job

The adapter MUST apply `visibility_policy` filtering before emitting the L5 manifest. L6 trusts the manifest it receives; there is no second filter layer. If an adapter emits a `private`-tagged entity, it leaks. Test this.

Recommended pattern:

```python
def export_l5(self, since=None) -> L5Manifest:
    raw_entities = self._read_native_entities()
    policy = self._load_visibility_policy()

    visible_entities = [
        e for e in raw_entities
        if self._apply_visibility(e, policy) != "private"
    ]

    return L5Manifest(
        spec_version="0.1",
        agent=self._agent_info(),
        last_updated=datetime.utcnow(),
        known_entities=visible_entities,
        ...
    )
```

## Idempotency

`export_l5()` MUST be deterministic for a given native store state. Same store state = same manifest. This lets L6 detect "has anything changed" via a simple hash comparison.

## Incremental Export (Optional)

The `since` parameter on `export_l5()` and `export_sessions()` allows efficient updates. Adapters that support it emit only entities/sessions updated after `since`. Adapters that do not support it emit everything (and the caller deduplicates). Declare support with `AdapterCapabilities(supports_incremental=True)`.

## Optional Extension Surfaces (v0.2)

These extensions live in `adapters/base.py`. They are deliberately optional so
existing adapters do not need churn.

### Metadata

Adapters that want to expose UI or documentation details implement
`AdapterMetadataProvider`:

```python
class MyToolAdapter:
    def adapter_metadata(self) -> AdapterMetadata:
        return AdapterMetadata(
            display_name="My Tool",
            description="Exports My Tool project and session memory.",
            docs_url="https://example.test/my-tool/bourdon",
            tags=["network-backed"],
        )
```

Metadata MUST be static or derived from adapter code/config. It MUST NOT read
native user memory; discovery UIs should be able to call it safely.

### Capabilities

Adapters declare optional support through `AdapterCapabilitiesProvider`:

```python
class MyToolAdapter:
    def adapter_capabilities(self) -> AdapterCapabilities:
        return AdapterCapabilities(
            supports_incremental=True,
            supports_batch_export=True,
            supports_async=True,
            supports_metadata=True,
            supports_sandbox_policy=True,
        )
```

Default capabilities are conservative: every optional flag is `False`.

### Batch Export

Very large native stores SHOULD implement `BatchExportAdapter` instead of
forcing callers to materialize all entities and sessions in one process:

```python
result = adapter.export_l5_batch(BatchExportOptions(limit=500, cursor=cursor))
for entity in result.known_entities:
    ...
cursor = result.next_cursor if result.has_more else None
```

`BatchExportOptions.limit` is bounded to `1..1000`. A batch result with
`has_more=True` MUST include `next_cursor`; a result with `next_cursor` MUST set
`has_more=True`. Cursor tokens are adapter-owned opaque strings.

Batch export returns normalized `Entity` and `Session` rows, not raw native
records. Visibility filtering and redaction requirements still apply before a
row leaves the adapter.

### Sandbox Policy

Adapters that can describe their access surface implement
`AdapterSandboxPolicyProvider`:

```python
class MyToolAdapter:
    def adapter_sandbox_policy(self) -> AdapterSandboxPolicy:
        return AdapterSandboxPolicy(
            filesystem_read_roots=["~/.my-tool"],
            filesystem_write_roots=[],
            network_hosts=["api.my-tool.example"],
            subprocess_commands=[],
        )
```

This is advisory in v0.2. Runtime enforcement is future work. The declaration
still matters: adapters should publish the narrowest filesystem roots, network
hosts, and subprocess commands they need so a future host can enforce them.
Empty lists mean no declared access.

### Async Network-Backed Adapters

Network-backed native stores such as Linear, Attio, Notion, or MCP-backed
systems SHOULD implement `AsyncBourdonAdapter`:

```python
class MyNetworkAdapter:
    agent_id = "my-network-tool"
    agent_type = "code-assistant"
    native_path = "https://api.example.test"

    async def adiscover(self) -> AgentStore: ...
    async def aexport_l5(self, since=None) -> L5Manifest: ...
    async def aexport_sessions(self, since, limit=100) -> list[Session]: ...
    async def ahealth_check(self) -> HealthStatus: ...
```

Async methods use an `a` prefix on purpose. Runtime protocol checks cannot
distinguish a sync `def export_l5` from an async `async def export_l5`; distinct
method names keep sync adapters from accidentally satisfying the async surface.

The same error semantics apply: async `ahealth_check()` must not raise, and
network calls should use bounded timeouts, retry only idempotent operations, and
avoid logging tokens or raw responses.

## Example: A Minimal Adapter

```python
from datetime import datetime
from pathlib import Path
from adapters.base import BourdonAdapter, L5Manifest, AgentInfo, Entity

class MyToolAdapter:
    agent_id = "my-tool"
    agent_type = "code-assistant"
    native_path = str(Path.home() / ".my-tool")

    def discover(self):
        if not Path(self.native_path).exists():
            raise AdapterDiscoveryError(f"My Tool not installed at {self.native_path}")
        return AgentStore(path=self.native_path, version="unknown")

    def export_l5(self, since=None):
        return L5Manifest(
            spec_version="0.1",
            agent=AgentInfo(id=self.agent_id, type=self.agent_type),
            last_updated=datetime.utcnow().isoformat() + "Z",
            known_entities=[],  # TODO: actual parsing in v0.1.0
        )

    def export_sessions(self, since, limit=100):
        return []

    def health_check(self):
        try:
            self.discover()
            return HealthStatus(status="ok")
        except AdapterDiscoveryError as e:
            return HealthStatus(status="blocked", reason=str(e))
```

## Testing an Adapter

Every adapter MUST ship:
- **Discovery test** — verifies `discover()` raises on missing store, returns on present store
- **Schema conformance test** — runs `export_l5()` against a fixture store, validates output against `L5_schema.json` using `jsonschema` library
- **Visibility test** — constructs a fixture with `private`-tagged entities, verifies they are absent from emitted L5
- **Round-trip test (if applicable)** — for adapters where the native format supports it: parse → emit → re-parse → assert equivalence

Fixtures live under `tests/fixtures/<agent_id>/` with sample native-store content.

## Versioning

- Adapter contract has its own semver, tied to Bourdon spec version but independently bumped on breaking changes
- Adapters declare `CONTRACT_VERSION = "0.2"` at module level
- L6 warns on version mismatch but does not reject — adapters are free to be ahead or behind

## Open Questions

- Runtime sandbox enforcement for declared `AdapterSandboxPolicy` values
- Async batch export for network-backed stores with very large histories
- UI display conventions for adapter icons and tags
- Backpressure and resumability guarantees for million-row adapter exports

## Reference Implementation

See `adapters/claude_code.py` for the first external adapter. See `adapters/base.py` for the Protocol + dataclass definitions.
