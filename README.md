# Bourdon

**[bourdon.ai](https://bourdon.ai)** · Cross-agent memory federation for human-AI collaboration.

Current AI memory systems are **call-and-repeat** — discrete turns with nothing happening in between. Real human language is **concurrent** — listeners recognize, recall, and formulate *while* speakers are still speaking. Bourdon is the engineering translation of that concurrent structure into AI systems.

> "We used our minds to make minds that make our minds better."

Named for the *bourdon* — the deep continuous drone of a pipe organ, the foundational tone that holds every other voice in place. (The lineage continues from the original *basso continuo* metaphor — the Baroque bass accompaniment — chosen when the project was named **Continuo** before the 2026-05-05 rebrand. Same music-theory family, tighter metaphor.)

---

## Status: Pre-Alpha (v0.3.0, BSL 1.1)

Project renamed Continuo → Bourdon on 2026-05-05 (v0.1.0); relicensed MIT → Business Source License 1.1 on 2026-05-06 (v0.2.0). See [release notes](https://github.com/getbourdon/bourdon/releases) for migration. The version-by-version history below covers v0.0.1 through v0.0.7 in detail; for v0.0.8 and later see GitHub Releases.

### Earlier version history

- **v0.0.1** -- initial scaffold, Phase 1 orchestrator working standalone
- **v0.0.2** -- L5 JSON Schema + adapter contract, base adapter module, first external adapter stub (Claude Code, discovers memory sources), test suite (49 tests), CI workflow (Windows + Ubuntu + macOS x Python 3.10-3.12)
- **v0.0.3** -- Claude Code adapter full parsing: PROJECTS/*/OVERVIEW.md -> project entities, LOG/*.md -> sessions, auto-memory frontmatter -> entities, memory.jsonl knowledge graph -> entities. Entity dedupe across sources. Conservative visibility policy.
- **v0.0.4** -- L2 UltraRAG async integration. `core/l2.py` with `L2Config` (YAML + env-var overrides), `L2Client` Protocol, `FastMCPL2Client`, `query_l2()` that never blocks / never raises. Disabled by default; opt in via `core/l2_config.yaml` or `BOURDON_L2_ENABLED=true`. Optional extra: `pip install 'bourdon[ultrarag]'`.
- **v0.0.5** -- L6 MCP server. The federation layer. `core/l6_store.py` loads every `~/agent-library/agents/*.l5.yaml`, builds a cross-agent entity index, and exposes query primitives (`list_agents`, `find_entity`, `list_recent_work`, `get_cross_agent_summary`) with visibility filtering re-applied at query time. `core/l6_server.py` wraps the store in a `fastmcp` server exposing `agent-library://` resources + `query_agent_memory` / `list_recent_work` / `find_entity` / `get_cross_agent_summary` MCP tools. Launch via `python -m core.l6_server`. Optional extra: `pip install 'bourdon[server]'`. 33 new tests (149 total): store query semantics, private-entity filter, reload behavior, lazy-import guard, server construction.
- **v0.0.6** (this release) -- Codex adapter + atomic L5 write. `adapters/codex.py` reads `~/.codex/session_index.jsonl` newest-first, resolves each entry's rollout file to pull `session_meta.cwd`, emits `Session` rows. Dedupes `thread_name`s into topic-type `Entity` rows with `last_touched` preserved. Registered under `bourdon.adapters` entry point. New `core/l5_io.py` provides `write_l5()` / `write_l5_dict()` with tmp+rename atomic semantics so L6 file watchers never see half-written manifests. 39 new tests (188 total): session_index parsing, rollout resolution, timestamp normalization, dedupe, schema round-trip, Codex L5 round-tripped through `L6Store` end-to-end.
- **v0.0.7** -- Generic Codex memory pipeline + first-class CLI. `adapters/codex.py` now treats `~/.codex/memories/*` as the primary distilled source, enriches with rollout chronology and structured `apply_patch` file evidence, and defaults Codex-derived entities/sessions to `team` visibility. New `bourdon codex export`, `bourdon codex build-context`, and `bourdon codex eval` commands turn that normalized model into L5 federation output plus Codex-oriented L0/L1 timing artifacts. `core/l6_store.py` and `core/l6_server.py` now support `access_level=public|team|private` while preserving `include_private` compatibility. **Plus `agent.role_narrative`** -- new optional L5 schema field that differentiates agents sharing the same `type` slug (Claude Code = manager; Codex = lead author; Cursor = debugger; Cline = throwaway; Clyde = general-purpose). Inspired by [Intrinsic Memory Agents](https://hf.co/papers/2508.08997). Both shipping adapters populate it; Clyde publisher does too. **Plus temporal validity windows** (`valid_from` / `valid_to` ISO 8601 dates on Entities, Zep-Graphiti-inspired) so federation queries can answer "what was active in Q1 2026?" not just "what's in memory?". **Plus `bourdon claude-code export`** subcommand designed for SessionEnd hook use -- writes the Claude Code L5 manifest to `~/agent-library/agents/claude-code.l5.yaml` silently, never raises, exits 0 in all failure modes. Wire it into `~/.claude/settings.json`:

```json
{
  "hooks": {
    "SessionEnd": [
      { "command": "bourdon claude-code export" }
    ]
  }
}
```

**Plus `spec/POSITIONING.md`** stakes the recognition-first thesis publicly, and **`spec/RELATED_WORK.md`** maps Bourdon's vocabulary to the wider field (Mem0, Zep, Letta, Cognee, Memora, SCS, Intrinsic Memory Agents, G-Memory, H-MEM, MCP roadmap). **And `core/recognition_runtime.py`** ships the first concrete implementation of the recognition-first runtime: synchronous template-based recognition string + concurrent L1 hydration awaitable, ≤3s timeout budget, never raises. This is the headline behavior the FINDINGS_JOURNAL flagged on 2026-04-19.

**Not ready for production use.** Built in the open as a spec-and-reference-implementation for a convention we hope the ecosystem adopts.

## What It Is

A tiered, timing-aware memory protocol for any human-AI collaboration where context matters over time:

- **Developer workflows** — memory across Claude Code, Codex, Cursor, Copilot
- **Customer support operations** — cross-tool customer intelligence
- **Scientific research** — lab notebook continuity across sessions and team members
- **Creative writing, architecture, project management, education** — and anywhere else context accrues

One architecture, many domains. Content is always domain-specific; cognition is universal.

## The Memory Stack

```
Per-agent personal memory:
  L0 — Hot Cache          always in system prompt, ~3K tokens
  L1 — Entity Synopses    triggered on L0 keyword hit, parallel loaded
  L2 — Episodic Memory    async retrieval during human response time
  L3 — Indexed History    on-demand searchable session logs
  L4 — Raw Archive        verbatim conversation history

Cross-agent federation:
  L5 — Agent Memory Manifest    per-agent public glossary (a projection of L0-L4)
  L6 — Federation Library       aggregates all L5s, exposed as MCP server
```

See [`spec/ARCHITECTURE_v0.1.md`](spec/ARCHITECTURE_v0.1.md) for the full architecture doc.

## Quick Start (Phase 1 Orchestrator)

```bash
# From a local clone:
cd core/
python -c "
import asyncio
from orchestrator import Bourdon

async def main():
    memory = Bourdon()
    base = 'You are a helpful AI assistant.'
    prompt = await memory.prepare('Let us work on Bourdon today', base)
    print(prompt)

asyncio.run(main())
"
```

This loads the L0 hot cache and any matching L1 synopses, then prints the fully-assembled system prompt ready to pass to an Ollama / OpenAI / Claude API call.

## Quick Start (Codex CLI)

```bash
bourdon codex export --access-level team
bourdon codex build-context --out-dir ./build/codex-context
bourdon codex prepare-turn --memory-md "Can we keep working on Bourdon?"
bourdon codex eval --fixtures
```

This generic Codex path is designed for org-wide distribution: local Codex memories stay `team` by default, public federation requires explicit promotion, and generated L0/L1 artifacts live separately from the repo's static Clyde examples.

## Quick Start (Cross-Agent Recognition)

```bash
bourdon prepare-turn "Can we keep working on Bourdon?" --access-level team
bourdon deeper-context "Can we keep working on Bourdon?" --access-level team
python -m core.l6_server --transport stdio
```

`prepare-turn` reads the L6 federation library and returns immediate recognition
plus a bounded prompt fragment. `deeper-context` is the companion L2 retrieval
surface; it returns empty context when L2 is disabled.

## Quick Start (Hybrid Memory Cycle)

```powershell
powershell -ExecutionPolicy Bypass -File scripts/bootstrap-bourdon-mcp.ps1 -WorkspaceRoot "."
powershell -ExecutionPolicy Bypass -File scripts/run_memory_cycle.ps1 -WorkspaceRoot "." -SchemaPath ".\spec\L5_schema.json"
```

What this does:

- Builds and validates hybrid memory indices.
- Exports L5 manifests to workspace + `~/agent-library/agents/`.
- Runs MCP smoke assertions against the L6 server.
- Writes machine-readable reports:
  - `.cursor/memory/reports/mcp-smoke-report.json`
  - `.cursor/memory/reports/memory-cycle-report.json`

Docs:

- [`docs/getting-started-memory-cycle.md`](docs/getting-started-memory-cycle.md)
- [`docs/good-first-issues.md`](docs/good-first-issues.md)
- [`docs/agent-integration-status.md`](docs/agent-integration-status.md)

## Hybrid Memory Tooling

Helper scripts:

- `scripts/bootstrap-bourdon-mcp.ps1`
- `scripts/doctor.ps1`
- `scripts/migrate_short_index.py`
- `scripts/validate_short_index.py`
- `scripts/build_bourdon_l5.py`
- `scripts/mcp_smoke_test.py`
- `scripts/regression_matrix.ps1`
- `scripts/run_memory_cycle.ps1`

CI guardrails:

- `python scripts/migrate_short_index.py --workspace-root "." --check`
- `python scripts/validate_short_index.py --workspace-root "."`
- `powershell -ExecutionPolicy Bypass -File scripts/regression_matrix.ps1 -WorkspaceRoot "."`

If CI fails on migration `--check`, run local migration and commit normalized files:

```powershell
python scripts/migrate_short_index.py --workspace-root "."
python scripts/validate_short_index.py --workspace-root "."
```

Run one-command preflight before full cycle:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/doctor.ps1 -WorkspaceRoot "." -InstallMissingDeps -RunRegressionMatrix
```

## Roadmap

- **v0.0.1** (now) — Scaffold + Phase 1 orchestrator (L0 + L1, manual files, Ollama-compatible)
- **v0.1.0** — L2 UltraRAG async integration + session-close L5 export
- **v0.2.0** — Relicense MIT → BSL 1.1
- **v0.3.0** — Codex operational layer: memory doctor + fallback recognition + L6 prep
- **v0.4.0** — `pip install bourdon` + `bourdon init/up/query` CLI; Clyde + Clair native L5 publishers
- **v1.0.0** — Docs site, community adapter contributions, public launch
- **v1.x** — Cursor adapter (SQLite reverse-engineering), Copilot adapter (partial coverage), framework adapters (LangChain, CrewAI, AutoGen)

## Adapter Compatibility

| Agent          | Difficulty        | Status    |
|----------------|-------------------|-----------|
| Clyde          | Native            | Planned   |
| Clair          | Native            | Planned   |
| Claude Code    | Native + Adapter  | Export hook available |
| Codex          | Moderate          | Fallback + prepare-turn available |
| Cursor         | SQLite            | Adapter available; `bourdon cursor export` |
| Cline          | Unknown           | Blocked pending native store path/schema |
| Copilot        | Deferred v1.x     | Encrypted reasoning, no session index |

## Philosophy

See [`spec/THESIS.md`](spec/THESIS.md) (canonical copy lives in the project's [claude-brain repo](https://github.com/ryandavispro1-cmyk/claude-brain)) for the founding argument.

See [`spec/USE_CASES.md`](spec/USE_CASES.md) for eight worked domain scenarios beyond developer workflows.

## Contributing

Bourdon is source-available under the Business Source License 1.1 (auto-converts to Apache 2.0 after four years per version). Free for solo developers, internal/non-competing commercial use, research, and education. Commercial license required for hosted-service offerings that compete with RADLAB LLC's paid versions. See [`LICENSE`](LICENSE) for the legal text and [`LICENSE_FAQ.md`](LICENSE_FAQ.md) for plain-English guidance. Contributions welcome — see [`CONTRIBUTING.md`](CONTRIBUTING.md).

## About

Bourdon is an open-source memory protocol and reference implementation seeded by [RADLAB LLC](https://bourdon.ai). Designed with Ryan Davis (RADMAN), with major research and implementation contributions from Claude and Codex.

## Contributors

- Ryan Davis -- creator, thesis, architecture, implementation direction
- Claude -- thesis drafting, architecture planning, early implementation
- Codex -- Codex adapter expansion, CLI implementation, timing-artifact generation, access-level model
- OpenAI Codex 5.3 -- hybrid memory cycle tooling, MCP smoke assertions, CI/report automation, starter template packaging

## Other RADLAB Projects

Because you found us here, you might like to check out:

- **ILTT** — AI fitness automation for personal trainers ([iltt.app](https://iltt.app))
- **PRUN** — Privacy-first encrypted password manager ([prunpassword.com](https://prunpassword.com))
- **Castmore** — Cross-platform streaming discovery
- **OMNIVour** — Universal file conversion with AI extras

## License

Business Source License 1.1, auto-converts to Apache License 2.0 four years after each version is published. See [`LICENSE`](LICENSE) for the full text and [`LICENSE_FAQ.md`](LICENSE_FAQ.md) for guidance on what's permitted. Commercial licensing inquiries: licensing@bourdon.ai.

Versions v0.0.1 through v0.1.0 were published under MIT and remain MIT in their distributed form. Relicensing to BSL 1.1 applies from v0.2.0 onward.
