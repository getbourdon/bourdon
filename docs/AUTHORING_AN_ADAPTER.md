# Authoring a Bourdon Adapter

This is the operational guide for adding a new agent to Bourdon's cross-agent memory federation. Use it when shipping a new entry into `~/agent-library/agents/`.

> **Canonical version of this guide** lives at `claude-brain/skills/bourdon-adapter-authoring/SKILL.md` in the Bourdon authors' shared knowledge repo and auto-loads in their Claude Code sessions. This file is the public mirror for external contributors. Behavioral content is identical; if you spot drift, the claude-brain copy wins and a sync PR against this file is welcome.

**Canonical sources (read these first when you need detail beyond what's here):**

- [`spec/ADAPTER_CONTRACT.md`](../spec/ADAPTER_CONTRACT.md) — the formal contract, semantic requirements, error semantics, idempotency guarantees, testing requirements. Comprehensive. **This guide is operational; the spec doc is normative.**
- [`spec/L5_schema.json`](../spec/L5_schema.json) — JSON Schema all manifests must validate against
- [`adapters/base.py`](../adapters/base.py) — `BourdonAdapter` Protocol, dataclasses, helpers
- [`core/l5_io.py`](../core/l5_io.py) — atomic write functions
- [`adapters/codex.py`](../adapters/codex.py) — most complete reference impl. Includes credential redaction, fallback memory recognition, MCP server enrichment. **Read this when in doubt — copy patterns rather than re-inventing.**
- [`adapters/cursor.py`](../adapters/cursor.py) — clean SQLite-based reference impl
- [`cli/main.py`](../cli/main.py) (`_handle_cursor_export`, ~line 152) — clean subparser handler reference

## Step 0 — Pick the Adapter Shape

Three shapes exist. The choice is structural.

| Shape | When | Where the code lives |
|---|---|---|
| **Native publisher** | Bourdon ships *inside* the agent (Clyde, Clair, anything we control) | Inside the agent's repo. L5 written at session close from internal state |
| **External adapter** (Python) | Agent stores native state on disk; we read it post-hoc (Claude Code, Codex, Cursor) | `bourdon/adapters/<id>.py`, registered via `[project.entry-points."bourdon.adapters"]` |
| **Platform-native plugin** | Agent has its own plugin SDK and we ship a plugin in *their* idiom (OpenClaw, future: anything with a plugin marketplace) | The agent's `extensions/` directory, published to their plugin registry. Plugin re-implements L5 write in the platform's language; uses Bourdon's L6 MCP server via stdio for query tools |

**Decision rule:** if the agent has a real plugin SDK and audience-side install pattern, ship a platform-native plugin even when an external adapter would also work — it's where adoption lives. The external adapter is a fallback when the agent has no plugin surface.

## Step 1 — Implement the Protocol (External Adapter)

For external adapters, satisfy `bourdon.adapters.base.BourdonAdapter`:

```python
from adapters.base import (
    BourdonAdapter, AgentInfo, Entity, Session, L5Manifest,
    AgentStore, HealthStatus, Visibility, VisibilityPolicy,
    AdapterDiscoveryError, apply_visibility, filter_for_federation,
    CONTRACT_VERSION,  # "0.1"
)

class MyAgentAdapter:  # implicitly satisfies the Protocol
    agent_id = "my-agent"            # kebab-case, matches L5 manifest agent.id
    agent_type = "code-assistant"    # one of the L5 schema agent.type enum values
    native_path = str(Path.home() / ".my-agent")

    def discover(self) -> AgentStore: ...
    def export_l5(self, since=None) -> L5Manifest: ...
    def export_sessions(self, since, limit=100) -> list[Session]: ...
    def health_check(self) -> HealthStatus: ...
```

Required behavior, in order of how-easy-it-is-to-get-wrong:

1. **`health_check()` MUST NOT raise.** Catch every exception inside it; convert to `HealthStatus(status="degraded", reason=str(e))`. The L6 server calls this in a tight loop.
2. **`export_l5()` MUST be idempotent.** Same native-store state → byte-identical manifest. L6 detects "anything changed" via hash comparison.
3. **Visibility filtering happens INSIDE the adapter.** L6 trusts what you emit. Use `filter_for_federation(entities, policy)` from `adapters.base` — don't roll your own filter.
4. **Errors:**
   - Native store missing → `AdapterDiscoveryError`
   - Native store present but parse failed → `AdapterExportError`
   - Native store newer/older than supported → `AdapterVersionMismatchError`
   - Anything else → catch + convert to `HealthStatus.degraded` (never propagate)

## Step 2 — Apply Credential Redaction

**Reuse — do not re-implement.** Import the patterns from `core/text_safety.py` (also re-exported from `adapters/codex.py` for backwards compatibility):

```python
from core.text_safety import _NATIVE_MEMORY_SENSITIVE_PATTERNS, _safe_native_memory_text
```

The pattern set covers `api[_-]?key`, `api[_-]?token`, `access[_-]?token`, `bearer\s+token`, `password`, `sk_live_*` (Stripe), `hf_*` (HuggingFace). `_safe_native_memory_text(value, limit=180)` does redaction, URL-strip-to-`[link]`, and 180-char truncation. **Run every string that originated from native agent state through it before it lands in an L5 field.**

If your agent has agent-specific credential patterns (e.g., a vendor-specific token prefix), extend the tuple in your adapter module:

```python
_AGENT_SENSITIVE_PATTERNS = _NATIVE_MEMORY_SENSITIVE_PATTERNS + (
    re.compile(r"\bmy_agent_token_[A-Za-z0-9]+\b"),
)
```

Then write a thin wrapper that uses the extended tuple. **Don't fork the helper function — extend the tuple and pass it in.**

## Step 3 — Atomic Write via `core/l5_io.py`

```python
from core.l5_io import write_l5  # also: write_l5_dict, read_l5_dict
from pathlib import Path

manifest = adapter.export_l5()
out_path = Path.home() / "agent-library" / "agents" / f"{adapter.agent_id}.l5.yaml"
write_l5(manifest, out_path)
```

`write_l5` does tmp + fsync + atomic rename. **Don't bypass it** with direct `yaml.safe_dump` — half-written manifests will be observed by L6's file watcher.

## Step 4 — Wire CLI

Add a subparser group in `cli/main.py`. Reference: `_handle_cursor_export` (cleanest), and the codex subparser block in `cli/main.py` (search for "codex" in the subparser-builder section) for the subparser-of-subparsers pattern. Pattern:

```python
def _handle_<id>_export(args):
    adapter = MyAgentAdapter(<args>)
    manifest = adapter.export_l5(since=_parse_since(args.since))
    data = filter_manifest_for_access(manifest, access_level=args.access_level)
    out_path = Path(args.out) if args.out else _default_<id>_l5_path()
    write_l5_dict(data, out_path)
    if args.print_manifest:
        _print_yaml(data)
    return 0

# In the subparser-builder section:
my_agent = subparsers.add_parser("<id>", help="<id>-specific commands")
my_agent_subparsers = my_agent.add_subparsers(dest="<id>_command")
export_cmd = my_agent_subparsers.add_parser("export", help=f"Build a <id> L5 manifest")
export_cmd.add_argument("--since")
export_cmd.add_argument("--out")
export_cmd.add_argument("--access-level", choices=("public", "team", "private"), default="team")
export_cmd.set_defaults(func=_handle_<id>_export)
```

For agents that need diagnosis or fallback work, mirror Codex's `doctor` / `prepare-turn` / `recognize` / `sync-native` subcommands.

## Step 5 — Register the Entry Point

Add a row to `pyproject.toml`:

```toml
[project.entry-points."bourdon.adapters"]
claude-code = "adapters.claude_code:ClaudeCodeAdapter"
codex = "adapters.codex:CodexAdapter"
cursor = "adapters.cursor:CursorAdapter"
my-agent = "adapters.my_agent:MyAgentAdapter"   # NEW
```

The entry-point name is canonical — it MUST match `agent_id`. The CLI and the L6 server discover adapters by iterating this group; no central registry.

## Step 6 — Test Suite (Mandatory Categories)

Per [`spec/ADAPTER_CONTRACT.md`](../spec/ADAPTER_CONTRACT.md) §Testing, every adapter MUST ship four test categories. Reference impl: [`tests/test_codex_adapter.py`](../tests/test_codex_adapter.py) (372 lines). Layout:

```
tests/
├── fixtures/<agent_id>/             # sample native-store content
│   ├── empty/                       # no data — discovery should still succeed
│   ├── populated/                   # realistic content for export tests
│   └── private_tagged/              # contains private-tag entities for visibility test
└── test_<agent_id>_adapter.py       # the test file
```

Test categories:

| Category | Asserts | Why |
|---|---|---|
| **Discovery** | `discover()` raises `AdapterDiscoveryError` on missing store; returns `AgentStore` on present store | Required by Protocol; smallest surface |
| **Schema conformance** | `export_l5()` against `populated/` fixture validates against `spec/L5_schema.json` via `jsonschema` | Required by spec §Data Contract |
| **Visibility** | `private_tagged/` fixture run through `export_l5()` produces NO entities with private visibility in the manifest | The PII guardrail |
| **Round-trip via L6Store** | Emit manifest → write_l5 → load via `core.l6_store.L6Store` → assert entities/sessions are present and queryable | Catches any schema-vs-loader drift |
| **Redaction** (when emitting native text) | A fixture with `password=secret123` produces `[redacted credential-like text]` in the manifest | Required when adapter surfaces native string content |

The Codex adapter test file also covers fallback memory recognition, MCP enrichment, and CLI handler smoke. Mirror those patterns where the agent has analogous surfaces.

## Step 7 — Update `docs/agent-integration-status.md`

Add a section for your agent following the existing format:

```markdown
## My Agent

Status: <one-line description>.

- `bourdon my-agent export` writes `~/agent-library/agents/my-agent.l5.yaml`.
- Intended hook: <agent>'s session-end hook (link to docs).
- Current role: <manager | reviewer | lead author | debugger | etc.> for L6 federation.
- <Any caveats or upstream blockers, e.g., "Native distilled memory blocked upstream — fallback ships and works.">
```

This file is the canonical "who has what layer running today" map. It gets read more than the spec.

## Step 8 — Choose `agent.role_narrative`

Optional but recommended L5 schema field that disambiguates agents sharing the same `agent.type` slug. Examples in tree:

| Agent | role_narrative |
|---|---|
| Claude Code | "Manages the larger picture and reviews work. Consults with subagents (Codex, Cursor) on solutions, problems, and issues via PR or Slack #agents." |
| Codex | "Lead author. Drives focused implementation tasks against well-scoped specs from Claude Code or human." |
| Cursor | "Debugger and inline-edit specialist. Used for surgical changes within a single file or for inline reasoning." |
| Cline | "Throwaway scratch-pad agent. Memory not durable; used for quick exploration." |
| Clyde | "General-purpose personal assistant outside the IDE." |

Pick the one that matches how the agent is actually used. Federated queries surface this so a user sees "Codex was lead-authoring on PRUN" not just "an agent did some work."

## Step 9 — Temporal Validity (`valid_from`, `valid_to`)

Every `Entity` supports ISO 8601 dates for `valid_from` and `valid_to`. Use them when an entity has a known lifetime — e.g., a project that wrapped, a deprecated API, an experiment that closed. Federated queries can filter by validity window; without these fields the entity is assumed active as of `last_updated`.

`Entity(name="prun-app-v1.0.4", type="release", valid_from="2026-04-15", valid_to="2026-04-23")` — useful for "what was active in April 2026?" queries.

## Step 10 — Platform-Native Plugin Variant (when applicable)

When the agent has a plugin SDK (OpenClaw is the reference case), a Bourdon **plugin** in their idiom is the right shipping shape, not an external adapter. The plugin must:

1. **Re-implement the L5 write semantics in the platform's language.** The atomic tmp+fsync+rename pattern is intentionally small (see Bourdon's `core/l5_io.py`) — port it, don't import. Cross-language calls into Python from a TS/Go plugin are wrong.
2. **Re-implement the redaction patterns.** Same regex set (`api[_-]?key`, `sk_live_*`, etc.) translated to the host language. Keep the 180-char cap.
3. **Use Bourdon's L6 MCP server for query tools.** The plugin spawns `python -m core.l6_server` as a stdio subprocess (or connects to `--transport http`) and proxies the L6 tools (`query_agent_memory`, `find_entity`, etc.) as native plugin tools. **This means the host agent's users get cross-agent recall without leaving the host.**
4. **Match the plugin's manifest contract.** OpenClaw uses `openclaw.plugin.json` with `id`, `kind`, `contracts.tools`, `configSchema`. Future plugin platforms will have different shapes — read `<platform>/AGENTS.md` and a memory plugin in their `extensions/` for the pattern.
5. **Update `docs/agent-integration-status.md` with the plugin status** (e.g., "Status: native plugin published to clawhub. Reads agent memory via plugin-sdk; exposes L6 tools as agent tools."). The Bourdon repo doesn't get a Python adapter for these — the plugin is the integration.

