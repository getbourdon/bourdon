# Codex Memory Metrics

`scripts/codex_memory_metrics.py` collects a read-only snapshot of Codex native
memory health and Bourdon federation surfaces.

The snapshot exists to make the v0.8.0 thesis measurable: native Stage 1 may be
degraded, but file-based context, fallback recall, L5, L6, MCP, and pre-turn
compiler surfaces can still carry recognition.

## Run

```bash
python scripts/codex_memory_metrics.py --skip-mcp
```

Include MCP wiring when the Codex CLI is available:

```bash
python scripts/codex_memory_metrics.py
```

Write a trendable report:

```bash
python scripts/codex_memory_metrics.py \
  --reports-dir reports/codex-memory \
  --format json
```

The first run writes `latest.json` and has no trend. Later runs compare against
the previous `latest.json`, write a timestamped snapshot, and refresh `latest`.

## What It Reads

- Codex `state_5.sqlite` through existing read-only adapter helpers.
- Codex memory files under `~/.codex/memories/`.
- Bourdon L5 manifests under `~/agent-library/agents/`.
- Optional `codex mcp get bourdon` output.

It does not read `~/.codex/auth.json`, mutate SQLite, write memory files, call
models, or access network services.

## Key Fields

- `derived.stage1_jobs_done`
- `derived.stage1_jobs_error`
- `derived.stage1_failure_ratio`
- `derived.stage1_error_classes`
- `derived.distilled_memory_items`
- `derived.fallback_memory_items`
- `derived.codex_l5_entity_count`
- `codex_mcp.installed`
- `trend.*_delta`
- `graph.nodes` and `graph.edges`

## Interpretation

Stage 1 failure is not a Bourdon failure. Treat it as a routing signal:

- High Stage 1 error rate: favor turn-compiled explicit pre-turn injection.
- Stale or empty Codex L5: run the relevant export/sync path.
- Missing MCP wiring: re-run the local MCP setup.
- Growing fallback items with flat native memory: keep the fallback/turn compiler
  path active and investigate native summarization separately.
