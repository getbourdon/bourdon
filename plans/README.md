# Implementation Plans

Generated 2026-06-10 by claude-desktop-chat (advisor), following the
shadcn/improve plan format — adopted as this fleet's handoff standard.
Executors: read the full plan before starting, honor its STOP conditions,
and update your status row when done.

## Execution order & status

| Plan | Title | Priority | Effort | Depends on | Status |
|------|-------|----------|--------|------------|--------|
| 001  | Federate improve-style plan backlogs via `bourdon improve sync` | P1 | M | — | DONE |

Status values: TODO | IN PROGRESS | DONE | BLOCKED (reason) | REJECTED (rationale)

## Dependency notes

- None yet.

## Findings considered and rejected

- Implement as a `participants/` class: rejected for v1 — participants scrape
  agent-native session stores; an improve backlog is repo-scoped, not
  agent-scoped. Revisit only if watch-mode/autodiscovery is needed.
- Sync via the MCP `commit_to_federation` tool: rejected — transport currently
  times out on writes (v0.9.1 known issue); in-process `L6Store.commit_l5` is
  the correct integration point and doubles as a write-path diagnostic.
