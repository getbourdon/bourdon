# AGENTS.md

## Cursor Cloud specific instructions

### Project overview

Bourdon is a Python 3.10+ cross-agent memory federation protocol (v0.8.0, pre-alpha). See `README.md` for the full L0–L6 memory stack architecture and `CONTRIBUTING.md` for contributor setup. Adapter authoring guide at `docs/AUTHORING_AN_ADAPTER.md`; formal contract at `spec/ADAPTER_CONTRACT.md`.

### Development environment

- **Virtual environment**: `.venv` at repo root. Activate with `source .venv/bin/activate`.
- **Install**: `pip install -e ".[dev,server,llama-cpp,federation]"` installs all runtime + dev + optional extras.
- The `python3.12-venv` system package is required on Ubuntu (pre-installed in the Cloud Agent snapshot).
- No external services (databases, Docker, Redis, etc.) required. The entire test suite runs in-process.

### Key commands

| Task | Command |
|---|---|
| Lint | `ruff check .` (pre-existing warnings; non-fatal in CI per `test.yml`) |
| Type check | `mypy core/ adapters/ cli/` (pre-existing type issues) |
| Tests | `pytest tests/ -v` (763+ tests, ~5s) |
| Orchestrator smoke | `cd core && python orchestrator.py` (used in CI) |
| CLI | `bourdon --help` |
| Cursor export | `bourdon cursor export --print` |
| All adapters export | `bourdon export-all` |
| Health check | `bourdon doctor` |
| Federation dogfood | `bourdon dogfood` |
| Setup wizard | `bourdon setup --non-interactive --dry-run` |
| Demo | `bourdon demo` |
| L6 MCP server | `python -m core.l6_server --transport stdio` |
| Cross-machine sync | `bourdon sync push <dest>` / `bourdon sync pull <src>` |
| Doctor preflight | `python scripts/doctor.py --workspace-root "."` |
| Regression matrix | `python scripts/regression_matrix.py --workspace-root "."` |
| Short-index check | `python scripts/migrate_short_index.py --workspace-root "." --check && python scripts/validate_short_index.py --workspace-root "."` |

### Registered adapters (v0.8.0)

| Adapter | Agent ID | CLI subcommands |
|---|---|---|
| Claude Code | `claude-code` | `export` |
| Claude Code Automations | `claude-code-automations` | `export`, `doctor`, `ingest-github` |
| Codex | `codex` | `export`, `build-context`, `doctor`, `sync-native`, `recognize`, `prepare-turn`, `compile-turn`, `eval` |
| Codex Automations | `codex-automations` | `export`, `doctor` |
| Cursor | `cursor` | `export` |
| Copilot | `copilot` | `export`, `doctor`, `init` |
| Cascade | `cascade` | `export`, `doctor`, `init` |

### CI workflows

Two GitHub Actions workflows run on PRs and pushes to `main`:

1. **`test.yml`** — 3x3 matrix (ubuntu/windows/macos x Python 3.10/3.11/3.12). Installs `.[dev,llama-cpp]`, runs `pytest tests/ -v`, then `cd core && python orchestrator.py` as smoke test. Ruff lint is non-fatal.
2. **`memory-cycle.yml`** — Windows-only. Installs `.[server]`, runs PowerShell bootstrap + short-index schema enforcement + regression matrix + full memory cycle.

### Non-obvious caveats

- **CI vs local test parity**: CI's `test.yml` installs `.[dev,llama-cpp]` but NOT `.[server]` or `.[federation]`. L6 server tests and federation tests skip in CI when `fastmcp` is absent but pass locally. The local environment runs a superset of CI tests.
- **`tomli` dependency**: v0.8.0 added `tomli>=2.0` for Python 3.10 (automation adapters parse TOML). Python 3.11+ uses stdlib `tomllib`.
- **Entry points**: Adapters are registered via `[project.entry-points."bourdon.adapters"]` in `pyproject.toml`. Verify: `python -c "from importlib.metadata import entry_points; print([ep.name for ep in entry_points(group='bourdon.adapters')])"`.
- **Automation adapters** (`claude-code-automations`, `codex-automations`) are separate agent IDs from their interactive counterparts. They read `~/<agent-home>/automations/<id>/{automation.toml, memory.md}`.
- **Cross-machine sync** (`bourdon sync push/pull`) uses `rsync` subprocess. Requires `rsync` on PATH.
- **Peer L6 federation** (`python -m core.l6_server --peer URL`) requires the `[federation]` extra.
- The `web/` directory is a Cloudflare Workers marketing site — not part of the dev workflow.
- Commit style is conventional commits: `feat:`, `fix:`, `docs:`, `refactor:`, `test:`, `chore:`.
- 1 pre-existing test failure in `test_codex_turn_compiler.py` (confidence level assertion, not a regression).
