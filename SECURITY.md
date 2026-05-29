# Security Model — Bourdon Adapters

This document describes the runtime security properties of Bourdon's adapter layer, with specific details for the Cascade (Windsurf) adapter. The same model applies to all convention-based adapters (Copilot, Cascade) and to external adapters (Claude Code, Codex, Cursor) that read native agent state.

## What an adapter reads at runtime

| Adapter | Reads |
|---------|-------|
| **Cascade** | `~/.cascade-bourdon/memory.md` (user-maintained YAML front-matter) |
| **Copilot** | `~/.copilot-bourdon/memory.md` (same convention pattern) |
| **Claude Code** | `~/.claude-brain/`, auto-memory, MCP knowledge graph |
| **Codex** | `~/.codex/` (session_index.jsonl, rollouts, state_5.sqlite) |
| **Cursor** | Cursor's SQLite state databases (read-only temp copy) |

No adapter reads outside its declared scope. Convention-based adapters (Cascade, Copilot) read a single file in a single directory under `$HOME`.

## What an adapter writes at runtime

All adapters write a single file: `~/agent-library/agents/<agent-id>.l5.yaml`.

Writes use **atomic tmp + fsync + rename** via `core/l5_io.py`. This prevents concurrent readers (the L6 federation server, other adapters) from observing a half-written manifest. No adapter bypasses this path.

## Credential redaction

Every string that originates from native agent state is run through the canonical redaction pipeline before landing in an L5 manifest field.

**Canonical pattern set** (from `adapters/codex.py::_NATIVE_MEMORY_SENSITIVE_PATTERNS`):

| Pattern | Catches |
|---------|---------|
| `api[_-]?key` | Generic API keys |
| `api[_-]?token` | Generic API tokens |
| `access[_-]?token` | OAuth access tokens |
| `bearer\s+token` | Bearer auth tokens |
| `password` | Password references |
| `sk_live_*` | Stripe live keys |
| `hf_*` (10+ chars) | HuggingFace tokens |

**Cascade-specific extensions** (via `_CASCADE_SENSITIVE_PATTERNS`):

| Pattern | Catches |
|---------|---------|
| `secret` | Generic secret references |
| `sk_test_*` | Stripe test keys |

Additional scrubbing applied by `_safe_native_memory_text`:

- **URL stripping**: `https?://...` → `[link]`
- **Length cap**: 180 characters, truncated with `...`
- **Uniform placeholder**: `[redacted credential-like text]`

New adapters MUST import and extend the canonical pattern set rather than forking it. See `docs/AUTHORING_AN_ADAPTER.md` Step 2.

## Visibility model

Entities are tagged with visibility metadata. Before federation:

1. **Private-tag guardrail**: Entities with tags matching the policy's `private_tags` list (e.g., `personal`, `credential`, `financial`, `secret`, `private`) are assigned `PRIVATE` visibility and **filtered out** before the manifest is written. This happens inside the adapter via `filter_for_federation()`.
2. **L6 trusts the adapter**: The L6 federation server does not re-filter. If an adapter emits a private entity, it leaks. Every adapter's test suite includes a visibility-filtering test with private-tagged fixtures.
3. **Access-level filtering**: CLI export commands accept `--access-level` (public/team/private) and apply `filter_manifest_for_access()` before writing.

## Defense-in-depth properties

- **No implicit network calls.** No adapter makes outbound HTTP requests. The L6 MCP server's HTTP transport is opt-in.
- **No filesystem access outside declared scope.** Reads: agent-specific directory under `$HOME`. Writes: `$HOME/agent-library/agents/`.
- **Atomic writes prevent half-written manifests.** `core/l5_io.py::write_l5` uses tmp-file + atomic rename.
- **health_check() never raises.** Required by the adapter contract. L6 calls it in a polling loop; a raised exception would crash federation for all agents.
- **Idempotent exports.** Same native-store state produces byte-identical manifests. L6 detects changes via hash comparison.

## Reporting

If you find a security issue, please email **licensing@bourdon.ai** (RADLAB LLC) or open a private security advisory on the [Bourdon repository](https://github.com/getbourdon/bourdon/security/advisories). Do not file public issues for security reports.
