# Plan 002: Show live agent activity in the tray (process count → session registry)

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving to the
> next step. If anything in the "STOP conditions" section occurs, stop and
> report — do not improvise. When done, update the status row for this plan
> in `plans/README.md`.
>
> **Drift check (run first)**:
> `git diff --stat 7b4dc50..HEAD -- tray/src-tauri/src/lib.rs tray/src/main.js core/agents_export.py core/l6_server.py core/federation_registry.py`
> If any of these files changed since this plan was written, compare the
> "Current state" excerpts below against the live code before proceeding;
> on a mismatch, treat it as a STOP condition.

## Status

- **Priority**: P2
- **Effort**: Phase A = S, Phase B = L
- **Risk**: Phase A = LOW, Phase B = MEDIUM
- **Depends on**: none (Phase A); Phase B optionally rides the radlab-cloud always-on serve host
- **Category**: feature
- **Planned at**: commit `7b4dc50`, 2026-06-30

## Why this matters

The tray today renders **one row per agent identity** — one `claude-code`, one
`codex` — regardless of how many CLI sessions of each are actually running. A
user with 2 claude-code sessions and 3 codex sessions still sees `1` and `1`.

This is **correct by design** (the tray is a roster of which agent *types* have
published memory, not a process monitor) but it is a recurring source of "is
this thing even seeing my sessions?" confusion. The ask is to surface live
activity. There are two honest ways to do it, at very different cost/fidelity:

- **Phase A — live process count.** Count OS processes per agent. Cheap,
  tray-local, no protocol change. Coarse: counts PIDs, not Bourdon sessions;
  local-only; heuristic matching.
- **Phase B — live session registry.** Each Bourdon session heartbeats into a
  presence store; the tray shows live *logical* sessions, source-tagged and
  federated. The correct model; real plumbing across core + every participant.

They **share the tray badge UI**, so Phase A's frontend work is not throwaway —
Phase B swaps only the data source behind the same badge. Ship A first for an
immediate "something's running" signal, then let B supersede it.

## Current state (excerpts — verify against drift check)

**Tray reads one row per L5 manifest.** `tray/src-tauri/src/lib.rs`:

```rust
// CLI_BASE_ARGS (~:63) — the only invocation; argv array, no shell.
const CLI_BASE_ARGS: &[&str] = &["-m", "cli.main", "agents", "--json"];

// Agent struct (~:113) ALREADY carries `instance` — currently unused in the UI.
pub struct Agent {
    pub id: String,
    pub instance: Option<String>,   // ~:119  <-- per-instance seam for Phase B
    pub session_count: Option<i64>, // committed sessions in the manifest, NOT live
    // ...
}

// read_and_apply (~:420) is where every result is assembled before the
// frontend sees it — the merge point for any live-activity field.
```

**Contract producer.** `core/agents_export.py::summarize_agent_manifest`:

```python
"instance": _redact_field(str(agent.get("instance") or "")) or None,  # ~:149
"session_count": len(visible_sessions),                               # ~:159
```

`export_local_agents` (~:196) emits `bourdon.agents/v1`; the `--federated`
path adds per-machine source tags.

**Frontend row.** `tray/src/main.js::buildAgentRow` (~:154) renders the row;
`renderOverview` (~:197) sets the `agent-count` element (~:198) from
`agents.length`. The live badge lands here.

**Daemon surface for Phase B.** `bourdon serve` → `core/l6_server.py`
(`run_l6_server`, FastMCP) + `core/federation_registry.py::FederationRegistry`
(per-agent tokens, `list_agents()`, `has_active_agents()`). The federated
fan-out already exists: `prepare_recognition_context_federated` (l6_server.py
~:216) calls each peer — live sessions become cross-machine for free if each
peer exposes its registry.

---

## Phase A — Live process count (tray-only, no protocol change)

**Goal:** each row shows a pip + "N live" = OS processes of that CLI running now
on this machine.

### A1. Add the process-scan dependency
- Add `sysinfo` to `tray/src-tauri/Cargo.toml`. Reads the process table
  directly — **no `pgrep`, no shell**, preserving the lib.rs exec-safety posture.

### A2. Signature table + scan
- In `lib.rs`, add a const map agent-id → exe/argv patterns:
  ```rust
  const PROCESS_SIGNATURES: &[(&str, &[&str])] =
      &[("claude-code", &["claude"]), ("codex", &["codex", "codex-cli"])];
  ```
- `fn live_counts() -> HashMap<String, usize>`: enumerate processes, match each
  against the table (case-insensitive substring on exe name + argv[0]).
- Keep the table small and explicit; an unknown agent simply gets no badge.

### A3. Merge into the contract
- Add `live_process_count: Option<usize>` to the `Agent` struct (default `None`
  so the JSON contract stays backward compatible).
- In `read_and_apply` (~:420), after parsing the report, overlay
  `live_counts()` onto each agent by `id`. This is the single merge point.

