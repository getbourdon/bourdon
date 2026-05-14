# Bourdon development workflow

## Branches

- Fork or branch from **`main`** unless you are patching a numbered release branch explicitly agreed with maintainers.
- Use clear prefixes so filters stay sane:
  - `feature/` — additive behavior or new surfaces
  - `fix/` — bug fixes / correctness (include subsystem if helpful, e.g. `fix/codex-sqlite-recognition`)
  - `docs/` — prose-only deltas

## Pull requests

- Aim for **one pull request per coherent change.** Mixing unrelated refactors + behavior + doc drive-bys slows review and reverts become expensive.
- If a change naturally splits (e.g. adapter core vs. README examples), ship two PRs and reference the precursor in the follow-up description.
- For hot fixes touching shipped adapters (**Codex**, **Cursor**, **Claude**, etc.), add or extend **narrow tests first** (`tests/test_*`) so regressions reproduce in CI — see existing patterns in **`tests/test_codex_adapter.py`**, **`tests/test_cli.py`**.

## Release discipline

Version bumps (`pyproject.toml`) and changelog-style notes accompany **release PRs**, not intermittent features. Tags (`v0.x.y`) are cut from maintainers once `main` is green.

After interrupting mid-release (as with v0.6.0 follow-ups), reconcile **tag**, **`main` tip**, and open PRs (`docs/v0.6-status-and-recovery.md`) before tagging again.

## Local verification before opening a PR

- `pytest` (or scoped `pytest tests/test_codex_adapter.py` when iterating adapters).
- `bourdon dogfood` when touching exports or federation wiring.
- `python scripts/mcp_smoke_test.py --assertions --isolate-federation-write-smoke --library-path "<tmp-agent-library>"`.