## Anti-patterns (from incidents)

- **Don't re-implement redaction from scratch.** The codex.py pattern set has been audited; new patterns drift.
- **Don't propagate exceptions from `health_check()`.** L6 calls it in a polling loop. A raised exception bubbles into the L6 process and crashes federation for *all* agents.
- **Don't trust L6 to filter visibility.** L6 trusts you. If your `export_l5()` emits a `private`-tagged entity, it leaks. Test the visibility category with a fixture explicitly.
- **Don't bypass `core/l5_io.py::write_l5()`.** A direct `yaml.safe_dump` to the final path will be observed half-written by readers.
- **Don't use the agent's display name for `agent_id`.** Use the kebab-case slug. `Claude Code` → `claude-code`, `Cline` → `cline`, `OpenClaw` → `openclaw`. The `agent_id` is the L5 filename.
- **Don't ship a Python adapter when the agent has a plugin SDK.** The plugin is where the install path is. The Python adapter is the fallback for SDK-less agents.

## Verification before claiming "shipped"

A new adapter is shipped when ALL of these are true:

- [ ] `pytest tests/test_<id>_adapter.py` is green (4+ test categories)
- [ ] `python -c "from importlib.metadata import entry_points; print([ep.name for ep in entry_points(group='bourdon.adapters')])"` lists the new agent
- [ ] `bourdon <id> export --print` works on the actual agent's native store and emits a manifest that validates against `L5_schema.json`
- [ ] `python -c "import yaml; from core.l6_store import L6Store; from pathlib import Path; store = L6Store(Path('~/agent-library').expanduser()); print(store.find_entity(<some entity name>, access_level='team'))"` returns the new entity from L6
- [ ] `bourdon <id> doctor` (if implemented) reports `ok` against the real native store
- [ ] `docs/agent-integration-status.md` updated
- [ ] CI green on all 12 matrix entries
- [ ] Release notes drafted

