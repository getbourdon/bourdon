# Security Model — Bourdon Participants

This document describes the runtime security properties of Bourdon's participant layer, with specific details for the Cascade (Windsurf) participant. The same model applies to all convention-based participants (Copilot, Cascade) and to external participants (Claude Code, Codex, Cursor) that read native agent state.

## What a participant reads at runtime

| Participant | Reads |
|---------|-------|
| **Cascade** | `~/.cascade-bourdon/memory.md` (user-maintained YAML front-matter) |
| **Copilot** | `~/.copilot-bourdon/memory.md` (same convention pattern) |
| **Claude Code** | `~/.claude-brain/`, auto-memory, MCP knowledge graph |
| **Codex** | `~/.codex/` (session_index.jsonl, rollouts, state_5.sqlite) |
| **Cursor** | Cursor's SQLite state databases (read-only temp copy) |

No participant reads outside its declared scope. Convention-based participants (Cascade, Copilot) read a single file in a single directory under `$HOME`.

## What a participant writes at runtime

All participants write a single file: `~/agent-library/agents/<agent-id>.l5.yaml`.

Writes use **atomic tmp + fsync + rename** via `core/l5_io.py`. This prevents concurrent readers (the L6 federation server, other participants) from observing a half-written manifest. No participant bypasses this path.

## Credential redaction

Every string that originates from native agent state is run through the canonical redaction pipeline before landing in an L5 manifest field.

**Canonical pattern set** (from `participants/codex.py::_NATIVE_MEMORY_SENSITIVE_PATTERNS`):

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

New participants MUST import and extend the canonical pattern set rather than forking it. See `docs/AUTHORING_A_PARTICIPANT.md` Step 2.

## Visibility model

Entities are tagged with visibility metadata. Before federation:

1. **Private-tag guardrail**: Entities with tags matching the policy's `private_tags` list (e.g., `personal`, `credential`, `financial`, `secret`, `private`) are assigned `PRIVATE` visibility and **filtered out** before the manifest is written. This happens inside the participant via `filter_for_federation()`.
2. **L6 trusts the participant**: The L6 federation server does not re-filter. If a participant emits a private entity, it leaks. Every participant's test suite includes a visibility-filtering test with private-tagged fixtures.
3. **Access-level filtering**: CLI export commands accept `--access-level` (public/team/private) and apply `filter_manifest_for_access()` before writing.

## Defense-in-depth properties

- **No implicit network calls.** No participant makes outbound HTTP requests. The L6 MCP server's HTTP transport is opt-in.
- **No filesystem access outside declared scope.** Reads: agent-specific directory under `$HOME`. Writes: `$HOME/agent-library/agents/`.
- **Atomic writes prevent half-written manifests.** `core/l5_io.py::write_l5` uses tmp-file + atomic rename.
- **health_check() never raises.** Required by the participant contract. L6 calls it in a polling loop; a raised exception would crash federation for all agents.
- **Idempotent exports.** Same native-store state produces byte-identical manifests. L6 detects changes via hash comparison.

## Reporting

If you find a security issue, please email **licensing@bourdon.ai** (RADLAB LLC) or open a private security advisory on the [Bourdon repository](https://github.com/getbourdon/bourdon/security/advisories). Do not file public issues for security reports.
