# Codex Turn-Scoped Recognition Compiler

Status: prototype architecture, implemented in `core/codex_turn_compiler.py`.

## Purpose

The Codex turn compiler is Bourdon's first explicit move from passive memory
storage toward active recognition orchestration. It builds a tiny ranked brief
for one Codex turn from the prompt, cwd/repo identity, Codex thread metadata,
recent work, and federated L6 context.

Native Codex Stage 1 is treated as a health signal, not as a dependency. This
matches the current local evidence from `scripts/codex_memory_metrics.py`: Stage
1 can be degraded by usage-limit and context-window failures while file-based
context, MCP reads, fallback recall, and the federation library remain usable.

## Pipeline

1. Validate prompt, access level, item count, character budget, and delivery
   mode.
2. Resolve cwd and git repo identity by walking up to `.git` and reading
   `.git/config`; no shell command is required.
3. Inspect Codex Stage 1 health read-only through the existing Codex participant
   SQLite inspection helper.
4. Load L6 recognition context from `agent-library/agents/*.l5.yaml` using the
   existing visibility-filtered store.
5. Add local Codex thread/session candidates from read-only Codex metadata.
6. Score candidates deterministically and render bounded delivery payloads.
7. Route the resulting brief to the strongest surface and attach a compact
   trace explaining the decision.

## Scoring Model

The v1 scorer favors recognition cues over exhaustive recall:

- Prompt match: exact entity, alias, title, and token-subsequence matches.
- Cwd/repo identity: repo basename, git remote, prior session cwd, and project
  focus overlap.
- Recency: recent sessions and threads receive a small boost.
- Cross-agent agreement: entities known by more than one L5 agent rank higher.
- Continuity: files touched, prior cwd, and recent thread continuity add signal.
- Penalties: generic names, oversized summaries, stale Codex-only evidence, and
  native Stage 1-only candidates are demoted.

Candidates must pass a recognition gate before ranking. Prompt-matched anchors
always pass. Cwd/repo-only anchors pass only for vague continuation prompts and
only when the candidate semantically names the current repo; recent threads do
not win from cwd alone. This keeps active recognition from firing on unrelated
requests like weather or generic Q&A.

Native Stage 1 never gates output.

## Output Schema

The CLI and MCP surfaces return `codex-turn-brief/v1`:

```yaml
schema_version: codex-turn-brief/v1
prompt: Can we keep working on Bourdon?
cwd: /Users/radman/bourdon
repo:
  name: bourdon
  root: /Users/radman/bourdon
  remote: git@github.com:...
health:
  native_stage1: degraded
  strategy: turn_compiled
items:
  - rank: 1
    score: 58.0
    kind: project
    name: Bourdon
    summary: Recognition orchestration substrate.
    reason: prompt matched Bourdon; repo name matched candidate
    source: l6_federation
    source_agents: [claude-code, codex]
    evidence: [known by claude-code, codex]
delivery:
  explicit_text: "Bourdon turn recognition brief..."
  mcp_payload: {}
  memory_md_block: ""
  fallback_block: ""
routing:
  mode: inject
  primary_surface: explicit_pre_turn
  surfaces: [explicit_pre_turn, mcp, repo_overlay_candidate]
  confidence: high
  reason: top anchor scored 58.0
  suppressed_surfaces: [native_stage1_primary]
trace:
  routing_decision: {}
  surface_health: {}
  source_mix: {}
  selected_items: []
diagnostics:
  scoring_components: {}
  exhausted_paths: []
```

## Delivery Surfaces

`bourdon codex compile-turn "<prompt>"` is read-only and defaults to all delivery
renderings. `bourdon codex prepare-turn "<prompt>" --strategy turn-compiled`
uses the same compiler for `prompt_context` while leaving the legacy strategy as
the default:

- `explicit_text`: primary v1 surface for direct pre-turn injection.
- `mcp_payload`: same compiled brief for MCP turn-start orchestration.
- `memory_md_block`: bounded optional block for `MEMORY.md` experiments.
- `fallback_block`: bounded optional block for `bourdon_fallback.md` experiments.
- `repo_overlay_block`: bounded candidate block for a future repo-local overlay
  surface, emitted only when repo identity and recognition anchors are present.

The MCP server exposes the same compiler through `compile_codex_turn(...)`, using
the server's configured L6 library path. Existing `prepare_recognition_context`,
legacy `prepare-turn`, `sync-native`, and fallback behavior are unchanged.

## Router And Trace

The router is a small policy layer over the ranked items:

- No useful anchors: observe only, no context injection.
- Medium confidence: prefer explicit pre-turn text.
- High confidence: prefer explicit pre-turn text plus MCP.
- Repo identity plus high confidence: mark a repo overlay as a candidate next
  surface.
- Degraded Stage 1: suppress native Stage 1 as a primary route.

The trace records the selected items, dominant score components, candidate source
mix, ignored source mix, surface health, and routing reason. This is meant to be
the debugging cockpit for recognition behavior: it explains why Bourdon chose a
surface without making the model rely on hidden state.

## Eval Harness

`bourdon codex eval --turn-compiler` attaches a deterministic compiler report to
the existing Codex eval output. It runs the canonical recognition prompts through
the turn compiler and reports compiled hit rate, average latency, primary surface
counts, confidence counts, and the top anchor per prompt.

Fixture mode writes the fixture manifest to a temporary agent-library so the
compiler exercises the same L6 file path it uses in production. Live mode can use
`--library-path`; without it, the current Codex manifest is exported to a
temporary library for a read-only local check.

## Safety And Verification

The compiler is read-only. It does not read `auth.json`, write native Codex
memory files, mutate SQLite, run model calls, or execute shell commands. It
reuses existing visibility filtering and Codex redaction helpers.

Tests cover ranking, cwd/repo identity, cross-agent context without Stage 1,
degraded Stage 1 routing, visibility filtering, redaction, output caps, CLI
schema, report writing, router decisions, trace output, and MCP helper shape.