## Write-side / cloud-only agents (`commit_to_federation`)

Every adapter described above reads from a local store the agent owns (Cursor's SQLite, Codex's session_index.jsonl, Claude Code's claude-brain, Copilot/Cascade's convention file). That shape doesn't fit **cloud-only or webview-wrapper agents** — most importantly Claude Desktop, where conversations are stored server-side by Anthropic and the local disk surface is just a Chromium webview cache. The same applies to ChatGPT desktop, future Anthropic-mobile, and any other MCP-aware agent without a readable on-disk store.

For these agents, use the write-side flow instead: the agent's model calls Bourdon's `commit_to_federation` MCP tool whenever it has context worth sharing. Bourdon writes that contribution to `~/agent-library/agents/<agent_id>.l5.yaml` atomically. No adapter code, no scraping, no per-agent reverse-engineering.

### Tool signature

```python
commit_to_federation(
    agent_id: str,                    # e.g. "claude-desktop"
    agent_type: str | None = None,    # required for new manifests
    instance: str | None = None,      # optional machine/deploy id
    role_narrative: str | None = None,
    entities: list[dict] | None = None,
    sessions: list[dict] | None = None,
    mode: str = "merge",              # or "replace"
) -> dict
```

Returns a write summary: counts of rows added/updated/total, the manifest path on disk, and the new `last_updated` timestamp. On invalid input, returns a dict with an `error` key rather than raising — keeps the MCP surface predictable.

