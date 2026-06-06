# Agent Integration Status

This page tracks the operational Bourdon layer available for each agent. It is
the current implementation map, not the long-term participant wishlist.

## Claude Code

Status: export hook available.

- `bourdon claude-code export` writes `~/agent-library/agents/claude-code.l5.yaml`.
- Intended hook: Claude Code `SessionEnd`.
- Current role: manager/reviewer memory source for L6 federation.

## Codex

Status: SQLite-backed fallback and turn preparation available.

- `bourdon codex doctor` diagnoses `~/.codex/state_5.sqlite`, stale-index fallback,
  and fallback recall.
- `bourdon codex export` prefers live `state_5.sqlite` thread metadata and falls
  back to `session_index.jsonl` on older Codex installs.
- `bourdon codex prepare-turn` refreshes Codex fallback memory surfaces and emits
  prompt-ready recognition context.
- `bourdon codex sync-native --from-library` sources `bourdon_fallback.md` from
  the federation library (`~/agent-library/agents/*.l5.yaml`) instead of local
  Codex history. Required on a fresh machine where Codex has no local sessions
  yet; without it, federation transport alone cannot deliver recognition
  context. Add `--include-local` to merge local Codex history into the same
  file. `--access-level public|team|private` filters which federation entities
  get rendered (default `team`).
- Native Codex Stage 1 distilled memory is not relied on when it reports no
  stage1 outputs and errored memory jobs.

## Cursor

Status: participant available; `bourdon cursor export` is the native SQLite export
path.

- `CursorParticipant` reads Cursor's SQLite state through a read-only temp copy.
- `bourdon cursor export` writes `~/agent-library/agents/cursor.l5.yaml`.
- **Setup walkthrough:** [`docs/integrations/cursor.md`](integrations/cursor.md) (MCP in Cursor IDE + export).
- The existing short-index memory-cycle scripts remain available for manually
  curated Cursor memory.

## Cline

Status: blocked pending confirmed native memory store path/schema.

- No Cline participant should be added until its durable local memory source is
  known.
- Until then, Cline can consume Bourdon through the generic MCP or shell
  surfaces: `prepare_recognition_context` or `bourdon prepare-turn`.

## OpenManus

Status: zero-code MCP integration — OpenManus consumes Bourdon's L6 server as
an MCP source.

- OpenManus is MCP-native (`Manus.mcp_clients`, `config/mcp.json` schema).
- Add a `bourdon` entry to OpenManus's `config/mcp.json` pointing at
  `python -m core.l6_server` (stdio) — see
  [`docs/integrations/openmanus.md`](integrations/openmanus.md) for the literal
  config block and walkthrough.
- After wiring, the OpenManus agent gets all seven L6 tools
  (`query_agent_memory`, `list_recent_work`, `find_entity`,
  `commit_to_federation`,
  `get_cross_agent_summary`, `prepare_recognition_context`,
  `get_deeper_context`) plus the `agent-library://` resources.
- OpenManus consumes Bourdon by default via read tools and can **publish**
  federation updates through `commit_to_federation` whenever the orchestrator
  surfaces that MCP surface to its model loop (same pattern documented in
  `docs/AUTHORING_A_PARTICIPANT.md` for other cloud-first agents).

## Cascade (Windsurf)

Status: participant available; `bourdon cascade export` is the convention-file
export path.

- Cascade has no standardized on-disk session state (similar to Copilot). The
  participant reads from a **convention-based memory file** at
  `~/.cascade-bourdon/memory.md` that Cascade maintains at session end.
- `bourdon cascade init` creates `~/.cascade-bourdon/memory.md` with a starter
  template. Edit the YAML front-matter to add entities and sessions.
- `bourdon cascade export` reads the file, applies visibility filtering, and
  writes `~/agent-library/agents/cascade.l5.yaml`.
- `bourdon cascade doctor` diagnoses the memory file and reports entity/session
  counts, front-matter validity, and health status.
- Credential redaction uses the canonical pattern set from `participants/codex.py`,
  extended with Cascade-specific patterns (`secret`, `sk_test_*`). See
  [`SECURITY.md`](../SECURITY.md) for the full runtime security model.
- Current role: agentic pair-programmer with multi-step planning for L6
  federation.

## GitHub Copilot

Status: participant available; `bourdon copilot export` is the convention-file
export path.

- GitHub Copilot has no accessible on-disk session index (cloud-side reasoning,
  no session JSONL). The participant reads from a **convention-based memory file**
  at `~/.copilot-bourdon/memory.md` that users or Copilot Chat can maintain.
- `bourdon copilot init` creates `~/.copilot-bourdon/memory.md` with a starter
  template. Edit the YAML front-matter to add entities and sessions.
- `bourdon copilot export` reads the file, applies visibility filtering, and
  writes `~/agent-library/agents/copilot.l5.yaml`.
- `bourdon copilot doctor` diagnoses the memory file and reports entity/session
  counts, front-matter validity, and health status.
