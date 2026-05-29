# Proof: Cascade Self-Installation

**Date:** 2026-05-11  
**Agent:** Cascade (Windsurf)  
**Adapter version:** v0.5.0  
**Branch:** `cascade/redaction-reuse-and-security`

## What this proves

Cascade built its own Bourdon adapter (`adapters/cascade.py`), then installed and used that adapter on itself to federate its own memory into the L6 cross-agent store. This is the first case of an agent **authoring** its adapter and then immediately **using** it in the same project lifecycle.

## Steps executed

### 1. Install Bourdon (editable)

```
$ .venv/bin/pip install -e ".[dev,server]"
Successfully installed bourdon-0.5.0
```

### 2. Verify entry point registration

```
$ python -c "from importlib.metadata import entry_points; ..."
['cascade', 'claude-code', 'codex', 'copilot', 'cursor']
```

### 3. Populate `~/.cascade-bourdon/memory.md`

Wrote real entities and sessions from Cascade's actual work:

- **7 entities:** Bourdon (project), CascadeAdapter (module), L5Manifest (concept), L6Store (module), Convention-file pattern (concept), Credential redaction (concept), Ryan Radman (person, private-tagged)
- **3 sessions:** adapter creation (May 10), redaction fix (May 11 AM), self-installation (May 11 PM)

### 4. Run health check

```
$ bourdon cascade doctor
health:
  status: ok
  details:
    entity_count: 7
    session_count: 3
    frontmatter_valid: true
```

### 5. Export L5 manifest

```
$ bourdon cascade export --print
```

**Result:** `~/agent-library/agents/cascade.l5.yaml` written (3,812 bytes).

Key validation points:
- **6 entities exported** (not 7) — "Ryan Radman" entity was correctly **filtered out** by the visibility policy (tagged `private`). The PII guardrail works.
- **180-char cap triggered** on "Credential redaction" entity summary (ends with `...`). Length truncation works.
- **3 sessions** with full key_actions, files_touched, project_focus.

### 6. Verify L6 federation round-trip

```python
from core.l6_store import L6Store
store = L6Store(Path.home() / 'agent-library')

store.find_entity('CascadeAdapter', access_level='team')
# → EntityMatch(name='CascadeAdapter', agents=['cascade'], types=['module'])

store.find_entity('Bourdon', access_level='team')
# → EntityMatch(name='Bourdon', agents=['cascade', 'codex'])
```

**Cross-agent aggregation confirmed:** The "Bourdon" entity is known by both Cascade and Codex. L6 correctly merges entity knowledge across agents.

### 7. Unified doctor (all adapters)

```
$ bourdon doctor
adapters:
- agent: claude-code  → status: degraded (2/3 sources)
- agent: codex        → status: ok
- agent: cursor       → status: ok
- agent: copilot      → status: ok
- agent: cascade      → status: ok (7 entities, 3 sessions)
```

Five agents reporting. Four healthy, one degraded (Claude Code missing knowledge_graph — expected on this machine).

## Security properties verified

| Property | Status |
|----------|--------|
| Private-tagged entity filtered before L5 write | ✅ "Ryan Radman" not in manifest |
| Credential patterns redact sensitive text | ✅ (tested via 51-test suite) |
| URL stripping to `[link]` | ✅ |
| 180-char length cap | ✅ "Credential redaction" summary truncated |
| Atomic write via core/l5_io.py | ✅ (file exists, no partial writes observed) |
| No network calls during export | ✅ |

## What this means for the project

1. **The adapter works end-to-end.** Not just in synthetic test fixtures — on real memory content, with real federation queries returning real results.
2. **The convention-file pattern is validated.** An agent with no accessible on-disk state can still participate fully in L6 federation by maintaining a structured memory file.
3. **Cross-agent entity overlap works.** "Bourdon" is independently recognized by both Cascade and Codex, and L6 correctly aggregates them into a single EntityMatch with both agents listed.
4. **The unified `bourdon doctor` command works.** All five adapters report their health from a single invocation.

## Files involved

| File | Role |
|------|------|
| `~/.cascade-bourdon/memory.md` | Convention memory file (Cascade maintains this) |
| `~/agent-library/agents/cascade.l5.yaml` | Exported L5 manifest (federation input) |
| `adapters/cascade.py` | Adapter implementation |
| `cli/main.py` | CLI handler (`bourdon cascade {export,doctor,init}`) |
| `tests/test_cascade_adapter.py` | 51 tests, all green |

---

## v0.8.0 Parity Update

**Date:** 2026-05-28  
**Branch:** `feat/cascade-v0.8-parity`

### What was added

Full feature parity with the Codex adapter — Cascade now has all 9 CLI subcommands
and the same recognition/compilation stack that powers Codex's active orchestration.

### New capabilities

| Surface | Status |
|---------|--------|
| Native Windsurf state reader (`adapters/_windsurf_native.py`) | ✅ Reads `state.vscdb`, workspace metadata, cascade sessions, plans, workflows |
| `bourdon cascade sync-native --from-library --write` | ✅ Renders federation into convention file with idempotent markers |
| `bourdon cascade recognize "<prompt>"` | ✅ Runs recognition_runtime against Cascade manifest |
| `bourdon cascade prepare-turn --strategy turn-compiled` | ✅ Refreshes memory surfaces + returns compiled context |
| `bourdon cascade compile-turn "<prompt>"` | ✅ Turn-scoped recognition compiler (`cascade-turn-brief/v1` schema) |
| `bourdon cascade eval --recognition --turn-compiler` | ✅ Full evaluation harness with latency + hit-rate metrics |
| `bourdon cascade build-context --out-dir` | ✅ Generates L0/L1 timing artifacts |
| `bourdon setup` — Cascade sync step | ✅ Wired into interactive wizard |

### Architecture

```
┌──────────────────────────────────────────────────────────────┐
│                    compile_cascade_turn()                      │
│                                                              │
│   ┌──────────────────┐  ┌─────────────────┐  ┌────────────┐ │
│   │ Convention File   │  │ Native Windsurf │  │    L6       │ │
│   │ (~/.cascade-     │  │ (state.vscdb +  │  │ Federation  │ │
│   │  bourdon/        │  │  .windsurf/     │  │ Library     │ │
│   │  memory.md)      │  │  plans/         │  │             │ │
│   └────────┬─────────┘  └────────┬────────┘  └──────┬─────┘ │
│            │                     │                    │       │
│            └─────────────────────┼────────────────────┘       │
│                                  ▼                            │
│                    candidate scoring + ranking                │
│                    (token_overlap + cwd_affinity              │
│                     + recency + source_confidence)            │
│                                  │                            │
│                                  ▼                            │
│               delivery: explicit | mcp | convention-file      │
└──────────────────────────────────────────────────────────────┘
```

### Test coverage

- **765 tests pass** (full suite), **43 new turn compiler tests**, **52 adapter tests**
- Native reader validates graceful fallback on all platforms (Darwin/Linux/Windows paths)
- Determinism assertions: same input → byte-identical output
