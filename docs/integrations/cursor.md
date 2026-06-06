# Bourdon × Cursor (IDE)

Cursor is both a **publisher** into cross-agent memory and an **MCP consumer** of the same federation.

## Publish: `bourdon cursor export`

Cursor stores composer/workspace state in SQLite `state.vscdb` files:

- **macOS:** `~/Library/Application Support/Cursor`
- **Linux:** `~/.config/Cursor`
- **Windows:** `%APPDATA%\Cursor`

```bash
bourdon cursor export
```

Writes `~/agent-library/agents/cursor.l5.yaml` by default. Use `--cursor-dir`, `--since`, `--out`, `--access-level`, and `--print` as documented in `bourdon cursor export --help`.

## Consume: MCP in Cursor

Prefer `bourdon serve` (requires `[server]` extra) per [`docs/integrations/claude-desktop.md`](claude-desktop.md) — same stack, substitute `bourdon` and your repo root / venv paths.

Example MCP server block (use **absolute** paths; minimal PATH in MCP subprocesses):

```json
{
  "mcpServers": {
    "bourdon": {
      "command": "/absolute/path/to/.venv/bin/bourdon",
      "args": ["serve", "--quiet"]
    }
  }
}
```

Or invoke the module directly:

```json
{
  "mcpServers": {
    "bourdon": {
      "command": "/absolute/path/to/.venv/bin/python",
      "args": ["-m", "core.l6_server"],
      "cwd": "/absolute/path/to/bourdon-repo-root"
    }
  }
}
```

Set `BOURDON_DEFAULT_ACCESS_LEVEL=team` when your manifests use team-scoped entities (see claude-desktop.md).

## Schema note

SQLite date extraction recognizes `lastUpdatedAt` on composer records (alongside `createdAt`, `updatedAt`, etc.) so `--since` filtering matches Cursor’s on-disk JSON.

## See also

- [`docs/agent-integration-status.md`](../agent-integration-status.md)
- [`docs/AUTHORING_A_PARTICIPANT.md`](../AUTHORING_A_PARTICIPANT.md)
