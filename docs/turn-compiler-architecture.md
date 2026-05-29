# Turn Compiler Architecture — the shared core and the `SessionSource` seam

Status: implemented in `core/turn_compiler.py`.

## Why this exists

The Codex turn compiler (PR #97, `codex-turn-brief/v1`) proved the
recognition-orchestration thesis on one agent. Issue #101 ports it to Claude
Code. Rather than fold Claude into the Codex module or duplicate ~1,200 lines,
the second implementation motivated extracting a single agent-agnostic engine
with a thin per-agent seam — exactly the `SessionSource` abstraction the issue
asked us to revisit "now that there are two callers."

## Layers

```
core/turn_compiler.py        agent-agnostic engine + SessionSource Protocol
  ├─ core/codex_turn_compiler.py    CodexSessionSource  + compile_codex_turn()
  └─ core/claude_turn_compiler.py   ClaudeSessionSource + compile_claude_turn()
```

`compile_turn(prompt, *, source: SessionSource, ...)` owns everything that does
not differ between agents:

- prompt/access/budget validation,
- cwd + git repo identity (walks to `.git`, reads `.git/config`),
- L6 federation manifest → candidates,
- the deterministic scorer, recognition gate, and ranker,
- the router, the explicit/MCP/memory/fallback/overlay delivery payloads,
- the recognition trace and diagnostics,
- credential redaction + length bounding (`core/text_safety.py`).

Each public entry point (`compile_codex_turn`, `compile_claude_turn`) is a thin
wrapper that injects its agent's `SessionSource` and keeps its original
signature.

## The `SessionSource` seam

A `typing.Protocol` (runtime-checkable). It carries the small set of things that
genuinely differ between agents — identity labels and read-only native-surface
probes:

| member | Codex | Claude Code |
|---|---|---|
| `agent_id` | `codex` | `claude-code` |
| `agent_display` | `Codex` | `Claude` |
| `schema_version` | `codex-turn-brief/v1` | `claude-turn-brief/v1` |
| `l5_source_label` | `codex_l5` | `claude_l5` |
| `native_health_key` | `native_stage1` | `native_memory` |
| `native_health_noun` | `native Stage 1` | `native memory` |
| `local_record_noun` | `Codex thread` | `Claude session` |
| `exhausted_paths` | Codex list | Claude list |
| `resolve_home(override)` | `~/.codex` | `~/.claude/projects` base |
| `inspect_native(home)` | `state_5.sqlite` stage1 report | `MEMORY.md` size report |
| `classify_native(report)` | available/degraded/unknown | available/degraded/unknown |
| `collect_local_records(home, *, limit)` | `threads` table → `kind="thread"` | project `*.jsonl` → `kind="thread"` |
| `native_diagnostics(report)` | `{stage1_jobs: ...}` | `{auto_memory: ...}` |

The engine reads `source.agent_id` to decide own-agent vs cross-agent
(`l6_federation`) source labels, derives the suppressed-route name as
`f"{native_health_key}_primary"`, and threads the display nouns into rendered
prose. Health is stored generically on `BriefHealth(value, key)` and serialized
under the agent's key; a `native_stage1` property preserves the original Codex
attribute access.

## Backwards compatibility

- `compile_codex_turn` keeps its signature and **byte-for-byte output**; the
  existing `tests/test_codex_turn_compiler.py` suite is the regression guard and
  stays green unchanged.
- `core/text_safety.py` is the new canonical home for
  `_NATIVE_MEMORY_SENSITIVE_PATTERNS` / `_normalize_text` /
  `_safe_native_memory_text`. `adapters/codex.py` re-exports them, so existing
  importers (`adapters/copilot.py`, `adapters/cascade.py`, `cli/main.py`) are
  unchanged.

## Adding a third agent

Implement a `SessionSource` (identity labels + the five read-only probes), add a
thin `compile_<agent>_turn` wrapper, and wire a CLI subcommand + MCP tool that
mirror the Codex/Claude pairs. No engine changes should be required; if they
are, that is the signal that the seam needs another parameter rather than a fork.
