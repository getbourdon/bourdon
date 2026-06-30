# Codex Pre-Turn Injection Dogfood

Purpose: validate Bourdon v0.8.0 as an active recognition orchestration layer.
This workflow is about pre-turn injection, not passive memory storage.

## Protocol

1. Capture the user prompt exactly as the next Codex turn will see it.
2. Compile a turn-scoped recognition brief:

   ```bash
   bourdon codex compile-turn "What should we work on next in Bourdon?" \
     --cwd /Users/radman/bourdon \
     --library-path /Users/radman/agent-library \
     --max-items 6 \
     --max-chars 1800
   ```

3. Read `delivery.explicit_text`.
4. Prepend that text before the user prompt in the next Codex turn.
5. Observe whether Codex recognizes the right project, task, handoff, and
   recent-work anchors.
6. Record misses, false positives, latency, and whether routing chose the right
   surface.

The filled protocol layer is:

```text
prompt/request -> turn compiler -> tiny recognition brief -> explicit pre-turn injection
```

## What Counts As Success

- `schema_version` is `codex-turn-brief/v1`.
- `routing.primary_surface` is `explicit_pre_turn` for relevant prompts.
- `routing.primary_surface` is `none` for unrelated prompts.
- `health.native_stage1` may be `degraded`; recognition still works.
- `delivery.explicit_text` is short enough to paste or prepend without crowding
  the actual task.
- The top item is recognizably the intended project, handoff, or continuation
  anchor.

## What This Is Not

- Not a default write to `MEMORY.md`.
- Not a default write to `bourdon_fallback.md`.
- Not a dependency on Codex native Stage 1.
- Not a Claude Code port.
- Not a repo overlay lifecycle.

Those are separate surfaces. The v0.8.0 dogfood loop keeps explicit pre-turn text
as the primary surface.

## Quick Eval

Run the fixture quality set:

```bash
bourdon codex eval --fixtures --turn-compiler
```

The `turn_compiler.quality.failed_expectations` list should be empty. The fixture
set covers a direct project prompt, vague repo continuation, handoff continuity,
an absent entity, and an unrelated prompt.

Run a live check:

```bash
bourdon codex eval --live --turn-compiler \
  --cwd /Users/radman/bourdon \
  --library-path /Users/radman/agent-library
```

Live mode reports routing and confidence, but does not enforce fixture
expectations because each machine's memory library is different.
