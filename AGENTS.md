# AGENTS.md

Guidance for AI agents (Cursor Cloud, Codex Cloud, Claude Code background agents, etc.) working in this repo. Human contributors should start with `CONTRIBUTING.md`; this file is the agent-shaped equivalent.

## Project overview

Bourdon is a Python 3.10+ **recognition-first runtime + agent federation memory** stack (L0–L6; see `README.md`). See `CONTRIBUTING.md` for contributor setup. Participant authoring guide at `docs/AUTHORING_A_PARTICIPANT.md`; formal contract at `spec/PARTICIPANT_CONTRACT.md`. The end-to-end cross-agent demo lives at `docs/PROOF.md`.

## Development environment

- **Virtual environment**: `.venv` at repo root. Activate with `source .venv/bin/activate`.
- **Install**: `pip install -e ".[dev,server,llama-cpp]"` installs all runtime + dev + optional extras.
- The `python3.12-venv` system package is required on Ubuntu (pre-installed in the Cursor Cloud Agent snapshot).
- No external services (databases, Docker, Redis, etc.) required. The entire test suite runs in-process.

## Key commands

| Task | Command |
|---|---|
| Lint | `ruff check .` (pre-existing warnings; non-fatal in CI per `test.yml`) |
| Type check | `mypy core/ participants/ cli/` (pre-existing type issues) |
| Tests | `pytest tests/ -v` (503 tests, ~3s) |
| Orchestrator smoke | `cd core && python orchestrator.py` (used in CI) |
| CLI | `bourdon --help` / `bourdon prepare-turn "prompt" --access-level team` |
| L6 MCP server | `bourdon serve` (or `python -m core.l6_server --transport stdio`; both require `fastmcp>=2.0` from `.[server]`) |
| Federation smoke test | `bourdon dogfood` — plants marker in convention-file participants, exports all, queries L6, prints round-trip matrix |
| Doctor preflight | `python scripts/doctor.py --workspace-root "."` |
| MCP smoke (stdio) | `python scripts/mcp_smoke_test.py --assertions` · disposable write probe: `... --isolate-federation-write-smoke --library-path <tmp>` · full seeded checks omit isolate flag |
| Regression matrix | `python scripts/regression_matrix.py --workspace-root "."` |
| Short-index check | `python scripts/migrate_short_index.py --workspace-root "." --check && python scripts/validate_short_index.py --workspace-root "."` |

## Cross-agent test stack (added in v0.4.1+)

Three layers, smallest first:

1. **`tests/test_federation_roundtrip.py`** — synthetic-fixture contract tests. Plants each participant's native fixture, exports L5, verifies round-trip through `L6Store` with correct attribution and shared-entity aggregation. Runs in CI. This is the regression net for "participant changed its L5 shape and L6 silently broke."
2. **`bourdon dogfood`** — smoke test on real local stores. Catches schema drift that synthetic fixtures can't (Cursor SQLite, Codex session index, etc.). Run periodically or after any participant change.
3. **`docs/PROOF.md`** — the public acceptance walkthrough. Not automatable; requires two real MCP-aware agents on a machine. Layer 3 is gating for v0.5.0.

## CI workflows

Two GitHub Actions workflows run on PRs and pushes to `main`:

1. **`test.yml`** — 3×3 matrix (ubuntu/windows/macos × Python 3.10/3.11/3.12). Installs `.[dev,llama-cpp]`, runs `pytest tests/ -v`, then `cd core && python orchestrator.py` as a smoke test. Ruff lint runs but is non-fatal (`|| echo "::warning::"` pattern).
2. **`memory-cycle.yml`** — Windows-only. Installs `.[server]`, runs PowerShell bootstrap + short-index schema enforcement + regression matrix + full memory cycle. Uploads reports as artifacts.

## Non-obvious caveats

- **`docs/v0.6-status-and-recovery.md`** — use when reconciling interrupted release work (tags vs `main` vs open GitHub PRs).
- **`docs/development-workflow.md`** — canonical branch + PR expectations for this repo.
- **CI vs local test parity**: CI's `test.yml` installs `.[dev,llama-cpp]` but NOT `.[server]`. L6 server tests (in `test_l6_server.py`) skip in CI when `fastmcp` is absent but pass locally with the full install. Local runs a superset of CI.
- **`tests/test_llama_cpp_backend.py`** requires the `httpx` package (installed via `.[llama-cpp]` extra). Without it, pytest collection fails with `ModuleNotFoundError`.
- **Entry points**: Participants are registered via `[project.entry-points."bourdon.participants"]` in `pyproject.toml`. After installing new participant code, verify registration with: `python -c "from importlib.metadata import entry_points; print([ep.name for ep in entry_points(group='bourdon.participants')])"`.
- **PATH gotcha when wiring `bourdon serve` into MCP hosts** (Claude Desktop, Cursor, etc.): hosts launch MCP subprocesses with a minimal PATH. If `bourdon` lives in a venv, use the absolute path to the venv's `bourdon` binary in the host's MCP config. See `docs/integrations/claude-desktop.md` for a worked example.
- The `web/` directory contains a static Cloudflare Workers marketing site (bourdon.ai) — not part of the dev workflow.
- **Workflow**: ship via PR, not direct-to-main. Even though `main` is not branch-protected, the PR flow runs CI gates and produces release-changelog hygiene. See `skills/bourdon-participant-authoring/SKILL.md` for the full rationale.
- **Commit style**: conventional commits — `feat:`, `fix:`, `docs:`, `refactor:`, `test:`, `chore:`.
