# Cursor Parity Scope — Bringing Cursor to Feature Parity with Codex and Claude Code

**Status**: Scoping document — brief for a fresh Cursor Cloud Agent run after PR #116 was retired
**Original Author**: Cursor (Second Engineer) — 2026-06-06 morning
**Reauthored against post-#117 main**: 2026-06-06 evening
**Predecessor PR**: #116 (closed; authored against the pre-#117 `Adapter` world that no longer exists on main)

## Why this doc was rewritten

PR #116 shipped a strong implementation of the parity work below — but it was authored against `adapters/` + `BourdonAdapter` before PR #117 (`refactor!: rename Adapter -> Participant across protocol`) landed. The PR conflicted with main in ways no straight rebase could resolve: the directory itself moved (`adapters/` → `participants/`), the base Protocol class renamed (`BourdonAdapter` → `BourdonParticipant`), the spec docs renamed (`ADAPTER_CONTRACT.md` → `PARTICIPANT_CONTRACT.md`, `AUTHORING_AN_ADAPTER.md` → `AUTHORING_A_PARTICIPANT.md`), and the `AGENTS.md` structure was reorganized. Closing #116 and re-running the work against this updated brief is cleaner than manually porting 1,743 lines.

**Scope of work below is unchanged.** Only terminology and file paths are updated to match post-#117 main.

## Context

As of v0.8.0, Codex and Claude Code each have:
- Rich interactive participants with multi-source memory parsing
- **Automation memory participants** (`codex-automations`, `claude-code-automations`) that publish background/scheduled agent work into federation
- Full CLI surfaces (`doctor`, `export`, `prepare-turn`, `sync-native`, etc.)
- Cross-machine federation participation via `bourdon sync` and peer L6 HTTP

Cursor has a working interactive participant (`participants/cursor.py`) that reads SQLite `state.vscdb` and exports an L5 manifest. It has **one** CLI subcommand (`export`). It participates in cross-machine federation passively (its L5 file syncs and is queryable) but has no automation memory, no doctor subcommand, no write-back, and no turn-scoped recognition.

## Gap Analysis

### What Cursor has today

| Capability | Status |
|---|---|
| `BourdonParticipant` Protocol compliance | Yes |
| `bourdon cursor export` | Yes |
| Entry point registration | Yes |
| `bourdon doctor` participation (via `health_check()`) | Yes |
| `bourdon export-all` participation | Yes |
| `bourdon dogfood` federation roundtrip | Yes |
| Cross-machine sync (L5 file participates in `bourdon sync`) | Yes (passive) |
| Peer L6 federation (queryable by remote peers) | Yes (passive) |
| Test coverage | Roughly 220 lines + federation roundtrip |

### What Cursor is missing

| Gap | Priority | Codex | Claude Code | Copilot/Cascade |
|---|---|---|---|---|
| **1. `cursor doctor` subcommand** | High | Yes (deep) | No (global only) | Yes |
| **2. `cursor-automations` participant** | High | Yes | Yes | No |
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
**What**: Add a `doctor` subparser under `bourdon cursor` that calls `CursorParticipant.health_check()` with formatted output matching the Copilot/Cascade pattern.

**Files**: `cli/main.py` (add subparser + handler)

**Reference**: `_handle_copilot_doctor` in `cli/main.py` — straightforward pattern to copy.

#### 1B. Credential scrubbing
**What**: Run composer/chat text through `_safe_native_memory_text()` (imported from `participants.codex`) before emitting it in `key_actions` or entity summaries. Currently the SQLite extraction passes raw text through without redaction.

**Files**: `participants/cursor.py` (`_to_session`, `_to_entity`), `participants/_cursor_sqlite.py`

**Reference**: `participants/codex.py::_safe_native_memory_text` — reuse, don't reimplement.

#### 1C. `CURSOR_DIR` environment variable
**What**: Read `os.environ.get("CURSOR_DIR")` as an override in `CursorParticipant.__init__` when no explicit `cursor_dir` is passed. The `health_check()` already mentions it in its `proposed_fix` text.

**Files**: `participants/cursor.py` (`__init__`), `participants/_cursor_sqlite.py` (`default_cursor_dir`)

#### 1D. `cursor-automations` participant
**What**: Create `participants/cursor_automations.py` following the exact pattern of `codex_automations.py`. Reads `~/.cursor/automations/<id>/{automation.toml, memory.md}`.

Cursor Cloud Agents run background tasks (PR reviews, code generation, etc.) that produce work artifacts. This participant would make that work visible to federation so other agents can recognize what Cursor did.

**Files to create**:
- `participants/cursor_automations.py` (~440 lines, mirror `codex_automations.py`)
- `tests/test_cursor_automations_participant.py` (mirror `test_codex_automations_participant.py`)

