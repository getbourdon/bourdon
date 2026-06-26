# Bourdon × Hermes Agent

[Hermes Agent](https://hermes-agent.nousresearch.com) (Nous Research) is a
tool-calling assistant that runs across a CLI/TUI and messaging gateways
(Slack, Telegram, Discord, WhatsApp). It keeps all durable state under a single
home directory — `~/.hermes` by default, overridable with `$HERMES_HOME`:

- `state.db` — a SQLite store with `sessions` and `messages` tables. Every CLI,
  TUI, and gateway conversation lands here.
- `memories/` — Markdown memory stores (`memory.md`, `user.md`) holding the
  durable, cross-session facts the agent has chosen to save.
- `skills/` — the curated skill library.

Hermes has two useful Bourdon surfaces:

- An **L5 participant** (`bourdon hermes export`) that reads `~/.hermes` and
  publishes a federation manifest. This is the background read surface — the
  Claude Code-like "publish my memory so other agents can see it" path.
- An optional **MCP server** entry so Hermes can query the L6 federation library
  live, mid-turn, via its `mcp_bourdon_*` tools.

## Publish Hermes into the federation

The participant is auto-discovered, so the one-shot path is just:

```bash
bourdon export-all          # writes ~/agent-library/agents/hermes.l5.yaml
```

or, to export only Hermes:

```bash
bourdon hermes export
```

Useful flags:

```bash
bourdon hermes export \
  --access-level team \           # public | team | private redaction tier
  --since 2026-06-01 \            # only sessions on/after this date
  --out ~/agent-library/agents/hermes.l5.yaml \
  --print                          # echo the manifest after writing
```

What the manifest carries:

- **`recent_sessions`** — derived from `state.db`. Archived sessions are
  excluded; each row carries the date, `cwd`, a `project_focus` from the cwd
  basename, and `key_actions` (the session title plus the distinct tool names
  used in that session).
- **`known_entities`** — project workspaces inferred from session `cwd`s, plus
  facts/preferences parsed from `memories/memory.md` and `memories/user.md`.

### Diagnose first

```bash
bourdon hermes doctor
```

```yaml
health:
  status: ok
  details:
    hermes_home: /home/you/.hermes
    state_db: /home/you/.hermes/state.db
    memories_dir: /home/you/.hermes/memories
    skills_dir: /home/you/.hermes/skills
native_path: /home/you/.hermes
```

`status` is `blocked` when `~/.hermes` is absent (Hermes not installed here),
`degraded` when memories exist but `state.db` doesn't (no session history yet),
and `ok` once at least one session is readable. `bourdon doctor` reports Hermes
alongside every other participant.

## Auto-publish at session end (shell hook)

Hermes supports shell-script hooks declared in `~/.hermes/config.yaml` and
managed by `hermes hooks`. Wire a `SessionEnd` hook so the manifest refreshes
after every conversation:

```yaml
# ~/.hermes/config.yaml
hooks:
  SessionEnd:
    - command: "bourdon hermes export"
      timeout: 10
```

Then approve it (hooks require first-use consent):

```bash
hermes hooks list          # review matcher + consent status
hermes hooks test SessionEnd   # fire against a synthetic payload
```

The export is read-only against `state.db` (opened `mode=ro&immutable=1`), so it
will never disturb a live Hermes process holding a write lock, and it exits
cleanly even when there is nothing new to publish.

## Read the federation from inside Hermes (MCP)

Register Bourdon's L6 server as an MCP server so Hermes can query the federation
live:

```bash
hermes mcp add bourdon -- bourdon serve --quiet
```

Once wired, Hermes gains the `mcp_bourdon_*` tool family —
`prepare_recognition_context`, `find_entity`, `get_cross_agent_summary`,
`list_agents`, `list_recent_work`, and `commit_to_federation` — so it can both
read cross-agent context and (as an MCP-aware cloud agent) push its own L5
contributions.

The MCP entry and the participant are complementary: the participant is how
*other* agents see Hermes; the MCP entry is how Hermes sees *everyone else*.

## Verify the export command

Run the handler directly before trusting it in a hook:

```bash
HERMES_HOME=~/.hermes bourdon hermes export --print | head -30
```

If `~/.hermes` has no readable sessions yet the manifest still validates — it
just carries an empty `recent_sessions` / `known_entities`. Visibility is
enforced inside the participant before emission: memory lines that look like
credentials are redacted, and any entity carrying a private-class tag
(`personal`, `financial`, `credential`, `secret`, `health`, `family`, `legal`)
is dropped, never federated.
