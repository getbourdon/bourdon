# Contributing to Bourdon

Bourdon is source-available under the Business Source License 1.1 (BSL) and in pre-alpha. Free for solo developers, internal/non-competing commercial use, research, and education; auto-converts to Apache 2.0 four years after each version is published. See [`LICENSE_FAQ.md`](LICENSE_FAQ.md) for plain-English guidance. Contributions welcome — especially participants for agents and frameworks not yet covered.

## Current Status

v0.0.1 scaffold. Phase 1 reference orchestrator (L0 + L1) is working standalone. Federation layer (L5 + L6) and participant plugin system are under active design.

**If you want to build on this today, expect sharp edges.** The spec is likely to change between v0.0.x and v0.1.x as we learn from the reference implementation.

## Where to Start

- **Read the thesis.** [`spec/THESIS.md`](spec/THESIS.md) explains *why* the architecture looks the way it does. If you disagree with the thesis, your contribution may end up pulling in the wrong direction.
- **Read the architecture.** [`spec/ARCHITECTURE_v0.1.md`](spec/ARCHITECTURE_v0.1.md) is the full layer-by-layer description.
- **See use cases.** [`spec/USE_CASES.md`](spec/USE_CASES.md) shows the intended breadth — dev tools are the proving ground, but the architecture targets universal knowledge work.
- **Check the contributor list.** [`CONTRIBUTORS.md`](CONTRIBUTORS.md) lists maintainers and AI co-implementors with their branch lanes (`cursor/*`, `claude/*`, `codex/*`). If you're submitting agent-assisted work, follow the agent-as-author convention documented there.

## Types of Contributions

### Participants (most wanted)
Bourdon's value scales linearly with the number of agents/tools it can federate. A participant reads a native agent's memory store and emits a normalized L5 manifest. See [`participants/`](participants/) for examples (once implemented) and the participant contract spec in `spec/PARTICIPANT_CONTRACT.md` (coming in v0.1.0).

Specifically wanted:
- Cursor participant (SQLite schema reverse-engineering) — v0 in progress at [`ryandavispro1-cmyk/cursor-spot`](https://github.com/ryandavispro1-cmyk/cursor-spot); will upstream once stable.
- Copilot participant (accepting the encrypted-reasoning limitation)
- LangChain / CrewAI / AutoGen memory exporters
- Non-agent data sources: Linear, Attio, Notion, Google Calendar, Slack, email (IMAP/MBOX), Obsidian

### Core improvements
- L0 keyword detection is currently naive string matching. Fuzzy matching + alias tables would be a straight win.
- L1 token budget enforcement currently drops entities in iteration order. LRU or relevance-weighted eviction would be better.
- Async timing model testing — we need property tests that verify L2 never blocks first-response generation.

### Documentation
- Worked tutorials per domain (one of the USE_CASES scenarios as an end-to-end walkthrough)
- Participant authoring guide
- Architecture diagram improvements

### Research
- How do we measure "recognition vs. lookup" feel? The subjective 8+/10 metric is a start — we need better eval methodology.
- What happens to memory at scale (year-active agent with 1000+ entities)? Rollup strategies need research.

## Development Setup

```bash
git clone https://github.com/getbourdon/bourdon
cd bourdon

# Python 3.10+ required
python -m venv .venv
source .venv/bin/activate   # macOS/Linux
.venv\Scripts\activate      # Windows

pip install -e .[dev]
```

## Hybrid Memory Cycle Workflow

For contributors working on L5/L6 federation and retrieval ergonomics, use the memory-cycle scripts:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/bootstrap-bourdon-mcp.ps1 -WorkspaceRoot "."
powershell -ExecutionPolicy Bypass -File scripts/run_memory_cycle.ps1 -WorkspaceRoot "." -SchemaPath ".\spec\L5_schema.json"
```

This run validates:

- short-index merge and workspace-over-global precedence
- L5 export correctness
- MCP tool availability + smoke assertions
- machine-readable report output under `.cursor/memory/reports/`

Running the smoke test:
```bash
cd core/
python orchestrator.py
```

## Filing Issues

Before filing:
- Check if it's already captured in `spec/ROADMAP.md` (coming soon) or existing issues
- If it's a participant request, consider building it — participants are the easiest contribution path
- If it's a philosophy/scope question, `spec/THESIS.md` is the canonical reference

## Commit Style

Conventional commits preferred: `feat:`, `fix:`, `docs:`, `refactor:`, `test:`, `chore:`.

## Code of Conduct

Be excellent to each other. If something needs formalizing later we will, but for now: assume good faith, disagree productively, don't be a jerk.

## License

All contributions are licensed under the Business Source License 1.1, with copyright assigned to RADLAB LLC. By submitting a PR you agree to license your contribution accordingly. This allows RADLAB LLC to maintain unified copyright over the codebase, which is required for offering commercial licenses to organizations whose use exceeds the BSL Additional Use Grant.