**Files to modify**:
- `pyproject.toml` — add entry point `cursor-automations = "participants.cursor_automations:CursorAutomationsParticipant"`
- `cli/main.py` — add `cursor-automations` subparser group with `export` and `doctor`

**Convention**: `~/.cursor/automations/` (or `$CURSOR_HOME/automations/`)

### Phase 2: Enrichment (Medium Priority)

#### 2A. `cursor init` subcommand
**What**: Create `~/.cursor-bourdon/` convention directory with a starter `memory.md` template, similar to Copilot/Cascade `init`. This could also optionally create `~/.cursor/automations/` for the automation participant.

**Files**: `cli/main.py`

#### 2B. Richer entity model
**What**: Extend `_to_entity` to extract:
- **Project entities** with `last_updated` timestamps from SQLite `lastUpdatedAt`
- **Topic entities** from recurring conversation themes
- **Aliases** from project path basenames
- **`valid_from`/`valid_to`** temporal windows when sessions span known date ranges

**Files**: `participants/cursor.py`, `participants/_cursor_sqlite.py`

#### 2C. Multi-source `discover()` metadata
**What**: Return a richer `AgentStore.metadata` dict listing which SQLite databases were found, their sizes, and row counts — matching the Codex pattern for doctor/debug observability.

**Files**: `participants/cursor.py` (`discover()`)

#### 2D. Setup wizard Cursor step
**What**: In `bourdon setup`, add a Cursor-specific setup step that creates `~/.cursor/automations/` and optionally configures Cursor MCP settings to point at `bourdon serve`.

**Files**: `cli/main.py` (`_handle_setup`)

### Phase 3: Advanced (Low Priority)

#### 3A. Hook-safe export
**What**: Make `bourdon cursor export` silent and never-raise for SessionEnd hook usage (matching Claude Code's contract). Currently can raise on failure.

#### 3B. Short-index pipeline integration
**What**: The legacy short-index pipeline (`scripts/build_bourdon_l5.py`, `.cursor/memory/short-index.json`) exists outside the participant. Consider merging curated short-index entities into `CursorParticipant.export_l5()` as a secondary source.

**Note**: PR #116 fixed a bug here where the global short-index path was being resolved against the SQLite data dir rather than `$CURSOR_DIR`. The fix should be carried forward.

#### 3C. `sync-native` equivalent
**What**: Write federation content back into a Cursor-readable format. This would be the equivalent of Codex's `sync-native --from-library` that seeds Codex's `MEMORY.md` from federation. For Cursor, this could write to `.cursor/memory/bourdon_context.md` or a workspace-level context file.

#### 3D. Turn-scoped recognition
**What**: A Cursor-specific turn compiler that builds recognition briefs optimized for Cursor's context window and interaction patterns. New module: `core/cursor_turn_compiler.py`.

## Effort Estimates

| Phase | Components | Invasiveness |
|---|---|---|
| Phase 1 | 1A + 1B + 1C + 1D | Low-moderate. 1A/1B/1C are small edits to existing files. 1D is a new participant file (~440 lines) following established patterns. |
| Phase 2 | 2A + 2B + 2C + 2D | Moderate. Entity enrichment requires deeper SQLite parsing; setup wizard needs UI text. |
| Phase 3 | 3A + 3B + 3C + 3D | Higher. Short-index merge and write-back require design decisions about Cursor's native format. |

## Dependencies and Risks

- **Cursor Cloud Agent automation convention**: The `~/.cursor/automations/` convention needs to be adopted by Cursor Cloud Agents for 1D to be useful. Without it, the participant has no data to read.
- **SQLite schema stability**: Cursor's `state.vscdb` schema is undocumented and could change. Entity enrichment (2B) increases surface area. See `DECISIONS.md` 2026-06-04 ("Codex adapter must introspect SQLite schema") for the pattern to apply here.
- **Setup wizard UX**: Adding Cursor steps to `bourdon setup` requires knowing what MCP config format Cursor expects.
- **Participant pattern choice**: This brief assumes adapter-reading-native-state (current Cursor approach). See `DECISIONS.md` 2026-06-06 ("Three federation participant patterns") — the strategic default is convention-file, but Cursor's existing SQLite reader stays as-is for this work. A future migration to convention-file is opportunistic, not in scope here.

## Recommended Starting Point

**Phase 1A + 1B + 1C** can be done in a single PR — they're small, well-scoped changes to existing files with clear reference patterns. Phase 1D (automation participant) should be its own PR since it introduces new files.

## See also

- `spec/PARTICIPANT_CONTRACT.md` — the formal contract every participant must satisfy
- `docs/AUTHORING_A_PARTICIPANT.md` — guide for authoring a new participant
- `participants/codex.py`, `participants/codex_automations.py` — reference implementations to mirror
- PR #116 (closed) — the original full-parity implementation, retired due to pre-#117 base
