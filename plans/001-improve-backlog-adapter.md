# Plan 001: Federate improve-style plan backlogs via `bourdon improve sync`

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving to the
> next step. If anything in the "STOP conditions" section occurs, stop and
> report — do not improvise. When done, update the status row for this plan
> in `plans/README.md`.
>
> **Drift check (run first)**:
> `git diff --stat ab49132..HEAD -- cli/main.py core/l6_store.py`
> If either file changed since this plan was written, compare the
> "Current state" excerpts below against the live code before proceeding;
> on a mismatch, treat it as a STOP condition.

## Status

- **Priority**: P1
- **Effort**: M
- **Risk**: LOW
- **Depends on**: none
- **Category**: direction
- **Planned at**: commit `ab49132`, 2026-06-10

## Why this matters

shadcn/improve (github.com/shadcn/improve, MIT) writes self-contained
implementation plans into a repo's `plans/` directory with a status index
(`plans/README.md`). Those backlogs are repo-scoped — only the agent sitting
in that repo can see them. Bourdon's thesis is that fleet state belongs in
the federation. This adapter reads any repo's improve-format backlog and
commits it upward: one entity per plan, one rollup entity per repo, one
session per sync run. Result: every federated agent can answer "what's
executable right now in repo X" without opening repo X. It is also the first
write-path exerciser that goes in-process through `L6Store.commit_l5` rather
than the MCP transport (which currently times out on long writes), giving us
a diagnostic wedge on the v0.9.1 write issue.

## Current state

