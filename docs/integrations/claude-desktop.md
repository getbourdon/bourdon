# Bourdon × Claude Desktop

Claude Desktop is Anthropic's MCP host with the simplest setup story: a single JSON config file, no plugin marketplace, no per-workspace surface. That makes it the lowest-friction reader for cross-agent federation. **If you only wire one MCP host, wire this one** — it's the demo described in [`docs/PROOF.md`](../PROOF.md).

Claude Desktop federates in **both directions**:

- **Reading**: any conversation can query the federation through the Bourdon tools (this page's main setup).
- **Code + Co-Work surfaces** publish native L5 manifests via the `claude-desktop-code` and `claude-desktop-cowork` participants (shipped v0.8.x — they parse the desktop app's on-disk state).
- **Chat** (claude.ai conversations in the desktop app) is the canonical **self-authoring** member: its IndexedDB store is opaque by design, so there is no parser — the chat model itself calls `commit_to_federation` with `agent_id="claude-desktop-chat"` whenever it has context worth sharing. See [The chat write path](#the-chat-write-path-claude-desktop-chat) below.

## What this gives you

Once configured, the Bourdon tools become callable from any Claude Desktop conversation. Ask the model anything that needs cross-session or cross-agent memory and it'll call into Bourdon transparently. Concrete queries that work after a single `bourdon claude-code export`:

- *"What projects has Claude Code touched this week?"*
- *"Find everything across my agents that mentions <project name>."*
- *"What was the most recent session focused on <topic>?"*

The full tool inventory matches the OpenManus integration; see the table in [`docs/integrations/openmanus.md`](openmanus.md#why-this-works) for what each tool does. Same server, same surface.

## Prerequisites

1. **Bourdon installed in a Python environment Claude Desktop can launch.** `pip install 'bourdon[server]'` in the global Python is the simplest path. If you use a venv, you'll need the absolute path to the venv's `bourdon` binary in the config — see "PATH gotcha" below.
2. **At least one L5 manifest.** Run `bourdon claude-code export` once to seed `~/agent-library/agents/claude-code.l5.yaml`. Verify with `bourdon serve` — the banner should show `agents: 1 loaded`.
3. **Claude Desktop installed and at least once-launched** so its config directory exists.

## The config

Locate Claude Desktop's MCP config file:

- **macOS:** `~/Library/Application Support/Claude/claude_desktop_config.json`
- **Windows:** `%APPDATA%\Claude\claude_desktop_config.json`
- **Linux:** `~/.config/Claude/claude_desktop_config.json` (path may vary; check Claude Desktop's docs)

If the file doesn't exist, create it. Add a `bourdon` entry under `mcpServers`:

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

If Claude Desktop already had other MCP servers configured, just add the `bourdon` key alongside them — don't replace the whole object.

Restart Claude Desktop. The Bourdon tools should appear in the model's tool list.

## PATH gotcha

Claude Desktop launches MCP servers as subprocesses with a minimal PATH — often just system defaults, not your interactive shell's PATH. If `bourdon` lives in a venv, the bare `"command": "bourdon"` won't resolve and the MCP server will silently fail to start.

Symptom: Bourdon tools never appear in the conversation, no obvious error, and Claude Desktop's MCP log says something like `command not found` or the server process exits immediately.

Fix: use the absolute path.

```json
{
  "mcpServers": {
    "bourdon": {
      "command": "/Users/you/path/to/.venv/bin/bourdon",
      "args": ["serve", "--quiet"]
    }
  }
}
```

To find the right path: `which bourdon` in the shell where you ran `pip install bourdon`.

## Custom library path

If your `agent-library/` lives somewhere other than `~/agent-library/` (rare, but the `bourdon serve --library` flag supports it):

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

Three of five participants tag entities as `team` visibility by default (Codex always, Copilot and Cursor by policy). Bourdon's L6 tools default to `access_level="public"`, which **filters those entities out**. For a single-user federation where you trust your own agents, you want `team`.

The friendly fix shipped in v0.6.0: set an environment variable that flips the L6 default per install.

```bash
export BOURDON_DEFAULT_ACCESS_LEVEL=team
```

Put that in your shell rc, or set it inside the MCP server entry in Claude Desktop's config so it inherits even when launched as an MCP subprocess:

```json
{
  "mcpServers": {
    "bourdon": {
      "command": "/Users/you/path/to/.venv/bin/bourdon",
      "args": ["serve", "--quiet"],
      "env": {
        "BOURDON_DEFAULT_ACCESS_LEVEL": "team"
      }
    }
  }
}
```

With the env var set, `list_recent_work`, `find_entity`, and `commit_to_federation` all default to `team` access — no need to ask the model to pass `access_level='team'` explicitly. Explicit arguments still win when provided, so per-call overrides keep working.

Valid values: `public` (default), `team`, `private`. Invalid values log a warning and fall back to `public`.

If you'd rather not use the env var, the original pattern still works — ask the model to pass the access level explicitly: *"Call `list_recent_work` with `access_level='team'`."*

## Verifying it works

The full diagnostic flow:

```
# 1. Verify Bourdon can see at least one manifest.
bourdon serve --quiet
# Expect: "agents: N loaded (claude-code, ...)"
# Ctrl-C to stop.

# 2. Verify the round-trip works on real data (without Claude Desktop in the loop).
bourdon dogfood
# Expect: PASS for any plantable participant you have set up.

# 3. In Claude Desktop, ask:
"Call the Bourdon MCP tool `list_recent_work` with `access_level='team'` and show me the raw result."
# Expect: a structured response with sessions from your federated agents.

# 4. If step 3 works but natural-language queries don't, the model isn't choosing
# to invoke Bourdon. That's a prompting / instructions issue, not an integration
# issue. Add a system prompt fragment like:
#   "When the user asks about past work or cross-agent context, call the
#   Bourdon MCP tools (`list_recent_work`, `find_entity`, etc.)."
```

## The chat write path (`claude-desktop-chat`)

The desktop app's Chat surface contributes to the federation by self-authoring:
the model calls `commit_to_federation` when it decides a piece of context is
worth sharing. No participant code, no IndexedDB reverse-engineering — this is
the canonical pattern for every cloud-only / webview surface
(`docs/AUTHORING_A_PARTICIPANT.md` § write-side).

With the MCP server wired (above), the only missing piece is telling the model
when to commit. Add a fragment like this to your Claude personal preferences
(Settings → Profile → "What personal preferences should Claude consider in
responses?") or a Project's custom instructions:

> When a conversation produces context worth remembering across my AI agents —
> a decision, a plan, a fact about an ongoing project — call the Bourdon MCP
> tool `commit_to_federation` with `agent_id="claude-desktop-chat"` and
> `agent_type="other"`. Put the durable fact in `entities` (name + summary,
> tags where useful) and the session shape in `sessions` (ISO date,
> project_focus, key_actions). Set `visibility: "private"` on anything
> credential-like, financial, or personal. Don't commit small talk; do commit
> decisions and direction changes. Use `mode="merge"` (the default).

Conventions:

- `agent_id` is always `claude-desktop-chat` (one manifest for the surface).
- `agent_type` is `other`.
- Repeated commits merge: entities dedupe by name, sessions by `(date, cwd)`.
- The contribution lands in `~/agent-library/agents/claude-desktop-chat.l5.yaml`
  and is immediately visible to every other federated agent, the tray, and
  peers. (Note: under v0.9.0 trust tiers, stdio callers are the trusted
  operator — chat commits write directly. Remote/quarantined members stage
  instead; see `docs/security-model.md`.)

Verify the path without the desktop app in the loop:

```
# Any MCP client (or another agent) against the same server:
"Call commit_to_federation with agent_id='claude-desktop-chat',
 agent_type='other', and one test entity."
# Then:
bourdon agents | grep claude-desktop-chat
```

## Known limitations
- **No automatic refresh.** The L6 server reads `~/agent-library/` on startup. New manifests written *while* Claude Desktop has the MCP server running won't show up until you restart Claude Desktop. (Future: file-watching in `L6Store`, tracked but not scheduled.)
- **Subprocess lifecycle quirks.** If Claude Desktop's MCP subprocess management gets confused, the Bourdon server can end up in a half-attached state. `pkill -f "core.l6_server"` clears it; restarting Claude Desktop respawns cleanly.
