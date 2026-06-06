# Cursor Automation Memory Integration

Cursor Cloud Agents (background PR reviews, code generation tasks, scheduled
runs) operate on a parallel track from interactive IDE sessions. Their work
never touches Cursor's SQLite `state.vscdb`, so the interactive
`adapters.cursor` adapter cannot see it.

The `cursor-automations` adapter closes this gap by reading the
`~/.cursor/automations/<id>/` convention — the same pattern Codex and Claude
Code use for their automation memory.

## Convention

```
~/.cursor/automations/
└── <automation_id>/
    ├── automation.toml   # id, name, status, schedule, kind, cwds
    └── memory.md         # dated bullet entries, one block per run
```

### `automation.toml`

```toml
version = 1
id = "cursor-cloud-agent"
name = "cursor-cloud-agent"
status = "ACTIVE"
kind = "cursor-cloud-agent"
rrule = ""
cwds = ["/workspace/bourdon"]
```

### `memory.md`

```markdown
2026-06-05
- Ran automated PR review for Bourdon federation layer.
- Found 2 issues: missing credential scrubbing and stale L5 manifest.

2026-06-06
- Verified cross-machine sync working across desktop and laptop.
```

## Quick Start

### 1. Initialize the convention directory

```bash
bourdon cursor init
# Creates ~/.cursor/automations/cursor-cloud-agent/ with template files
```

Or with a custom automation ID:

```bash
bourdon cursor init --automation-id my-weekly-audit
```

### 2. Write automation run entries

**Option A: Shell helper (recommended for scripts/hooks)**

```bash
# Symlink or copy to a convenient location
cp scripts/cursor-automation-memory-append.sh ~/.cursor/hooks/

# Append a run entry
~/.cursor/hooks/cursor-automation-memory-append.sh cursor-cloud-agent \
    "Reviewed 3 open PRs in Bourdon; no critical issues."
```

The shell helper:
- Creates `automation.toml` on first run (if missing)
- Appends a dated bullet to `memory.md`
- Opens a new date section if today's date isn't the last header
- Exits 0 silently — never fails the calling automation

Environment override: `CURSOR_AUTOMATIONS_DIR` (default: `~/.cursor/automations`)

**Option B: Direct file editing**

Edit `~/.cursor/automations/<id>/memory.md` directly. Each `YYYY-MM-DD`
header starts a new run; bullets under it become `key_actions` in the L5
manifest.

### 3. Export to federation

```bash
bourdon cursor-automations export
# Writes ~/agent-library/agents/cursor-automations.l5.yaml
```

Or as part of a full export:

```bash
bourdon export-all
```

### 4. Verify

```bash
bourdon cursor-automations doctor
bourdon doctor  # includes cursor-automations in the global check
```

## Ingesting Remote Automation Memory

When Cursor Cloud Agents run on a different machine or in CI, their
automation memory needs to be merged into the local convention directory.

### From a local directory

```bash
bourdon cursor-automations ingest --source /path/to/remote/automations/
```

### From a workflow artifact zip

```bash
bourdon cursor-automations ingest --artifact-zip cursor-automations.zip
```

The `merge_automation_tree` function handles:
- Creating new automation directories if they don't exist
- Synthesizing `automation.toml` stubs when the source lacks one
- Per-date bullet deduplication (exact-string match)
- Chronological ordering of date sections
- Skipping automation IDs with invalid characters
- Idempotent operation — running twice is a no-op

### CI/CD Integration

In a GitHub Actions or similar workflow:

```yaml
- name: Record Cursor automation run
  run: |
    ./scripts/cursor-automation-memory-append.sh cursor-cloud-agent \
      --kind github-action --cwd $GITHUB_WORKSPACE \
      "Completed automated review of ${{ github.event.pull_request.title }}"

- name: Upload automation memory
  uses: actions/upload-artifact@v4
  with:
    name: cursor-automations
    path: ~/.cursor/automations
    retention-days: 30
```

Then on a local machine:

```bash
# Download and merge
gh run download <run-id> --name cursor-automations --dir /tmp/artifact
bourdon cursor-automations ingest --source /tmp/artifact/automations
bourdon cursor-automations export
```

## How It Federates

Once exported, `cursor-automations.l5.yaml` participates in the full
Bourdon federation stack:

- `bourdon prepare-turn` — includes automation entities in recognition
- `bourdon serve` / MCP tools — `find_entity`, `list_recent_work` surface runs
- `bourdon sync push/pull` — automation manifests sync across machines
- Peer L6 HTTP federation — automation entities queryable by remote peers

The automation adapter is a separate agent ID (`cursor-automations`) from
the interactive adapter (`cursor`), so federation queries can distinguish
"human used Cursor IDE" from "Cloud Agent ran in background."
