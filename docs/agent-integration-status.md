# Agent Integration Status

This page tracks the operational Bourdon layer available for each agent. It is
the current implementation map, not the long-term adapter wishlist.

## Claude Code

Status: export hook available.

- `bourdon claude-code export` writes `~/agent-library/agents/claude-code.l5.yaml`.
- Intended hook: Claude Code `SessionEnd`.
- Current role: manager/reviewer memory source for L6 federation.

## Codex

Status: fallback and turn preparation available; native distilled memory currently blocked upstream.

- `bourdon codex doctor` diagnoses `~/.codex/state_5.sqlite` and fallback recall.
- `bourdon codex prepare-turn` refreshes Codex fallback memory surfaces and emits
  prompt-ready recognition context.
- Native Codex Stage 1 distilled memory is not relied on when it reports no
  stage1 outputs and errored memory jobs.

## Cursor

Status: adapter available; `bourdon cursor export` is the native SQLite export
path.

- `CursorAdapter` reads Cursor's SQLite state through a read-only temp copy.
- `bourdon cursor export` writes `~/agent-library/agents/cursor.l5.yaml`.
- The existing short-index memory-cycle scripts remain available for manually
  curated Cursor memory.

## Cline

Status: blocked pending confirmed native memory store path/schema.

- No Cline adapter should be added until its durable local memory source is
  known.
- Until then, Cline can consume Bourdon through the generic MCP or shell
  surfaces: `prepare_recognition_context` or `bourdon prepare-turn`.
