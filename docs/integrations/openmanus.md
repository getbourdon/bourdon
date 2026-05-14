# Bourdon × OpenManus

[OpenManus](https://github.com/FoundationAgents/OpenManus) is an open-source autonomous-agent framework that's MCP-native — it ships first-class support for [Model Context Protocol](https://modelcontextprotocol.io/) servers as tool sources. Bourdon's L6 federation server *is* an MCP server. That makes the integration a one-step config edit on the OpenManus side. No adapter code, no glue layer.

After wiring this up, an OpenManus agent can recall what your Claude Code, Codex, and Cursor sessions did via Bourdon's cross-agent memory federation, all from inside its normal MCP tool surface.

## Why this works

OpenManus's `Manus` agent (in [`app/agent/manus.py`](https://github.com/FoundationAgents/OpenManus/blob/main/app/agent/manus.py)) accepts MCP servers via its `mcp_clients` field. Servers are loaded from `config/mcp.json` at agent startup with two transport options: `stdio` (subprocess pipe) and `sse` (HTTP/SSE). Bourdon's L6 server speaks both — `stdio` is the default and the recommended transport because it doesn't require running a long-lived process.

Once Bourdon's L6 is registered, every tool the L6 server exposes becomes a tool the OpenManus agent can call:

| Bourdon L6 tool | What it does |
|---|---|
| `query_agent_memory` | Search a specific agent's L5 manifest for entities and recent work matching a topic |
| `list_recent_work` | List recent sessions across all agents (or filtered to one) since a date |
| `find_entity` | Look up an entity (project, file, concept) across agents and return everywhere it's been touched |
| `get_cross_agent_summary` | Summarize a project's state across every agent that's worked on it |
| `prepare_recognition_context` | Recognition-first runtime hook — format Bourdon recognition + prompt fragment for federation-backed memory |
| `get_deeper_context` | Pull deeper episodic context (L2) for a prompt when the agent has the response time |
| `commit_to_federation` | (v0.6.0+) Write-side tool — the OpenManus agent can contribute its own entities and sessions to the federation, not just read from it |

Plus the `agent-library://` MCP resources (`agent-library://agents`, `agent-library://agents/{id}/memory`, `agent-library://entities/{name}`).

## Prerequisites

1. **Install Bourdon.** `pip install 'bourdon[server]'` (latest release; see [GitHub Releases](https://github.com/getbourdon/bourdon/releases) for the current version). The `bourdon` CLI must be on PATH for the OpenManus user — see the [PATH gotcha](#path-gotcha) note below if it isn't.
2. **Have at least one L5 manifest.** Bourdon needs something to federate. The fastest path: enable Bourdon's Claude Code adapter via the SessionEnd hook (see [`docs/integrations/claude-code.md`](claude-code.md) or [`docs/agent-integration-status.md`](../agent-integration-status.md)) so that ending a Claude Code session writes `~/agent-library/agents/claude-code.l5.yaml`. After one session, you have something to query.
3. **Verify the L6 server starts.** Run `bourdon serve --quiet` once in a terminal. The process should start and stay attached on stdio. Press Ctrl-C to stop.

## The config block

Add Bourdon to your OpenManus `config/mcp.json` (create the file if it doesn't exist; the schema is documented in OpenManus's [`config/mcp.example.json`](https://github.com/FoundationAgents/OpenManus/blob/main/config/mcp.example.json)):

```json
{
  "mcpServers": {
    "bourdon": {
      "type": "stdio",
      "command": "bourdon",
      "args": ["serve", "--quiet"]
    }
  }
}
```

That's the entire integration on the OpenManus side. Restart OpenManus; the agent now has the Bourdon tools available.

If you want to point at a specific agent-library directory (e.g., a shared NAS mount or a Litestream-replicated copy), pass `--library`:

```json
{
  "mcpServers": {
    "bourdon": {
      "type": "stdio",
      "command": "bourdon",
      "args": ["serve", "--quiet", "--library", "/mnt/nas/agent-library"]
    }
  }
}
```

If you'd rather run L6 as a long-lived HTTP service (useful when OpenManus runs in a container that can't spawn subprocesses), start it separately and use the `sse` transport:

```sh
bourdon serve --transport http --port 7500 --library ~/agent-library
```

```json
{
  "mcpServers": {
    "bourdon": {
      "type": "sse",
      "url": "http://localhost:7500/sse"
    }
  }
}
```

## Verifying the integration

1. **Start OpenManus** with the config above in place.
2. **Ask the agent a question that requires cross-agent memory.** Example: "What was the last thing Codex worked on in the bourdon repo?" The agent should reach for `query_agent_memory` or `list_recent_work` rather than refusing or guessing.
3. **Confirm L6 is being called.** Run L6 manually with `--transport stdio` and watch its log lines as OpenManus issues queries. You'll see `query_agent_memory(agent="codex", topic="bourdon")` style entries.

If the agent says it has no memory tools, double-check:
- The config file location matches what OpenManus reads on startup (`config/mcp.json` by default; verify against `app/config.py` in your OpenManus version).
- The `bourdon` binary is on the PATH OpenManus inherits when it spawns the MCP subprocess. See the [PATH gotcha](#path-gotcha) below — replacing `"command": "bourdon"` with the absolute path to the venv's `bourdon` binary is the standard fix.
- `~/agent-library/agents/` contains at least one `.l5.yaml` file. Empty libraries return nothing, which the agent might describe as "no memory available."

## PATH gotcha

MCP hosts launch stdio servers as subprocesses with a minimal PATH — often just system defaults, not the user's shell PATH. If `bourdon` lives in a venv, the bare `"command": "bourdon"` won't resolve and the server will silently fail to start.

Symptom: Bourdon tools never appear in OpenManus's tool list, no obvious error in OpenManus's logs, and the MCP subprocess exits immediately.

Fix: use the absolute path to the venv's `bourdon` binary.

```json
{
  "mcpServers": {
    "bourdon": {
      "type": "stdio",
      "command": "/Users/you/path/to/.venv/bin/bourdon",
      "args": ["serve", "--quiet"]
    }
  }
}
```

To find the right path: run `which bourdon` (macOS/Linux) or `Get-Command bourdon` (Windows PowerShell) in the shell where you ran `pip install bourdon`.

## Visibility model

OpenManus calls Bourdon's tools at the **`team`** access level by default. Entities tagged `private` in their source L5 manifest are filtered out at the L6 layer and never reach the OpenManus agent. If you want to give OpenManus access to private entities, override per-call (most L6 tools accept an `access_level` argument).

If you want to *prevent* OpenManus from seeing certain agents' memory entirely, scope the L6 server's `--library` flag to a directory that only contains the manifests you want to expose.

## Tradeoffs

- **OpenManus is currently in maintenance mode.** Last merged PR was 2025-11-14. The integration shape is stable (it's a config edit, no upstream code), but if OpenManus's MCP loader changes in a future release, this doc may need an update.
- **No Bourdon-side adapter for OpenManus yet.** Bourdon doesn't read OpenManus's session/memory state to publish an `openmanus.l5.yaml`. OpenManus is a *consumer* of Bourdon, not a *publisher* into it. If/when OpenManus's distilled-memory model stabilizes upstream, a Python adapter following [`docs/AUTHORING_AN_ADAPTER.md`](../AUTHORING_AN_ADAPTER.md) would close the loop.
- **Recognition-first runtime is opt-in.** The `prepare_recognition_context` tool is exposed but OpenManus's default agent loop doesn't call it on every turn. If you want true concurrent recognition (the Bourdon thesis), wire it into the system prompt or pre-turn hook explicitly.

## Related

- Bourdon project: [bourdon.ai](https://bourdon.ai) · [getbourdon/bourdon](https://github.com/getbourdon/bourdon)
- OpenManus project: [FoundationAgents/OpenManus](https://github.com/FoundationAgents/OpenManus)
- MCP specification: [modelcontextprotocol.io](https://modelcontextprotocol.io/)
- Adapter authoring (if you want to ship an OpenManus → Bourdon publisher): [`docs/AUTHORING_AN_ADAPTER.md`](../AUTHORING_AN_ADAPTER.md)
