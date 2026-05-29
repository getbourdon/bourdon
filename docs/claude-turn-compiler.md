# Claude Code Turn-Scoped Recognition Compiler

Status: prototype architecture, implemented in `core/claude_turn_compiler.py`.

## Purpose

The Claude Code turn compiler is the Claude-side port of the Codex turn compiler
(see [`codex-turn-compiler.md`](codex-turn-compiler.md)). It builds a tiny ranked
brief for one Claude turn from the prompt, cwd/repo identity, local Claude
session transcripts, recent work, and federated L6 context.

Both compilers share one agent-agnostic engine — `core/turn_compiler.py`,
parameterized over a `SessionSource` seam. See
[`turn-compiler-architecture.md`](turn-compiler-architecture.md) for the design.

Claude's native auto-memory (`MEMORY.md`) is treated as a health signal, not a
dependency. The Claude analogue of Codex's degraded Stage 1 is the documented
`MEMORY.md` size-limit truncation: past a soft limit the loader only loads part
of the index (observed in the wild as e.g. *"MEMORY.md is 31.5KB (limit: 24.4KB)
— Only part of it was loaded."*). An oversized index classifies as `degraded`,
and active recognition is preferred over the partially-loaded native index.

## Native Surfaces

The Claude-specific reads (all read-only, never touching auth/credential files):

- **Native memory health** — the auto-memory index
  `~/.claude/projects/<slug>/memory/MEMORY.md`. Present + under the soft size
  limit → `available`; present + oversized → `degraded`; absent → `unknown`.
- **Local records** — live session transcripts
  `~/.claude/projects/<slug>/*.jsonl`, newest by mtime. Only the first few JSON
  records of each transcript are read to extract a thread name (first user
  message or a `summary` record) and a `cwd`. These are fresh sessions that may
  not yet be exported to the federation L5, so they catch in-flight work.

The per-workspace `<slug>` is Claude's encoding of the workspace path (path
separators and the Windows drive colon become `-`, e.g.
`C:\Users\cumul\repos\bourdon` → `C--Users-cumul-repos-bourdon`). The cwd read
from the transcript is authoritative; a best-effort slug decode is only a
fallback hint.

## Scoring Model

Identical to the Codex compiler — the scorer lives in the shared engine:

- Prompt match: exact entity, alias, title, and token-subsequence matches.
- Cwd/repo identity: repo basename, git remote, prior session cwd, and project
  focus overlap.
- Recency, cross-agent agreement, and continuity add signal.
- Penalties demote generic names, oversized summaries, and stale own-agent L5
  evidence.

Candidates pass a recognition gate before ranking. Prompt-matched anchors always
pass; cwd/repo-only anchors pass only for vague continuation prompts that
semantically name the current repo; local session threads do not win from cwd
alone. Native memory never gates output.

## Output Schema

The CLI and MCP surfaces return `claude-turn-brief/v1`:

```yaml
schema_version: claude-turn-brief/v1
prompt: Keep working on Bourdon recognition
cwd: C:\Users\cumul\repos\bourdon
repo:
  name: bourdon
  root: C:\Users\cumul\repos\bourdon
  remote: git@github.com:getbourdon/bourdon.git
health:
  native_memory: degraded
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
  suppressed_surfaces: [native_memory_primary]
trace:
  routing_decision: {}
  surface_health: {native_memory: degraded}
  source_mix: {}
  selected_items: []
diagnostics:
  scoring_components: {}
  auto_memory: {present: true, size_bytes: 32256, over_limit: true}
  exhausted_paths: []
```

The only structural differences from `codex-turn-brief/v1` are the agent-specific
labels: `schema_version`, `health.native_memory` (vs `native_stage1`),
`source: claude_session`/`claude_l5`, the `auto_memory` diagnostics block, and
prose nouns ("native memory", "Claude session", "the Claude turn").

## Delivery Surfaces

`bourdon claude-code compile-turn "<prompt>"` is read-only and defaults to all
delivery renderings:

- `explicit_text`: primary surface for direct pre-turn injection.
- `mcp_payload`: same compiled brief for MCP turn-start orchestration.
- `memory_md_block`: bounded optional block for `MEMORY.md` experiments.
- `fallback_block`: bounded optional block for `bourdon_fallback.md` experiments.
- `repo_overlay_block`: bounded candidate block for a future repo-local overlay,
  emitted only when repo identity and recognition anchors are present.

The MCP server exposes the same compiler through `compile_claude_turn(...)`,
using the server's configured L6 library path. The Codex compiler and all
existing tools are unchanged.

## Router And Trace

Identical policy to the Codex compiler:

- No useful anchors: observe only, no context injection.
- Medium confidence: prefer explicit pre-turn text.
- High confidence: prefer explicit pre-turn text plus MCP.
- Repo identity plus high confidence: mark a repo overlay as a candidate.
- Degraded native memory: suppress `native_memory_primary` as a route.

The trace records selected items, dominant score components, candidate/ignored
source mix, surface health, and the routing reason.

## Safety And Verification

The compiler is read-only. It does not read auth files, write native Claude
memory, mutate the federation library, run model calls, or execute shell
commands. It reuses the shared visibility filtering and credential redaction
(`core/text_safety.py`), and reads only the first few records of any transcript.

Tests (`tests/test_claude_turn_compiler.py`) mirror the Codex suite — ranking,
cwd/repo identity, cross-agent context without native memory, degraded native
memory routing, visibility filtering, redaction, output caps, schema identity,
router decisions, and trace output — plus Windows-shaped path coverage: slug
decoding and a local session transcript with a Windows-shaped slug + cwd match.
