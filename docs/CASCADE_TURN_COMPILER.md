# Cascade Turn Compiler — `cascade-turn-brief/v1`

Skill/reference document for the Cascade turn-scoped recognition compiler. Mirrors
`core/codex_turn_compiler.py` architecture but adapted for Cascade's data surfaces.

## Contract

- **Read-only**: never mutates convention files, native Windsurf state, or federation library
- **Deterministic**: same inputs → same output (no model calls, no network)
- **Bounded**: prompt capped at 8K chars, output capped at configurable max_chars (default 1800)
- **Safe**: credential redaction via `_CASCADE_SENSITIVE_PATTERNS`, access-level filtering
- **Fast**: must complete in <100ms on typical developer machine

## Schema: `cascade-turn-brief/v1`

```yaml
schema_version: cascade-turn-brief/v1
prompt: "<bounded prompt text>"
cwd: "/path/to/workspace"
repo:
  name: bourdon
  root: /Users/radman/bourdon
  remote: https://github.com/getbourdon/bourdon
health:
  convention_file: available | degraded | missing
  native_state: available | degraded | missing
  strategy: turn_compiled
routing:
  primary_surface: explicit | mcp | convention-file | fallback
  confidence: high | medium | low
  reason: "<one-line explanation>"
items:
  - rank: 1
    score: 8.5
    kind: project | topic | person | session | plan | workflow
    name: "Entity Name"
    summary: "Brief summary"
    reason: "Why this matched"
    source: convention_file | native_windsurf | l6_federation | workspace_context
    source_agents: [cascade, claude-code]
    evidence: ["token overlap: bourdon", "cwd match"]
delivery:
  explicit_text: "<bounded recognition brief for pre-turn injection>"
  mcp_payload: { ... }
  convention_file_block: "<idempotent block for ~/.cascade-bourdon/memory.md>"
trace:
  candidates_gathered: 42
  candidates_scored: 42
  candidates_above_threshold: 6
  scoring_method: token_overlap_recency_affinity
diagnostics:
  convention_file_entities: 12
  native_state_sessions: 5
  federation_entities: 38
  workspace_plans: 2
  workspace_workflows: 3
```

## Candidate Sources (gathered in order)

1. **Convention file** (`~/.cascade-bourdon/memory.md`) — entities and sessions from YAML front-matter
2. **Native Windsurf state** (`state.vscdb`) — chat session metadata, workspace associations
3. **L6 Federation library** (`~/agent-library/agents/*.l5.yaml`) — cross-agent entities/sessions
4. **Workspace context** (`.windsurf/plans/*.md`, `.windsurf/workflows/*.md`) — active plans/workflows as entity candidates

## Scoring Heuristics

Each candidate gets a 0–10 score computed from:

| Signal | Weight | Notes |
|--------|--------|-------|
| Prompt-token overlap | 0.4 | Case-insensitive word-boundary match against entity name + aliases + summary |
| CWD/repo affinity | 0.25 | Does the candidate's cwd/project_focus match the current repo? |
| Recency | 0.2 | Exponential decay from last_touched / session date |
| Source confidence | 0.15 | convention_file=1.0, native_windsurf=0.9, l6_federation=0.8, workspace_context=0.7 |

Threshold for inclusion: score >= 2.0 (same as Codex compiler).

## Delivery Surfaces

| Surface | When used |
|---------|-----------|
| `explicit_text` | Primary — bounded pre-turn recognition text injected before the prompt |
| `mcp_payload` | When Cascade consumes Bourdon via MCP `compile_cascade_turn` tool |
| `convention_file_block` | Compatibility — idempotent block that can be merged into memory.md |

## Differences from Codex Compiler

| Aspect | Codex | Cascade |
|--------|-------|---------|
| Native DB | `state_5.sqlite` (threads table) | `state.vscdb` (ItemTable key-value) |
| Rollout chronology | Yes (rich) | No (not exposed) |
| Workspace enrichment | No | Yes (`.windsurf/plans/`, `.windsurf/workflows/`) |
| Native Stage 1 health | SQLite memory jobs | N/A — Cascade has no equivalent |
| Health signal | `native_stage1: available\|degraded\|unknown` | `convention_file` + `native_state` pair |
| Session source | threads + rollout dirs | chat session index + convention file |

## CLI Surfaces

```bash
bourdon cascade compile-turn "<prompt>"
bourdon cascade prepare-turn --strategy turn-compiled "<prompt>"
bourdon cascade eval --turn-compiler
```

## MCP Surface

The L6 server exposes `compile_cascade_turn(prompt, cwd, access_level, max_items, max_chars, delivery)` returning the same `cascade-turn-brief/v1` schema.
