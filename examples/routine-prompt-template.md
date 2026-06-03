# Routine self-report prompt template

Drop this snippet at the **end** of any `/schedule`'d routine prompt body so the routine reports back to a tracking GitHub Issue. Your local box's `bourdon claude-code-automations ingest-github --gh-issue …` then pulls the report into the federation.

## Why a GitHub Issue?

Routines run on Anthropic infrastructure — they can't write to your local `~/.claude/automations/`. They *can* run shell commands inside their sandboxed runner, including `gh issue comment`. A long-lived "tracking issue" per routine becomes the durable, audit-friendly relay:

- You can read the run history in the GitHub UI without any local tooling.
- The `gh` CLI on your box pulls the same content via the documented API.
- It's idempotent: `bourdon …ingest-github --gh-issue` dedupes bullets by exact match, so re-pulling the same issue is a no-op.

## One-time setup

1. Create a private tracking issue in a repo you own. Title it after the routine, e.g. `Routine: Weekly PR Audit`. Note its number.
2. Make sure the routine's runtime has a `GITHUB_TOKEN` (or `GH_TOKEN`) with `issues: write` scope to that repo. The `/schedule` skill takes secrets at creation time.

## Prompt snippet (paste at end of routine prompt body)

```
After completing the work above, summarize what happened as a markdown
comment on issue your-org/your-repo#42. Each material thing the run
discovered or did should be one dashed bullet. Keep each bullet under
one line. Skip preamble. Then run:

    gh issue comment 42 --repo your-org/your-repo --body-file - <<'EOF'
    - <first bullet>
    - <second bullet>
    - <...>
    EOF

Replace the placeholder bullets with the actual ones you wrote.
```

## Local ingest

Once per day (or on demand), run on your local box:

```sh
bourdon claude-code-automations ingest-github \
    --gh-issue your-org/your-repo#42 \
    --automation-id weekly-pr-audit
bourdon claude-code-automations export
```

The first command:
- Calls `gh issue view 42 --repo your-org/your-repo --json title,body,comments,createdAt`.
- Treats the issue body as a run entry (dated by `createdAt`).
- Treats each comment as a run entry (dated by its `createdAt`).
- Bullets within a body/comment are extracted; multi-paragraph bodies fall back to one bullet per non-empty line.
- Merges everything into `~/.claude/automations/weekly-pr-audit/memory.md`, deduping bullets per-date.

The second command re-publishes `~/agent-library/agents/claude-code-automations.l5.yaml`.

## Wiring `ingest-github --gh-issue` to a launchd / cron

A simple daily pull:

```sh
# crontab line, runs daily at 09:05 local time
5 9 * * * /Users/you/bourdon/.venv/bin/bourdon claude-code-automations ingest-github \
    --gh-issue your-org/your-repo#42 --automation-id weekly-pr-audit \
    && /Users/you/bourdon/.venv/bin/bourdon claude-code-automations export
```

Tune the cadence to how often the routine runs.

## Alternatives

The relay model is what matters; the channel is interchangeable. If your routine can push to a Git repo (e.g. claude-brain), the same `ingest-github --source ~/claude-brain/automations` mode works. The GitHub Issue path is the most documentable and the lowest-friction one to set up.
