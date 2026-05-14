# Agent Integration Status

This page tracks the operational Bourdon layer available for each agent. It is
the current implementation map, not the long-term adapter wishlist.

## Claude Code

Status: export hook available.

- `bourdon claude-code export` writes `~/agent-library/agents/claude-code.l5.yaml`.
- Intended hook: Claude Code `SessionEnd`.
- Current role: manager/reviewer memory source for L6 federation.

## Codex

Status: SQLite-backed fallback and turn preparation available.

- `bourdon codex doctor` diagnoses `~/.codex/state_5.sqlite`, stale-index fallback,
  and fallback recall.
- `bourdon codex export` prefers live `state_5.sqlite` thread metadata and falls
  back to `session_index.jsonl` on older Codex installs.
- `bourdon codex prepare-turn` refreshes Codex fallback memory surfaces and emits
  prompt-ready recognition context.
- Native Codex Stage 1 distilled memory is not relied on when it reports no
  stage1 outputs and errored memory jobs.

## Cursor

Status: adapter available; `bourdon cursor export` is the native SQLite export
path.

- `CursorAdapter` reads Cursor's SQLite state through a read-only temp copy.
- `bourdon cursor export` writes `~/agent-library/agents/cursor.l5.yaml`.
- **Setup walkthrough:** [`docs/integrations/cursor.md`](integrations/cursor.md) (MCP in Cursor IDE + export).
- The existing short-index memory-cycle scripts remain available for manually
  curated Cursor memory.

## Cline

Status: blocked pending confirmed native memory store path/schema.

- No Cline adapter should be added until its durable local memory source is
  known.
- Until then, Cline can consume Bourdon through the generic MCP or shell
  surfaces: `prepare_recognition_context` or `bourdon prepare-turn`.

## OpenManus

Status: zero-code MCP integration — OpenManus consumes Bourdon's L6 server as
an MCP source.

- OpenManus is MCP-native (`Manus.mcp_clients`, `config/mcp.json` schema).
- Add a `bourdon` entry to OpenManus's `config/mcp.json` pointing at
  `python -m core.l6_server` (stdio) — see
  [`docs/integrations/openmanus.md`](integrations/openmanus.md) for the literal
  config block and walkthrough.
- After wiring, the OpenManus agent gets all seven L6 tools
  (`query_agent_memory`, `list_recent_work`, `find_entity`,
  `commit_to_federation`,
  `get_cross_agent_summary`, `prepare_recognition_context`,
  `get_deeper_context`) plus the `agent-library://` resources.
- OpenManus consumes Bourdon by default via read tools and can **publish**
  federation updates through `commit_to_federation` whenever the orchestrator
  surfaces that MCP surface to its model loop (same pattern documented in
  `docs/AUTHORING_AN_ADAPTER.md` for other cloud-first agents).

## Cascade (Windsurf)

Status: adapter available; `bourdon cascade export` is the convention-file
export path.

- Cascade has no standardized on-disk session state (similar to Copilot). The
  adapter reads from a **convention-based memory file** at
  `~/.cascade-bourdon/memory.md` that Cascade maintains at session end.
- `bourdon cascade init` creates `~/.cascade-bourdon/memory.md` with a starter
  template. Edit the YAML front-matter to add entities and sessions.
- `bourdon cascade export` reads the file, applies visibility filtering, and
  writes `~/agent-library/agents/cascade.l5.yaml`.
- `bourdon cascade doctor` diagnoses the memory file and reports entity/session
  counts, front-matter validity, and health status.
- Credential redaction uses the canonical pattern set from `adapters/codex.py`,
  extended with Cascade-specific patterns (`secret`, `sk_test_*`). See
  [`SECURITY.md`](../SECURITY.md) for the full runtime security model.
- Current role: agentic pair-programmer with multi-step planning for L6
  federation.

## GitHub Copilot

Status: adapter available; `bourdon copilot export` is the convention-file
export path.

- GitHub Copilot has no accessible on-disk session index (cloud-side reasoning,
  no session JSONL). The adapter reads from a **convention-based memory file**
  at `~/.copilot-bourdon/memory.md` that users or Copilot Chat can maintain.
- `bourdon copilot init` creates `~/.copilot-bourdon/memory.md` with a starter
  template. Edit the YAML front-matter to add entities and sessions.
- `bourdon copilot export` reads the file, applies visibility filtering, and
  writes `~/agent-library/agents/copilot.l5.yaml`.
- `bourdon copilot doctor` diagnoses the memory file and reports entity/session
  counts, front-matter validity, and health status.
- Current role: inline-completion and chat ambient layer for L6 federation.
- Copilot Chat can be instructed to update `memory.md` at session end using a
  custom instruction like:

  > "At the end of each session, append a YAML session block to the `sessions:`
  > list in `~/.copilot-bourdon/memory.md`, and add any new project or concept
  > entities to the `entities:` list."
