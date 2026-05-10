# AGENTS.md

## Cursor Cloud specific instructions

### Project overview

Bourdon is a Python 3.10+ cross-agent memory federation protocol. See `README.md` for full architecture (L0–L6 memory stack) and `CONTRIBUTING.md` for dev setup docs.

### Development environment

- **Virtual environment**: `.venv` at repo root. Activate with `source .venv/bin/activate`.
- **Install**: `pip install -e ".[dev,server,llama-cpp]"` installs all runtime + dev + optional extras.
- The `python3.12-venv` system package is required on Ubuntu (pre-installed in the Cloud Agent snapshot).

### Key commands

| Task | Command |
|---|---|
| Lint | `ruff check .` (94 pre-existing style warnings as of v0.3.0) |
| Type check | `mypy core/ adapters/ cli/` (25 pre-existing type issues) |
| Tests | `pytest tests/ -v` (384 tests, ~2s) |
| Orchestrator demo | `python -c "import asyncio; from core.orchestrator import Bourdon; asyncio.run(Bourdon().prepare('test', 'base'))"` |
| CLI | `bourdon --help` / `bourdon prepare-turn "prompt" --access-level team` |
| L6 MCP server | `python -m core.l6_server --transport stdio` (requires `fastmcp>=2.0`) |

### Non-obvious caveats

- `tests/test_llama_cpp_backend.py` requires the `httpx` package (installed via `.[llama-cpp]` extra). Without it, pytest collection fails with `ModuleNotFoundError`.
- The L6 MCP server and L2 retrieval client require `fastmcp>=2.0` (installed via `.[server]` extra). These are optional features; L0+L1 work standalone.
- There are no external services (databases, Docker, etc.) required. The entire test suite runs in-process.
- Pre-existing lint (ruff) and type-check (mypy) issues exist in the codebase; these are not regressions.
- The `web/` directory contains a static marketing site deployed to Cloudflare Workers — it is not part of the development workflow.