- `core/l6_store.py:1159` — `L6Store.commit_l5(agent_id, *, agent_type=None,
  instance=None, role_narrative=None, entities=None, sessions=None,
  mode="merge")`. Entities require a non-empty `name`; sessions require an
  ISO `date`. Merge mode dedupes entities by `name.lower()` and sessions by
  `(date, cwd)`; list fields union, scalar fields overwrite (scalar/list
  coercion fixed in #135). Returns a write-summary dict.
- `cli/main.py:2515` — `subparsers = parser.add_subparsers(dest="command")`.
  Agent command groups follow the nested pattern at `cli/main.py:2549-2632`
  (the `cursor` group: `export`, `doctor`, `compile-turn`, `sync-native`,
  `init`). Store handles are constructed as
  `store = L6Store(Path(args.library))` — see `cli/main.py:189` and `:310`.
- `participants/` holds agent-native scrapers (`cascade.py`, `codex.py`, …)
  built on `participants/base.py`. This adapter is repo-scoped, not
  agent-scoped, so it does NOT use the participant base class (see
  Maintenance notes for the promotion path).
- No `plans/` parsing exists anywhere in the repo today.
- Input format (vendored from the improve plan spec — inlined because the
  executor may not have it):
  - `plans/README.md` contains a markdown table under
    `## Execution order & status` with columns
    `| Plan | Title | Priority | Effort | Depends on | Status |`.
    Status ∈ TODO | IN PROGRESS | DONE | BLOCKED (reason) |
    REJECTED (rationale) — parenthetical reasons must be captured.
    An optional `## Findings considered and rejected` section holds bullets.
  - Each `plans/NNN-slug.md` has a `## Status` block of bold bullets:
    Priority, Effort, Risk, Depends on, Category, and
    Planned at (`commit \`sha\`, YYYY-MM-DD`).

## Commands you will need

| Purpose      | Command                                                        | Expected on success        |
|--------------|----------------------------------------------------------------|----------------------------|
| Tests (new)  | `python -m pytest tests/test_improve_backlog.py -q`            | all pass                   |
| Tests (full) | `python -m pytest -q`                                          | 985+ pass, 0 fail          |
| Lint         | `ruff check core/improve_backlog.py cli/main.py tests/test_improve_backlog.py` | exit 0    |
| Manual check | `bourdon improve sync . --dry-run`                             | backlog printed, no write  |

Run from repo root. The repo uses pytest>=8 and ruff (`pyproject.toml`).
If the `bourdon` entry point is missing in your environment, run
`pip install -e .` once first.

## Scope

**In scope** (the only files you should modify):
- `core/improve_backlog.py` (create) — parser, entity/session builders, sync
- `cli/main.py` — wiring only: `improve` subparser group + `sync` handler
- `tests/test_improve_backlog.py` (create)
- `plans/README.md` — status row update on completion

**Out of scope** (do NOT touch, even though they look related):
- `participants/*` — different abstraction (agent-native session scrapers)
- `core/l6_store.py` — consume `commit_l5` as-is; needing to change it is a
  STOP condition, not an invitation
- MCP server / transport code — this adapter is deliberately in-process
- `spec/L5_schema.json`

## Git workflow

- Branch: `feat/improve-backlog-adapter`
- Conventional commits with scope, matching repo style from `git log`
  (e.g. `fix(store): coerce scalar-typed list fields in commit_l5 merge`).
  Use scope `improve`, e.g. `feat(improve): parse plans index status table`.
- Do NOT push or open a PR unless the operator instructs it.

## Steps

### Step 1: Resolve the entity `type` question from the schema

Read `spec/L5_schema.json`. If entity objects define a `type` enum, use a
valid value for plan entities; if entity `type` is free-form or absent,
omit `type` entirely and rely on `tags` for classification. Do not guess.

**Verify**: quote the relevant schema lines in your report.

### Step 2: `core/improve_backlog.py` — parser

Implement, with dataclasses for rows, pure functions, no writes:
- `parse_index(path) -> BacklogIndex` — the status table (tolerant: extra
  columns allowed, map columns by header name not position; normalize status
  casefold; capture parenthetical BLOCKED/REJECTED reasons) plus the
  rejected-findings bullets.
- `parse_plan_status(path) -> PlanMeta` — the `## Status` bullet block;
  missing fields become None, never raise on partial blocks.

**Verify**: `python -c "from core.improve_backlog import parse_index, parse_plan_status"` → exit 0.

### Step 3: builders + sync

- `build_entities(repo_name, index, plan_metas) -> list[dict]`: per plan,
  `name="plan:<repo>/<NNN-slug>"` (this exact shape — it is the merge/dedupe
  key), `summary="<Title> — <STATUS>; P<n>, effort <S/M/L>, risk <…>;
  depends on <…>; planned at <sha> <date>[; reason: <…>]"`,
  `tags=["improve-plan", <repo>, <category>, <status-lower>]`,
  `visibility="public"`. Plus one rollup entity
  `name="improve-backlog:<repo>"` whose summary gives counts by status and
  names the next executable plan (lowest-numbered TODO whose deps are DONE).
- `sync(repo_path, library_path, agent_id="improve", agent_type="other",
  dry_run=False) -> dict`: parse → build → one session
  `{date: <today ISO>, cwd: str(repo_path),
  project_focus: [<repo>, "improve-backlog"],
  key_actions: ["synced N plans: X done, Y todo, Z blocked, W rejected"]}`
  → `L6Store(library_path).commit_l5(...)`. `dry_run=True` prints what would
  be committed and performs zero writes.

**Verify**: `python -c "from core.improve_backlog import sync"` → exit 0.

### Step 4: tests

`tests/test_improve_backlog.py`, modeled structurally on
`tests/test_cascade_participant.py`, using `tmp_path` fixtures. Minimum
cases: well-formed index parses; BLOCKED-with-reason captured; extra column
tolerated; index row whose plan file is missing (no crash, meta=None);
rejected-findings bullets parsed; entity names stable across two syncs
(merge dedupe depends on it); `dry_run` performs no write (library dir
untouched); real sync round-trips — manifest readable back via `L6Store`.

**Verify**: `python -m pytest tests/test_improve_backlog.py -q` → ≥8 pass.
Then `python -m pytest -q` → full suite green, no regressions.

### Step 5: CLI wiring

In `cli/main.py`, add an `improve` subparser group with a `sync` subcommand,
mirroring the `cursor` group at `cli/main.py:2549`: positional `path`
(default `.`), `--library` (same default/help as `cursor export` uses),
`--agent-id` (default `improve`), `--dry-run` flag. Handler derives repo
name from the resolved `path` basename and calls `sync`, printing the
returned write summary.

**Verify**: `bourdon improve sync --help` → exit 0, shows all four options.

### Step 6: End-to-end on this repo

1. `bourdon improve sync . --dry-run` → prints this very backlog
   (plan 001 row + rollup), zero writes.
2. `bourdon improve sync .` → write summary reports entities added.
3. Confirm: read `~/agent-library/agents/improve.l5.yaml` and verify
   `plan:bourdon/001-improve-backlog-adapter` is present with the status
   currently shown in `plans/README.md`, plus `improve-backlog:bourdon`.

**Verify**: the three outputs above, quoted in your report.

## Done criteria

Machine-checkable. ALL must hold:

- [ ] `python -m pytest -q` exits 0 (full suite, no regressions)
- [ ] `python -m pytest tests/test_improve_backlog.py -q` — ≥8 tests, all pass
- [ ] `ruff check core/improve_backlog.py cli/main.py tests/test_improve_backlog.py` exits 0
- [ ] `bourdon improve sync . --dry-run` prints the backlog and writes nothing
- [ ] Real sync produces `improve.l5.yaml` containing both
      `plan:bourdon/001-improve-backlog-adapter` and `improve-backlog:bourdon`
- [ ] No files outside the in-scope list are modified (`git status`)
- [ ] `plans/README.md` status row for 001 updated

## STOP conditions

Stop and report back (do not improvise) if:

- The drift check shows `cli/main.py` or `core/l6_store.py` changed since
  `ab49132` and the "Current state" excerpts no longer match the live code.
- `commit_l5` raises schema/validation errors on your entities after Step 1's
  type resolution — report the error verbatim; do not patch `core/l6_store.py`
  or `spec/L5_schema.json`.
- The `cursor` subparser pattern at `cli/main.py:2549` does not match the
  excerpt (the file is 2,500+ lines and actively evolving).
- The full test suite has failures BEFORE your changes — record the baseline
  first; pre-existing failures are a STOP, not yours to fix.

## Maintenance notes

- Promotion path: if improve backlogs later need watch-mode or autodiscovery
  (cf. branch `feat/participant-autodiscovery`), refactor into a
  `participants/base.py` subclass; the parser functions stay reusable as-is.
- If shadcn/improve changes its index columns, only `parse_index` should need
  touching — that is why column mapping is by header name, not position.
- Reviewer focus: entity `name` stability (it IS the federation merge key),
  and that `--dry-run` is provably write-free.
- Attribution: any user-facing docs or release notes for `bourdon improve
  sync` must credit the plan format it consumes — shadcn/improve
  (github.com/shadcn/improve, MIT). We complement that project; we do not
  fork or replicate it.
- Deferred (out of this plan, recorded for reconcile): publishing federation
  status back into the repo; peer-fanout reads already exist via
  `get_cross_agent_summary`.