- Current role: inline-completion and chat ambient layer for L6 federation.
- Copilot Chat can be instructed to update `memory.md` at session end using a
  custom instruction like:

  > "At the end of each session, append a YAML session block to the `sessions:`
  > list in `~/.copilot-bourdon/memory.md`, and add any new project or concept
  > entities to the `entities:` list."

## Cross-machine library distribution (`bourdon sync`)

Status: shipped as `bourdon sync push|pull` per issue #74.

`~/agent-library/` no longer requires manual `scp -r` or git-temp-branch
transports to move between machines. The CLI surface:

```bash
# push the local library to another host
bourdon sync push user@bourdon-mac.tailnet:~/agent-library/

# pull a remote library into the local one
bourdon sync pull user@bourdon-pc.tailnet:~/agent-library/
```

Behind the scenes:

- **Visibility filter (push only).** Push stages a copy of `~/agent-library/`
  in a tempdir with all `agents/*.l5.yaml` filtered by `--access-level`
  (default: `public`). Entities and sessions above the requested level are
  dropped from the staged copy before the rsync. The default is the most
  restrictive — pushing `team` or `private` manifests is an opt-in.
- **Transport.** Wraps `rsync -az --checksum --delay-updates` so the network
  leg is idempotent (checksum-based change detection) and atomic per file
  (rsync writes updated files to the destination's `.~tmp~/` and renames
  them in at the end of the run, so a mid-sync failure leaves the destination
  consistent).
- **Sources / destinations.** Whatever rsync accepts: local paths,
  `user@host:path` over SSH, or `rsync://` URLs. Trailing-slash semantics
  follow rsync.

`bourdon sync push` requires `rsync` on `$PATH`. macOS/Linux ship it; Windows
users need WSL.

For visibility verification before pushing, `--dry-run` shows the rsync plan
plus per-agent entity/session counts so you can confirm the filter outcome
without burning a network leg.

## Peer L6 federation (Phase 1.6)

Status: available behind the `[federation]` extras.

Bourdon's L6 server can federate with peer L6 servers on other machines over
HTTP (designed to ride a Tailscale tailnet). Federated query tools merge peer
responses with local results at call time. Peer-sourced agents are tagged
`peer:<peer-name>:<agent>` so provenance stays clear.

### Worked example — Mac ↔ PC over Tailscale

```bash
# On each machine:
pip install 'bourdon[server,federation]'

# Generate one shared bearer token per direction (different tokens both ways
# is the recommended setup; same-Tailnet is not a substitute for auth).
TOKEN_PC=$(python -c "import secrets; print(secrets.token_urlsafe(32))")
TOKEN_MAC=$(python -c "import secrets; print(secrets.token_urlsafe(32))")

# On PC -- expose locally to the Tailnet, accept Mac's bearer:
BOURDON_PEER_TOKEN_SERVER=$TOKEN_MAC \
  bourdon serve --transport http --port 7500 \
    --peer http://bourdon-mac.tailnet:7500

# On Mac -- mirror:
BOURDON_PEER_TOKEN_SERVER=$TOKEN_PC \
BOURDON_PEER_TOKEN=$TOKEN_MAC \
  bourdon serve --transport http --port 7500 \
    --peer http://bourdon-pc.tailnet:7500
```

Or declare peers in `~/.bourdon/peers.yaml` (see
[`config/peers.example.yaml`](../config/peers.example.yaml)).

### Auth defaults

- HTTP transport requires `BOURDON_PEER_TOKEN_SERVER` to be set; missing
  token + missing `--allow-unauthenticated` flag returns 503 to every
  request (fails closed).
- `--allow-unauthenticated` exists as an explicit escape hatch for
  localhost-only testing. Don't use it on Tailscale-exposed ports.
- Client-side: default env is `BOURDON_PEER_TOKEN`; per-peer override via
  `token_env:` in `peers.yaml`.

### What gets federated

| Tool | Local-only | Federated path |
|---|---|---|
| `list_agents` | sync | merged + sorted, peer agents not prefix-tagged here |
| `find_entity` | sync | merged by entity name, peer agents tagged `peer:<name>:<agent>` |
| `list_recent_work` | sync | merged sessions, dedupe by `(date, cwd, agent)`, peer agents tagged |
| `get_cross_agent_summary` | sync | merged agents + sessions + entities, peer agents tagged |
| `prepare_recognition_context` | sync (~1.2 ms) | local fires first, peers queried in parallel with per-peer timeout (default 200 ms). Peer-matched entities merged with `peer:<name>:<agent>` tags. Slow/dead peers dropped. See `peer_latencies_us` in the response. |
| `commit_to_federation` | local | local-only; peers commit to their own libraries |

### Out of scope for v0

- Peer auth rotation / multi-tenant ACLs (Phase 1.7).
- Conflict resolution beyond "newest wins" (Phase 1.7).
- Per-peer rate limiting / circuit breaking.
- Cross-peer pagination cursor (cursored calls fall back to local-only paging).
