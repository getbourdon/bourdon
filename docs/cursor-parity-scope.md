# Cursor Parity Scope — Bringing Cursor to Feature Parity with Codex and Claude Code

**Status**: Scoping document  
**Author**: Cursor (Second Engineer)  
**Date**: 2026-06-06  

## Context

As of v0.8.0, Codex and Claude Code each have:
- Rich interactive adapters with multi-source memory parsing
- **Automation memory adapters** (`codex-automations`, `claude-code-automations`) that publish background/scheduled agent work into federation
- Full CLI surfaces (`doctor`, `export`, `prepare-turn`, `sync-native`, etc.)
- Cross-machine federation participation via `bourdon sync` and peer L6 HTTP

Cursor has a working interactive adapter (`adapters/cursor.py`, 248 lines) that reads SQLite `state.vscdb` and exports an L5 manifest. It has **one** CLI subcommand (`export`). It participates in cross-machine federation passively (its L5 file syncs and is queryable) but has no automation memory, no doctor subcommand, no write-back, and no turn-scoped recognition.

## Gap Analysis

### What Cursor has today

| Capability | Status |
|---|---|
| `BourdonAdapter` Protocol compliance | Yes |
| `bourdon cursor export` | Yes |
| Entry point registration | Yes |
| `bourdon doctor` participation (via `health_check()`) | Yes |
| `bourdon export-all` participation | Yes |
| `bourdon dogfood` federation roundtrip | Yes |
| Cross-machine sync (L5 file participates in `bourdon sync`) | Yes (passive) |
| Peer L6 federation (queryable by remote peers) | Yes (passive) |
| Test coverage | 220 lines + federation roundtrip |

### What Cursor is missing

| Gap | Priority | Codex | Claude Code | Copilot/Cascade |
|---|---|---|---|---|
| **1. `cursor doctor` subcommand** | High | Yes (deep) | No (global only) | Yes |
| **2. `cursor-automations` adapter** | High | Yes | Yes | No |
| **3. Credential scrubbing in SQLite text** | High | Yes | Yes | N/A |
| **4. `cursor init` subcommand** | Medium | No | No | Yes |
| **5. Richer entity model** (topics, preferences, aliases, temporal validity) | Medium | Yes | Yes | No |
| **6. Multi-source `discover()` metadata** | Medium | Yes (rich sources map) | Yes | No |
| **7. `CURSOR_DIR` env var** (mentioned in `health_check` but not implemented) | Medium | `CODEX_HOME` works | `CLAUDE_BRAIN` works | N/A |
| **8. Hook-safe export** (silent, never raises) | Low | N/A | Yes | N/A |
| **9. `sync-native` / `--from-library`** | Low | Yes | No | No |
| **10. Turn-scoped recognition compiler** | Low | Yes | No | No |
| **11. Short-index pipeline integration** | Low | N/A | N/A | N/A |
| **12. Linux path detection mismatch** | Low | N/A | N/A | N/A |

## Proposed Implementation Plan

### Phase 1: Foundation (High Priority)

#### 1A. `bourdon cursor doctor` subcommand
**What**: Add a `doctor` subparser under `bourdon cursor` that calls `CursorAdapter.health_check()` with formatted output matching the Copilot/Cascade pattern.

**Files**: `cli/main.py` (add subparser + handler)

**Reference**: `_handle_copilot_doctor` in `cli/main.py` — straightforward pattern to copy.

#### 1B. Credential scrubbing
**What**: Run composer/chat text through `_safe_native_memory_text()` (imported from `adapters.codex`) before emitting it in `key_actions` or entity summaries. Currently the SQLite extraction passes raw text through without redaction.

**Files**: `adapters/cursor.py` (`_to_session`, `_to_entity`), `adapters/_cursor_sqlite.py`

**Reference**: `adapters/codex.py::_safe_native_memory_text` — reuse, don't reimplement.

#### 1C. `CURSOR_DIR` environment variable
**What**: Read `os.environ.get("CURSOR_DIR")` as an override in `CursorAdapter.__init__` when no explicit `cursor_dir` is passed. The `health_check()` already mentions it in its `proposed_fix` text.

**Files**: `adapters/cursor.py` (`__init__`), `adapters/_cursor_sqlite.py` (`default_cursor_dir`)

