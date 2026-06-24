# Cascade (Windsurf) Turn-Scoped Recognition Compiler

Status: prototype architecture, implemented in `core/cascade_turn_compiler.py`.

## Purpose

The Cascade turn compiler is the Windsurf-side member of the shared turn-compiler
family (see [`codex-turn-compiler.md`](codex-turn-compiler.md) and
[`claude-turn-compiler.md`](claude-turn-compiler.md)). It reconciles Cascade's
earlier standalone `cascade-turn-brief/v1` compiler onto the agent-agnostic
engine in `core/turn_compiler.py` — the **third caller** after Codex and Claude,
which is what motivates the shared `SessionSource` abstraction. See
[`turn-compiler-architecture.md`](turn-compiler-architecture.md).

All three compilers now share one scorer, recognition gate, router, and brief
shape; only the per-agent `SessionSource` differs.

## Native Surfaces

The Cascade-specific reads (all read-only; auth/credential keys in the state DB
are skipped):

- **Native state health + local records** come from Windsurf's on-disk state via
  `adapters/_windsurf_native.read_native_windsurf_state`:
  - Cascade **editor sessions** (from the global `state.vscdb`) → `kind="thread"`
    (gated like Codex threads / Claude transcripts: surface on a direct prompt
    match, not vague continuations).
  - Active **`.windsurf/plans`** → `kind="plan"`, and **`.windsurf/workflows`** →
    `kind="workflow"` (workspace-relative; can surface on a vague prompt when
    they name the current repo).
- Cascade's **convention-file memory** (`~/.cascade-bourdon/memory.md`) flows in
  via the L6 federation library once exported (`bourdon cascade export`), the
  same way Codex/Claude L5 does — so it is not re-read by the compiler.

Native state health classifies as `available` (any Windsurf state found),
`degraded` (state read but with errors), or `unknown` (nothing found).

## Scoring Model

Identical to the Codex/Claude compilers — the scorer lives in the shared engine.
Cascade's former weight-based scorer (`_W_TOKEN_OVERLAP`, `_SCORE_THRESHOLD`,
`_SOURCE_CONFIDENCE`, …) is dropped in favor of the shared additive scorer
(prompt match, cwd/repo identity, recency, cross-agent agreement, continuity,
penalties), so a given entity ranks the same regardless of which agent compiles
the turn.

## Output Schema

The CLI and MCP surfaces return `cascade-turn-brief/v1` — structurally identical
to the Codex/Claude briefs, differing only in agent-specific labels:

- `schema_version: cascade-turn-brief/v1`
- `health.native_state` (vs Codex `native_stage1` / Claude `native_memory`)
- local-record sources `windsurf_native` (editor sessions) and
  `windsurf_workspace` (plans/workflows); own-agent L5 is `cascade_l5`
- the `native_state` diagnostics block (Windsurf state summary)

Note the reconciliation drops the standalone compiler's two-part health
(`convention_file` + `native_state`) for the shared single-health field; the
convention-file status is represented by Cascade's L5 presence in the federation
manifest, and the richer Windsurf summary lives under `diagnostics.native_state`.
The bespoke `convention_file_block` delivery is served by the shared
`memory_md_block` / `fallback_block`.

## Delivery Surfaces

`bourdon cascade compile-turn "<prompt>"` is read-only and defaults to all
delivery renderings (`explicit_text`, `mcp_payload`, `memory_md_block`,
`fallback_block`, `repo_overlay_block`). The MCP server exposes the same compiler
through `compile_cascade_turn(...)`. The Codex and Claude compilers are
unchanged.

## Safety And Verification

The compiler is read-only. It does not read auth tokens (the Windsurf state
reader skips `secret://` keys), write native files, mutate the federation
library, run model calls, or execute shell commands. It reuses the shared
visibility filtering and credential redaction.

Tests (`tests/test_cascade_turn_compiler.py`) mirror the Codex/Claude suites —
ranking, cwd/repo identity, cross-agent context without native state, visibility
filtering, redaction, output caps, schema identity, routing, repo overlay — plus
Cascade-native coverage: `.windsurf/plans` and `.windsurf/workflows` surfacing,
and a Cascade editor session parsed from a Windsurf `state.vscdb` fixture.

## Out of scope

Cascade's broader v0.8 parity branch also adds `cascade sync-native` /
native-memory writes (the `prepare-turn` analogue) and the `cli/setup.py`
wiring. Those are a separate, write-path concern; this reconciliation covers the
read-only `compile-turn` surface over the shared engine.