### When to call it

The model decides. Suggested triggers (in a system prompt or user instruction):

- The user mentions a project / topic worth carrying across sessions
- A decision or constraint gets settled (e.g. "we picked Postgres over MySQL because…")
- A session is about to end and the model wants to leave breadcrumbs for the next one
- A cross-agent question came up that the model thinks other agents would want to know the answer to

The convention is *under-write* — only commit things that would be useful to another agent looking back. The L5 isn't a transcript; it's the per-agent **public** glossary.

### Visibility — the agent decides

The caller is responsible for setting `visibility` on entity/session rows. Bourdon does NOT redact server-side because:

- The model is making a deliberate decision to share, not scraping a noisy native store
- Read-side adapters apply visibility filtering at query time regardless

That said, agents using this tool **should** treat anything tagged credential / financial / personal as `visibility: private` by default. The system prompt fragment that recommends `commit_to_federation` should also recommend that default.

### Merge vs replace

- **`merge`** (default) is right for cloud agents. The model adds new things this conversation learned; old context stays. Entities dedupe by `name.lower()`; sessions dedupe by `(date, cwd)`. List fields (`tags`, `aliases`, `key_actions`, `files_touched`, `project_focus`) are unioned on dupe; non-list fields are overwritten by the incoming value.
- **`replace`** wipes the manifest entirely. Use it when the agent wants explicit control over its full published view each time (e.g. "remember exactly these things and forget the rest").