#### 1D. `cursor-automations` adapter
**What**: Create `adapters/cursor_automations.py` following the exact pattern of `codex_automations.py`. Reads `~/.cursor/automations/<id>/{automation.toml, memory.md}`.

Cursor Cloud Agents run background tasks (PR reviews, code generation, etc.) that produce work artifacts. This adapter would make that work visible to federation so other agents can recognize what Cursor did.

**Files to create**:
- `adapters/cursor_automations.py` (~440 lines, mirror `codex_automations.py`)
- `tests/test_cursor_automations_adapter.py` (mirror `test_codex_automations_adapter.py`)

**Files to modify**:
- `pyproject.toml` — add entry point `cursor-automations = "adapters.cursor_automations:CursorAutomationsAdapter"`
- `cli/main.py` — add `cursor-automations` subparser group with `export` and `doctor`

**Convention**: `~/.cursor/automations/` (or `$CURSOR_HOME/automations/`)

### Phase 2: Enrichment (Medium Priority)

#### 2A. `cursor init` subcommand
**What**: Create `~/.cursor-bourdon/` convention directory with a starter `memory.md` template, similar to Copilot/Cascade `init`. This could also optionally create `~/.cursor/automations/` for the automation adapter.

**Files**: `cli/main.py`

#### 2B. Richer entity model
**What**: Extend `_to_entity` to extract:
- **Project entities** with `last_updated` timestamps from SQLite `lastUpdatedAt`
- **Topic entities** from recurring conversation themes
- **Aliases** from project path basenames
- **`valid_from`/`valid_to`** temporal windows when sessions span known date ranges

**Files**: `adapters/cursor.py`, `adapters/_cursor_sqlite.py`

#### 2C. Multi-source `discover()` metadata
**What**: Return a richer `AgentStore.metadata` dict listing which SQLite databases were found, their sizes, and row counts — matching the Codex pattern for doctor/debug observability.

**Files**: `adapters/cursor.py` (`discover()`)

#### 2D. Setup wizard Cursor step
**What**: In `bourdon setup`, add a Cursor-specific setup step that creates `~/.cursor/automations/` and optionally configures Cursor MCP settings to point at `bourdon serve`.

**Files**: `cli/main.py` (`_handle_setup`)

### Phase 3: Advanced (Low Priority)

#### 3A. Hook-safe export
**What**: Make `bourdon cursor export` silent and never-raise for SessionEnd hook usage (matching Claude Code's contract). Currently can raise on failure.

#### 3B. Short-index pipeline integration
**What**: The legacy short-index pipeline (`scripts/build_bourdon_l5.py`, `.cursor/memory/short-index.json`) exists outside the adapter. Consider merging curated short-index entities into `CursorAdapter.export_l5()` as a secondary source.

#### 3C. `sync-native` equivalent
**What**: Write federation content back into a Cursor-readable format. This would be the equivalent of Codex's `sync-native --from-library` that seeds Codex's `MEMORY.md` from federation. For Cursor, this could write to `.cursor/memory/` or a workspace-level context file.

#### 3D. Turn-scoped recognition
**What**: A Cursor-specific turn compiler that builds recognition briefs optimized for Cursor's context window and interaction patterns.

## Effort Estimates

| Phase | Components | Invasiveness |
|---|---|---|
| Phase 1 | 1A + 1B + 1C + 1D | Low-moderate. 1A/1B/1C are small edits to existing files. 1D is a new adapter file (~440 lines) following established patterns. |
| Phase 2 | 2A + 2B + 2C + 2D | Moderate. Entity enrichment requires deeper SQLite parsing; setup wizard needs UI text. |
| Phase 3 | 3A + 3B + 3C + 3D | Higher. Short-index merge and write-back require design decisions about Cursor's native format. |

## Dependencies and Risks

- **Cursor Cloud Agent automation convention**: The `~/.cursor/automations/` convention needs to be adopted by Cursor Cloud Agents for 1D to be useful. Without it, the adapter has no data to read.
- **SQLite schema stability**: Cursor's `state.vscdb` schema is undocumented and could change. Entity enrichment (2B) increases surface area.
- **Setup wizard UX**: Adding Cursor steps to `bourdon setup` requires knowing what MCP config format Cursor expects.

## Recommended Starting Point

**Phase 1A + 1B + 1C** can be done in a single PR — they're small, well-scoped changes to existing files with clear reference patterns. Phase 1D (automation adapter) should be its own PR since it introduces new files.
