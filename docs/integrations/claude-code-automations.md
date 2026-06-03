# Bourdon × Claude Code automations

The `claude-code` adapter only sees what an **interactive** Claude Code session writes — `claude-brain/`, `~/.claude/projects/<workspace>/memory/`, the MCP knowledge graph. Automations escape that path: GitHub Action runs of `claude-code-action`, CronCreate-fired prompts, `/loop` wake-ups, and `/schedule`-driven remote routines never touch any of those three stores. Result: the federation graph thinks Claude Code only worked when a human typed at the terminal.

The `claude-code-automations` adapter closes that gap. It reads a parallel local convention — `~/.claude/automations/<id>/{automation.toml, memory.md}` — that automation entry points write to.

## The convention

```
~/.claude/automations/
└── weekly-pr-digest/
    ├── automation.toml        # one-time config: id, name, schedule, kind, cwds
    └── memory.md              # append-only dated run log
```

`automation.toml`:

```toml
version = 1
id = "weekly-pr-digest"
name = "Weekly PR Digest"
status = "ACTIVE"            # ACTIVE | PAUSED | RETIRED
kind = "loop"                # loop | cron | github-action | schedule
rrule = "FREQ=WEEKLY;BYDAY=MO"
cwds = ["/Users/radman/claudework"]
```

`memory.md`:

```
2026-06-03
- Ran weekly PR audit across RADLAB repos.
- 3 PRs awaiting review, none blocked.

2026-05-27
- Ran weekly PR audit.
- ShipStable PR #213 surfaced two CI gaps; filed punchlist.
```

The adapter parses one **Run** per dated header. Bullets become the run's `key_actions`. Project hints in the bullets (ShipStable, ILTT, Bourdon, …) become known entities, and signal patterns (release, billing, ci-failure, …) become `automation-signal` entities so the federation can answer *"what kinds of work did Claude Code's automations do this month?"*

## Writing entries

Use the bundled helper to avoid hand-formatting:

```sh
~/bourdon/scripts/automation-memory-append.sh weekly-pr-digest \
  --name "Weekly PR Digest" \
  --rrule "FREQ=WEEKLY;BYDAY=MO" \
  --kind loop \
  --cwd /Users/radman/claudework \
  "Ran weekly PR audit. 3 PRs awaiting review."
```

- First call creates `automation.toml` from the flags (defaults: `status=ACTIVE`, `kind=claude-code-automation`, `cwd=$(pwd)`).
- Subsequent calls only need `<id>` and the summary line — flags are ignored once the toml exists.
- Bullets dated today are merged under today's section; a new dated section is opened on the next UTC day.

Symlink it onto your `PATH` if you call it from many places:

```sh
ln -s ~/bourdon/scripts/automation-memory-append.sh ~/.claude/hooks/automation-memory-append.sh
```

## Where to call it from

| Surface | Where to add the call |
|---|---|
| **`/loop` continuations** | At the end of each iteration's prompt, e.g. `"...When finished, run: ~/.claude/hooks/automation-memory-append.sh my-loop-id 'summary of what changed'"` |
| **CronCreate jobs** | Same — append the call to the scheduled prompt body. |
| **`claude-code-action` (GitHub Action)** | Add a post-step that uploads `~/.claude/automations/` as an artifact and rsyncs it back to your local box. (See Path B in the integration plan.) |
| **`/schedule` remote routines** | Once the routine finishes, have it append a one-line summary that the local sync pulls down. (See Path C.) |
| **MCP-initiated turns** (Vercel Agent, IFTTT, Linear → Claude) | The caller writes the summary; Claude's prompt body invokes the helper. |

## Publishing the manifest

```sh
bourdon claude-code-automations export
```

Writes `~/agent-library/agents/claude-code-automations.l5.yaml`, the sibling manifest the federation graph reads.

Add it next to the existing `claude-code export` in `~/.claude/settings.json`:

```json
{
  "hooks": {
    "SessionEnd": [
      { "type": "command", "command": "/Users/radman/bourdon/.venv/bin/bourdon claude-code export" },
      { "type": "command", "command": "/Users/radman/bourdon/.venv/bin/bourdon claude-code-automations export" }
    ]
  }
}
```

Also runnable from cron — there's no session dependency.

## GitHub Actions (claude-code-action runs)

Runs of `claude-code-action` in CI happen on an ephemeral runner — they never touch your local `~/.claude/automations/`. Bridge the gap in two steps:

### 1. Emit from the workflow

Drop the template at [`examples/github-actions/claude-code-bourdon-federation.yml`](../../examples/github-actions/claude-code-bourdon-federation.yml) into your repo's `.github/workflows/`. Three things happen per run:

- The workflow runs `claude-code-action` (your existing step).
- A `record run` step calls `automation-memory-append.sh` once per non-blank summary line, appending dated bullets to `~/.claude/automations/<workflow-name>/memory.md` on the runner.
- The runner's `~/.claude/automations/` directory is uploaded as a workflow artifact named `claude-code-automations`.

Tune the `SUMMARY` env var to interpolate whatever signals your run produces — step outputs, PR number, run URL, exit codes. Each non-blank line becomes one bullet.

### 2. Ingest on your local box

```sh
bourdon claude-code-automations ingest-github \
    --repo your-org/your-repo \
    --run <run-id> \
    --artifact-name claude-code-automations
bourdon claude-code-automations export
```

Or, if you already have the artifact zip downloaded:

```sh
bourdon claude-code-automations ingest-github --artifact-zip path/to/artifact.zip
```

Ingestion is **idempotent**: re-running on the same artifact is a no-op. Bullets dedupe per-date by exact string match, and existing automations gain new bullets without disturbing what's already there.

You can schedule the ingest as a launchd / cron job. A daily pull is usually enough; tune to how chatty your `claude-code-action` workflows are.

### `gh` CLI requirement

`ingest-github` shells out to the `gh` CLI in repo-mode (`--repo + --run`). Install it from [cli.github.com](https://cli.github.com) and run `gh auth login` once. The `--source` and `--artifact-zip` modes don't require `gh`.

## Diagnostics

```sh
bourdon claude-code-automations doctor
```

States:
- **blocked** — `~/.claude/automations/` does not exist. Run the helper once to create it.
- **degraded** — directory exists but no `automation.toml` files were found.
- **ok** — at least one automation found. The report includes counts of automations, memory files, runs extracted, and active automations.

## Privacy

- Default visibility is `TEAM` (same as `codex-automations`).
- The `private_tags` set (`personal`, `financial`, `credential`, `health`, `family`, `legal`) blocks an entity from federating if any matching tag is present.
- Bullets that contain credential-shaped strings (`api_key`, `sk_live_*`, `Bearer `, etc.) are replaced with `[redacted credential-like text]` at export time. The raw entry in `memory.md` is untouched.
