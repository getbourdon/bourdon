# Bourdon × Claude Code

Claude Code is Anthropic's CLI agent. Unlike Claude Desktop, which is a read-only MCP host, Claude Code is **both a writer and a reader** in the Bourdon federation when fully wired:

- **Writer**: the `claude-code` adapter that ships with Bourdon parses your claude-brain `LOG/`, Claude Code auto-memory, and the MCP knowledge graph into a Claude Code L5 manifest at `~/agent-library/agents/claude-code.l5.yaml`. A SessionEnd hook runs `bourdon claude-code export` after every session.
- **Reader**: the `bourdon serve` MCP server, registered in Claude Code's `mcp.json`, lets a Claude Code session query the federation mid-conversation — the same surface Claude Desktop uses.

This doc covers both sides, on macOS and Windows.

## What this gives you

After both edits below, every Claude Code session that ends auto-refreshes its L5 manifest, and every new Claude Code session has MCP tools that read the federation. A fresh session can ask:

- *"What did my previous session work on?"* (reads its own history)
- *"What has Codex been doing this week?"* (cross-agent read)
- *"Find every entity across my agents that mentions <project name>."*

## Prerequisites

1. **Bourdon installed in a Python environment Claude Code can launch.** `pip install 'bourdon[server]'` in the global Python is the simplest path. If you use a venv, you'll need the absolute path to the venv's `bourdon` binary — see "PATH gotcha" below.
2. **At least one L5 manifest.** Run `bourdon claude-code export` once to seed `~/agent-library/agents/claude-code.l5.yaml`.
3. **Claude Code installed and run at least once** so `~/.claude/` exists.