### A4. Render the badge
- In `buildAgentRow` (main.js ~:154), when `live_process_count > 0`, render a
  green pip + `${n} live`. Reuse the existing `agent-pulse` styling.

### A5. (optional) Health nudge
- A "live process but stale manifest" agent could bias toward Yellow in
  `compute_health` — defer unless it reads cleanly; not required for A.

**Phase A trade-offs (document in the row tooltip):** counts PIDs not sessions;
can't distinguish interactive vs `-automations`; local-only; brittle if a
wrapper renames the binary. Honest answer to "is anything running?", nothing
finer.

---

## Phase B — Live session registry (core + participants + tray)

**Goal:** live *logical* Bourdon sessions, source-tagged and federated, using
the `instance` field that already exists end-to-end.

### B1. Ephemeral presence store (separate from durable L5)
- New `~/.bourdon/live_sessions.json` (or an L6 store table) keyed by
  `(agent_id, instance_id)` → `{started_at, last_heartbeat, host, pid,
  project_focus}`.
- **Liveness = TTL**: a row is live iff `now − last_heartbeat < HEARTBEAT_TTL`
  (~90s). TTL is what reaps crash-without-deregister — ghosts self-expire.
- This store is **never** federated as durable memory; it is presence only.

### B2. Heartbeat producer (BourdonParticipant protocol += 3 methods)
- Add `register_session` / `heartbeat` / `deregister_session` to the participant
  protocol (see `docs/AUTHORING_A_PARTICIPANT.md`).
- Wire to hooks that already fire: SessionStart → register; periodic tick (cron
  or the existing export cadence) → heartbeat; SessionEnd (already runs
  `bourdon export`) → deregister.
- **Two transports, pick by daemon presence:**
  - **(a) Daemon** — if `bourdon serve` runs (LaunchAgent, like
    `com.radlab.litestream-mac`), POST to a new `/session/heartbeat` tool on the
    L6 server. Real-time, robust.
  - **(b) Daemon-free** — write-through to `live_sessions.json` with a file lock
    + TTL. Honors Phase 0's "zero daemon" posture; "live" is only as fresh as
    the last write × last tray refresh.
- Start with (b) for parity with current architecture; (a) is the robust target.

### B3. Contract extension
- `summarize_agent_manifest` joins the presence store: add
  `live_sessions: [{instance, host, started_at, project_focus, age_s}]` and
  `live_count` per agent row.
- The `--federated` path already source-tags per machine, so live sessions
  become **cross-machine** once each peer exposes its registry — no new
  fan-out code, just include the presence join in the peer response.

### B4. Tray rendering
- Identity row shows `live_count` (supersedes Phase A's `live_process_count`
  behind the same badge component).
- Click-to-expand into **per-instance child rows** using `Agent.instance`
  (lib.rs ~:119) / the `live_sessions` array.
- A live session = a distinct "live" pip in `pulseClass`, separate from the
  freshness dot.

**Phase B trade-offs:** every participant must implement 3 protocol methods;
needs either daemon-as-LaunchAgent or a heartbeat cadence; up-to-TTL ghost
window on hard crash; federated liveness needs peers reachable.

**Tie-in:** Phase B's daemon transport is the natural tenant of the planned
`radlab-cloud` always-on Bourdon federation server — host the presence registry
there and liveness survives local machines sleeping / the NAS going offline.

---

## Verification

**Phase A**
1. `cd tray && cargo build` — compiles with `sysinfo`.
2. Start 2 `claude` CLIs + 1 `codex`. Launch the tray (or
   `cargo run -- --selftest` once A3 exposes the field in JSON).
3. Expect `claude-code` row badge = `2 live`, `codex` = `1 live`. Kill one
   claude, Refresh → `1 live`.
4. `cargo run -- --selftest` JSON still parses (backward-compatible field).

**Phase B**
1. Unit: a registered session appears in `live_sessions.json`; after
   `HEARTBEAT_TTL` with no heartbeat it is absent from the join.
2. `python -m cli.main agents --json` includes `live_sessions`/`live_count`.
3. `python -m cli.main agents --json --federated` carries peer live sessions
   with correct `source`/`source_kind` tags.
4. SessionEnd deregisters; a `kill -9`'d session disappears within TTL.

## STOP conditions

- The drift check shows any listed file changed and the excerpt no longer
  matches — re-read the live code before touching it.
- Phase A: more than one distinct binary legitimately maps to one agent id and
  the signature table can't disambiguate without false positives — stop and
  decide the matching policy with Ry before shipping a misleading count.
- Phase B: adding the 3 protocol methods would force a breaking change to the
  BourdonParticipant Protocol that ripples to all existing participants — stop
  and version the protocol deliberately, do not silently break adapters.
- Any change would make `bourdon agents --json` emit a non-backward-compatible
  contract (the tray Rust struct must still parse older/newer payloads).
