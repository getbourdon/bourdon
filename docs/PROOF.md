# PROOF — cross-agent recall, end to end

This is the demo that makes Bourdon real: one agent learns something, the user switches to a different agent, and the second agent can recall what the first one did — without anything being copy-pasted between them.

If this works on your machine, the product works. If it doesn't, the rest of the test suite doesn't matter.

## The five-minute demo

```
┌─────────────────────────┐         ┌─────────────────────────┐
│   Agent A (writer)      │         │   Agent B (reader)      │
│   e.g. Claude Code      │         │   e.g. Claude Desktop   │
└────────────┬────────────┘         └────────────▲────────────┘
             │                                   │
             │ writes L5 manifest                │ queries L6 via MCP
             ▼                                   │
     ~/agent-library/agents/                     │
     claude-code.l5.yaml          ───►   bourdon serve  (L6 MCP server)
```

The promise: after a Claude Code session ends, opening *any* MCP-aware agent configured with Bourdon's L6 server should let that agent answer "what did Claude Code work on?" using nothing but Bourdon as the bridge.

## What "success" looks like

You ask Agent B something like:

> *"What projects did Claude Code touch in the last week?"*

Agent B calls the MCP tool `list_recent_work` (or `find_entity`, or `query_agent_memory`) against Bourdon's L6 server, gets back a structured response, and answers with real entity names, sessions, and attribution to the `claude-code` agent.

If Agent B says *"I don't know what Claude Code did"* — the demo failed. Diagnose with `bourdon dogfood` and `bourdon doctor`.

## Prerequisites

1. **Bourdon installed in a Python env that's accessible to the MCP client.** For most setups, that means `pip install bourdon[server]` in either the global Python or a venv whose `bourdon` binary is on PATH.
2. **At least one L5 manifest in `~/agent-library/agents/`.** The fastest way to produce one is to run `bourdon claude-code export` once — that emits `~/agent-library/agents/claude-code.l5.yaml` from your `~/claude-brain` and `~/.claude` content. (Long-term, the Claude Code SessionEnd hook does this automatically; right now the export is manual.)
3. **An MCP-aware reader agent.** Claude Desktop is the most friction-free, but Cursor, OpenManus, and any other MCP host work the same way. See per-host config in [`docs/integrations/`](integrations/).

## Worked example — Claude Code (writer) → Claude Desktop (reader)

### Step 1 — produce an L5 manifest

```
bourdon claude-code export
```

Verify it landed:

```
ls -la ~/agent-library/agents/
# claude-code.l5.yaml should appear
```

Optionally inspect it:

```
head -40 ~/agent-library/agents/claude-code.l5.yaml
# You should see `agent.id: claude-code`, `known_entities`, `recent_sessions`.
```

### Step 2 — verify the L6 server starts and sees the manifest

```
bourdon serve --quiet &
# Or in another terminal:
bourdon serve
# Should print:
#   Bourdon L6 server
#     library:   /Users/you/agent-library
#     agents:    1 loaded (claude-code)
#     transport: stdio
#     ...
# Press Ctrl-C to stop.
```

If `agents:` says `0 loaded`, the L6 server isn't seeing your manifest. Check the library path and that the export from Step 1 actually wrote a file.

### Step 3 — wire Bourdon into Claude Desktop

Open Claude Desktop's MCP config file:

- **macOS:** `~/Library/Application Support/Claude/claude_desktop_config.json`
- **Windows:** `%APPDATA%\Claude\claude_desktop_config.json`

Add a `mcpServers` entry pointing at `bourdon serve`:

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

If `bourdon` isn't on the PATH that Claude Desktop sees (a common gotcha when Bourdon lives in a venv), use the absolute path:

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

Restart Claude Desktop. The Bourdon tools should appear in the MCP tool list.

### Step 4 — ask the reader to recall the writer

In Claude Desktop, ask something Claude Code knew but Claude Desktop has never seen:

> *"What projects has Claude Code been working on this week, based on Bourdon?"*

Claude Desktop should invoke one of:
- `list_recent_work(agent="claude-code")`
- `get_cross_agent_summary(project="<a project name>")`
- `query_agent_memory(agent="claude-code", topic="<topic>")`

…and answer with real entity names from your `claude-brain` projects, attributed to `claude-code`.

**That's the demo.** If Claude Desktop returns a coherent answer that includes content it could only have known via Bourdon, federation works end-to-end.

## What failure looks like (and how to diagnose)

| Symptom | What it usually means | Fix |
|---|---|---|
| `bourdon serve` says `agents: 0 loaded` | No L5 manifests in `~/agent-library/agents/` | Run `bourdon claude-code export` (or the equivalent for whatever you want to federate) |
| Claude Desktop never shows Bourdon tools after restart | The `command` in the MCP config isn't on Claude Desktop's PATH | Use an absolute path to the `bourdon` binary in the venv |
| Bourdon tools appear but agent says "I don't have data" | The tools work but the agent isn't using them | Ask more explicitly: *"Call the Bourdon MCP tool `list_recent_work` and tell me what it returns."* |
| Tools return empty results | Visibility filtering — entities tagged `team` or `private` are filtered at default `public` access | Pass `access_level="team"` in the tool call. See [`docs/agent-integration-status.md`](agent-integration-status.md) for which adapters default to which visibility |
| Anything else | `bourdon dogfood` for the round-trip smoke test, `bourdon doctor` for adapter health | Both commands write actionable notes |

## Variations

- **Reader = Cursor:** Cursor's MCP config story is evolving; see [`docs/integrations/`](integrations/) for the current setup.
- **Reader = OpenManus:** Already documented in [`docs/integrations/openmanus.md`](integrations/openmanus.md).
- **Reader = any MCP-aware client:** The Bourdon L6 server exposes a standard MCP surface (resources + tools). Any client that speaks MCP can wire it the same way as Claude Desktop above. The transport choices are `stdio` (default, fewer moving parts) and `http` (`bourdon serve --transport http --port 7500`).
- **Writer = Codex / Cursor / Copilot / Cascade:** Each adapter has its own export command (`bourdon codex export`, `bourdon cursor export`, etc.) and adapter-specific quirks. See [`docs/agent-integration-status.md`](agent-integration-status.md).
- **Multi-writer:** Run all the export commands you have access to. The L6 server federates whatever lands in `~/agent-library/agents/`. The reader sees a unified view.

## Why this is the gating criterion

Layer 1 (`tests/test_federation_roundtrip.py`) proves the contract with synthetic fixtures. Layer 2 (`bourdon dogfood`) proves the round-trip with real local data. Neither layer proves what an actual user experiences: *"I open my second agent and it just knows what my first agent did."*

That experience is only provable end-to-end with two real MCP-aware agents on a machine. This document is the script for verifying that experience. Until this demo runs clean on a fresh machine in under ten minutes, the rest of the suite is structural support for a product that doesn't yet exist.

If you run this and it fails, file an issue with:
- The `bourdon dogfood` output
- The `bourdon doctor` output
- The reader agent's MCP tool log (most clients have one)
- The exact prompt you asked and the response you got
