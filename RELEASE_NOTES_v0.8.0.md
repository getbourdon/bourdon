# v0.8.0 — Active Codex recognition orchestration

**v0.7.0 made Bourdon adoptable. v0.8.0 makes Codex recognition active.**

This release reframes Codex memory around the original Bourdon thesis: active
recognition orchestration across asymmetric agent surfaces. Federated memory is
still a key transport, but Codex native Stage 1 is no longer treated as the
dependency that must succeed before recognition can work.

## What's new

### `codex-turn-brief/v1`

New `core/codex_turn_compiler.py` compiles a tiny, ranked recognition brief per
turn from prompt text, cwd/repo identity, lightweight Codex thread metadata,
recent work, and L6 federation context.

Native Stage 1 is demoted from required source to health signal:

- `available`, `degraded`, or `unknown` appears in brief health.
- degraded Stage 1 suppresses native memory as a primary route.
- file-based, MCP, and explicit turn-time surfaces remain usable.

### CLI surfaces

```bash
bourdon codex compile-turn "<prompt>"
bourdon codex prepare-turn --strategy turn-compiled "<prompt>"
bourdon codex eval --turn-compiler
```

`compile-turn` is read-only and emits the full `codex-turn-brief/v1` schema.
`prepare-turn --strategy turn-compiled` keeps legacy behavior as the default
while allowing Codex sessions to opt into the active compiler. `eval
--turn-compiler` records hit rate, route confidence, primary surfaces, and
latency against canonical prompts.

### MCP surface

The L6 server now exposes `compile_codex_turn(...)`, returning the same schema as
the CLI. Existing `prepare_recognition_context` behavior is unchanged.

### Additive delivery surfaces

The compiler renders bounded delivery shapes for:

- explicit pre-turn text
- MCP payloads
- `MEMORY.md` compatibility blocks
- `bourdon_fallback.md` compatibility blocks
- repo overlay candidate blocks

The primary v1 route is explicit pre-turn text. Native files remain secondary
compatibility surfaces, not the core strategy.

## Safety

This release is read-only by default. The compiler does not mutate Codex SQLite,
write native memory files, read auth files, call models, or make network
requests. It enforces access-level filtering, credential-like text redaction,
bounded prompt/output sizes, and deterministic scoring.

## Counts

- Tests passing: 727
- Skipped: 1
- New schema: `codex-turn-brief/v1`

## Migration

Pure additive release. Existing `recognize`, `prepare-turn`, `sync-native`,
fallback recall, and L6 federation behavior remain compatible. To try the new
path, opt in explicitly with `--strategy turn-compiled` or call
`bourdon codex compile-turn`.
