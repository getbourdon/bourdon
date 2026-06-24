# Cursor Automation Memory Integration

Cursor Cloud Agents (background PR reviews, code generation tasks, scheduled
runs) operate on a parallel track from interactive IDE sessions. Their work
never touches Cursor's SQLite `state.vscdb`, so the interactive
`participants.cursor` participant cannot see it.

The `cursor-automations` participant closes this gap by reading the
`~/.cursor/automations/<id>/` convention — the same pattern Codex and Claude
Code use for their automation memory.

## Quick Start

```bash
bourdon cursor init                          # bootstrap ~/.cursor/automations/
bourdon cursor-automations export --print    # publish L5 manifest
bourdon cursor-automations doctor            # check coverage
```

## Ingesting Remote Automation Memory

```bash
bourdon cursor-automations ingest --source /path/to/automations/
bourdon cursor-automations ingest --artifact-zip cursor-automations.zip
```

## Writer Script

```bash
scripts/cursor-automation-memory-append.sh <automation_id> "summary line"
```

See `docs/cursor-parity-scope.md` for the full convention spec.
