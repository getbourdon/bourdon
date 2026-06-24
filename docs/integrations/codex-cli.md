# Bourdon x Codex CLI

Codex CLI has two useful Bourdon surfaces:

- A `UserPromptSubmit` hook that injects one compact recognition cue into the
  live turn before the model answers.
- An optional MCP server entry that lets Codex query the L6 federation library.

The hook is the live-loop cue. The MCP server is the Claude Code-like background
read surface.

## UserPromptSubmit hook

Add this to `~/.codex/hooks.json`:

```json
{
  "hooks": {
    "UserPromptSubmit": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "bourdon codex hook user-prompt-submit --access-level team",
            "timeout": 5,
            "statusMessage": "Loading Bourdon recognition"
          }
        ]
      }
    ]
  }
}
```

Codex also discovers project hooks at `.codex/hooks.json` inside a trusted
project. User-level hooks are usually better for Bourdon because recognition is a
machine-level integration, not a single-repo behavior.

After editing the hook file, restart Codex CLI and run `/hooks` to review and
trust the new command. Codex will not run new or changed hooks until they are
approved.

If Codex's hook environment cannot find `bourdon` on `PATH`, use the absolute
executable path reported by `Get-Command bourdon` on Windows or `which bourdon`
on POSIX systems:

```json
{
  "type": "command",
  "command": "C:\\Users\\you\\.bourdon-venv\\Scripts\\bourdon.exe codex hook user-prompt-submit --access-level team"
}
```

## Verify the hook command

Run the handler directly before trusting it in Codex:

```powershell
'{"hook_event_name":"UserPromptSubmit","prompt":"Do you know what Bourdon is?","cwd":"C:\\Users\\you\\repos\\bourdon"}' | bourdon codex hook user-prompt-submit --access-level team
```

Expected shape:

```json
{
  "hookSpecificOutput": {
    "hookEventName": "UserPromptSubmit",
    "additionalContext": "..."
  }
}
```

If the prompt has no matching anchors, the hook exits `0` and prints nothing. If
the hook receives malformed JSON or the turn compiler fails, it also exits `0`
and prints nothing so Codex can continue normally. The live hook strips debug
metadata (`score`, `Why:`, routing trace) by default; use
`bourdon codex compile-turn` when you need the full diagnostic brief.

## Optional MCP entry

Register Bourdon's L6 server with Codex when you also want explicit federation
queries:

```bash
codex mcp add bourdon -- bourdon serve --quiet
```

On Windows, use an absolute executable path if needed:

```powershell
codex mcp add bourdon -- C:\Users\you\.bourdon-venv\Scripts\bourdon.exe serve --quiet
```

The MCP entry does not replace the hook. Without the hook, Codex can query
Bourdon only when it decides to call the MCP tool. The hook gives Codex a
recognition brief at the start of the turn.