### Example: Claude Desktop committing a project decision

```
User: "Let's lock in Postgres for the OMNIVour storage layer."

Model decides to commit. Calls commit_to_federation with:
  agent_id      = "claude-desktop"
  agent_type    = "code-assistant"  # only matters on first call
  entities      = [
                    {
                      "name": "OMNIVour storage decision",
                      "type": "decision",
                      "summary": "Postgres chosen over alternatives 2026-05-11. Reasons: ...",
                      "tags": ["omnivour", "architecture"],
                      "visibility": "team",
                    }
                  ]
  sessions      = [
                    {
                      "date": "2026-05-11",
                      "cwd": "/Users/you/omnivour",
                      "project_focus": ["OMNIVour"],
                      "key_actions": ["Locked in Postgres for storage layer"],
                      "visibility": "team",
                    }
                  ]
```

Next time Claude Code starts a session in `~/claudework/omnivour/`, Bourdon's recognition runtime will surface the "OMNIVour storage decision" entity and the Claude Desktop session — without anything being copy-pasted.

### What this generalizes to

This pattern is the canonical fit for **every future cloud-only or webview-based MCP-aware agent**. ChatGPT desktop, Anthropic mobile, browser-based agents, hosted agent frameworks — they all federate via the same `commit_to_federation` call. No per-agent adapter code. No filesystem reverse-engineering. The agent's model picks what to share; Bourdon persists.

For agents that *do* have a readable local store, prefer the adapter path documented above — automatic capture beats deliberate commit when you can get it.

## Paginating `list_recent_work` (issue #48)

`L6Store.list_recent_work` returns one **page** of sessions, capped at 20 by default and 100 by `MAX_LIMIT`. Adapters or external clients that want the full history walk pages via the `cursor` token in the response:

```python
from core.l6_store import L6Store
from pathlib import Path

store = L6Store(Path("~/agent-library").expanduser())
cursor = None
while True:
    page = store.list_recent_work(
        # Pass the same since/agent/access_level on every page so the
        # query stays consistent across the loop.
        since=some_datetime,
        agent="claude-code",
        access_level="team",
        limit=100,        # max per page
        cursor=cursor,    # None on the first call; carry forward on subsequent
    )
    for session in page:
        ...               # consume page.sessions
    if not page.has_more:
        break
    cursor = page.next_cursor
```

Two adapter-author pitfalls worth knowing about:

- **The 14-day default window.** If you call `list_recent_work()` with no `since` and no `cursor`, the store applies a 14-day default window. Adapter integration tests that hard-code dates older than 14 days will look like they pass locally on the day of writing and silently empty out later. Pass an explicit `since` (e.g. `datetime(2020, 1, 1, tzinfo=timezone.utc)`) in tests.
- **Pass `since` on every paginated call.** The cursor only encodes pagination offset, not the original filter. If you drop `since` after the first page, the default-window logic would re-apply mid-loop. The convention is: same filter args on every page.

The MCP surface (`bourdon serve` → `list_recent_work` tool) exposes the same parameters one-to-one and wraps the response in `{sessions, next_cursor, has_more, ...}`.

## Contributing back

If you ship an adapter, please open a PR to `getbourdon/bourdon` with the adapter module + tests + agent-integration-status update. We'll review against this guide and the spec doc. Adapters that follow this guide tend to merge in one round; adapters that skip the visibility-test category or roll their own redaction tend to need a second pass.

For very-different agent shapes (cloud-only agents like Devin/Manus that have no on-disk state, or agents whose memory model doesn't fit the Entity/Session shape cleanly), please open a Discussion before the PR — there are decisions in the contract that can flex if the use case warrants it.