> **Windows note**: as of v0.6.0, the npm `bourdon` package is a reserved-name placeholder, not a runtime. Use the PyPI install regardless of platform. See [Windows install](#windows-install) below.

## The two-part wiring

### 1. MCP server — read side

Edit `~/.claude/mcp.json`. If the file doesn't exist, create it. Add a `bourdon` entry under `mcpServers`:

```json
{
  "mcpServers": {
    "bourdon": {
      "command": "bourdon",
      "args": ["serve", "--quiet"]
    }
  }
}
```

If Claude Code already had other MCP servers, add the `bourdon` key alongside them — don't replace the whole object.

### 2. SessionEnd hook — write side

Edit `~/.claude/settings.json`. Add a `SessionEnd` hook that runs `bourdon claude-code export`. If a `SessionEnd` array already exists, append rather than replace:

```json
{
  "hooks": {
    "SessionEnd": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "bourdon claude-code export"
          }
        ]
      }
    ]
  }
}
```

The `bourdon claude-code export` subcommand is silent and never raises — it exits 0 in all failure modes, so it won't break Claude Code's shutdown.

Restart Claude Code for the MCP server to register. Then `/mcp` (or your model's equivalent) should list `bourdon`.

## Per-platform paths

### macOS

- Settings: `~/.claude/settings.json`
- MCP config: `~/.claude/mcp.json`
- Default venv install: `~/.bourdon-venv/bin/bourdon`
- Default library: `~/agent-library/`

### Windows

- Settings: `C:\Users\<you>\.claude\settings.json`
- MCP config: `C:\Users\<you>\.claude\mcp.json`
- Default venv install: `C:\Users\<you>\.bourdon-venv\Scripts\bourdon.exe`
- Default library: `C:\Users\<you>\agent-library\`

Windows JSON requires double-backslashes in paths:

```json
{
  "mcpServers": {
    "bourdon": {
      "command": "C:\\Users\\you\\.bourdon-venv\\Scripts\\bourdon.exe",
      "args": ["serve", "--quiet"]
    }
  }
}
```

Hook command:

```json
{
  "type": "command",
  "command": "\"$HOME/.bourdon-venv/Scripts/bourdon.exe\" claude-code export"
}
```

## Windows install

If Python isn't already on the system, install it first. The minimum supported version is whatever Bourdon's `pyproject.toml` declares.

```powershell
# Install Python (one-time)
winget install Python.Python.3.12 -e --scope user --silent `
  --accept-source-agreements --accept-package-agreements

# Create a dedicated venv (keeps your global Python clean)
$py = "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe"
& $py -m venv $env:USERPROFILE\.bourdon-venv
& $env:USERPROFILE\.bourdon-venv\Scripts\python.exe -m pip install 'bourdon[server]'

# Verify
& $env:USERPROFILE\.bourdon-venv\Scripts\bourdon.exe doctor
```

Note: `winget`'s user-scope Python install does not always add Python to `PATH`. That's fine — Claude Code's MCP wiring uses the absolute path to `bourdon.exe`, so it doesn't matter whether `python` is globally callable.

## PATH gotcha

Claude Code launches MCP servers as subprocesses with a minimal `PATH` — often just system defaults, not your interactive shell's `PATH`. If `bourdon` lives in a venv, the bare `"command": "bourdon"` won't resolve and the MCP server will silently fail to start.

Symptom: Bourdon tools never appear in the model's tool list, no obvious error, and the MCP log says something like `command not found` or the server process exits immediately.

Fix: use the absolute path. Find it with `which bourdon` (macOS) or `Get-Command bourdon` (Windows PowerShell) in the shell where you ran `pip install bourdon`.

## Custom library path

If your `agent-library/` lives somewhere other than `~/agent-library/`:

```json
{
  "mcpServers": {
    "bourdon": {
      "command": "bourdon",
      "args": ["serve", "--quiet", "--library", "/path/to/agent-library"]
    }
  }
}
```

## Visibility (the access-level question)

Three of five adapters tag entities as `team` visibility by default (Codex always, Copilot and Cursor by policy). Bourdon's L6 tools default to `access_level="public"`, which **filters those entities out**. For a single-user federation where you trust your own agents, you want `team`.

The friendly fix shipped in v0.6.0: set an environment variable that flips the L6 default per install. Set it inside the MCP server entry so it inherits when launched as a subprocess:

```json
{
  "mcpServers": {
    "bourdon": {
      "command": "bourdon",
      "args": ["serve", "--quiet"],
      "env": {
        "BOURDON_DEFAULT_ACCESS_LEVEL": "team"
      }
    }
  }
}
```

Valid values: `public` (default), `team`, `private`. Invalid values log a warning and fall back to `public`.

## Verifying it works

```bash
# 1. Confirm at least one manifest is on disk.
bourdon serve --quiet
# Expect: "agents: N loaded (claude-code, ...)"
# Ctrl-C to stop.

# 2. Round-trip on your local stores.
bourdon dogfood
# Expect: PASS for each plantable adapter you have set up.

# 3. End a Claude Code session and check the manifest refreshed.
ls -la ~/agent-library/agents/claude-code.l5.yaml
# (Compare timestamps before and after a session.)

# 4. In a new Claude Code session, ask:
#    "Call the Bourdon MCP tool `list_recent_work` with `access_level='team'`
#     and show me the raw result."
# Expect: structured response with sessions from your federated agents.
```

## Cross-machine federation note

The Bourdon L5 store at `~/agent-library/` is **per-machine**. If you run Claude Code on multiple machines (e.g., a macOS workstation and a Windows desktop), each machine writes its own `claude-code.l5.yaml` and they don't auto-sync.

However: if both machines share a `claude-brain` (or equivalent shared source layer) via git, the `claude-code` adapter on each machine reads the *combined* logs and produces a manifest that includes cross-machine sessions. The `agent.instance` field distinguishes which physical machine ran the session. So cross-machine federation works in practice today via shared source data, even though shared L5 sync is not native in v0.6.0.

## Known limitations

- **No automatic refresh.** The L6 server reads `~/agent-library/` on startup. New manifests written *while* Claude Code has the MCP server running won't show up until the next Claude Code restart.
- **MCP server changes need a Claude Code restart.** Editing `mcp.json` mid-session won't take effect until you exit and re-launch.
- **`bourdon --version`** (no subcommand) currently errors with an "unrecognized arguments" message; use `bourdon doctor` or `pip show bourdon` to confirm the installed version.
