# Bourdon Desktop Tray (Phase 0)

The first GUI for [Bourdon](../README.md) — a local, cross-agent memory tool.
This tray makes the otherwise-invisible memory layer **visible**.

Phase 0 is **read-only**: zero IPC backend, zero daemon, zero write paths. It
reads agent data by shelling out to a single Bourdon CLI verb and displays it.
A manual **Refresh** (tray menu or window button) and a refresh-on-window-focus
are the only data triggers — no polling, no file watching.

## What you see

- A **system-tray icon** whose color encodes memory health (grey / green /
  yellow / red). The icon swaps live as state changes.
- A **tray menu**: a health/agent-count tooltip, **Open Bourdon**, a **Scope**
  submenu (_This machine_ ✓ / _Federated — all peers_ — disabled until you
  configure a peer), **Refresh**, and **Quit**.
- A **main window** (hidden on launch — this is a tray-first app): a health
  badge + scope switcher, an **agent list** (id, type, instance, role, last
  touched, capability/session counts; agents with 0 sessions show "no sessions
  yet", broken manifests show an error row, never a crash), and an aggregated
  **Recent activity** panel (every agent's recent activity merged, date-desc,
  capped ~30, each row showing date · agent · focus · first action · a
  visibility badge).

Closing the window **hides it to the tray** rather than quitting. Quit only via
the tray's **Quit** item (or, on macOS, the app runs as an _accessory_ — no Dock
icon).

## Prerequisites

- **Rust + MSVC toolchain** (Windows) / Xcode CLT (macOS) / webkit2gtk (Linux) —
  the standard Tauri v2 prerequisites: <https://v2.tauri.app/start/prerequisites/>
- **Node.js** (for the Tauri CLI) — `npm install` pulls `@tauri-apps/cli`.
- **Python 3** on `PATH` — the tray invokes the Bourdon read verb via
  `python -m cli.main agents --json` (see the config point below).

## Run it (dev)

From this `tray/` directory:

```bash
npm install
npm run tauri dev
```

`tauri dev` compiles the Rust backend and serves the static frontend in
`tray/src/` directly (no separate dev server — `frontendDist` points at the
static files). The window starts hidden; click the tray icon or the tray's
**Open Bourdon** item.

## Build a bundle

```bash
npm run tauri build
```

## The CLI config point (swappable)

The tray invokes the Bourdon read CLI from a **single clearly-marked constant
block** at the top of [`src-tauri/src/lib.rs`](src-tauri/src/lib.rs):

```rust
const CLI_PROGRAM: &str = "python";
const CLI_ARGS: &[&str] = &["-m", "cli.main", "agents", "--json"];
// cwd = repo root (CARGO_MANIFEST_DIR/../.., or $BOURDON_REPO_DIR override)
```

- **Why `python -m cli.main` and not `bourdon`?** On the dev machine the global
  `bourdon` resolves to a **stale** build, so the tray runs the repo's CLI
  module directly with the working directory set to the repo root.
- To repoint at a packaged `bourdon` binary later, change **only** that block
  (e.g. `CLI_PROGRAM = "bourdon"`, `CLI_ARGS = ["agents", "--json"]`, and adjust
  the cwd logic).
- Override the repo root at runtime with the `BOURDON_REPO_DIR` env var.

**Security:** the CLI is invoked via an **argv array** through Rust
`std::process::Command` — never a shell string, never `sh -c`, never any runtime
value interpolated into a command line. That is the entire write/exec-safety
posture for Phase 0. No `tauri-plugin-shell` is used, so the capability set
grants no shell/exec/fs/network permissions to the frontend.

## Health states

| State  | Meaning                                                        | Trigger                                                                 |
| ------ | ------------------------------------------------------------- | ----------------------------------------------------------------------- |
| Grey   | Installed, nothing published yet (benign — _not_ an error)    | `agents: []`                                                            |
| Green  | Present + fresh                                               | ≥1 agent, no parse errors, freshest activity within 7 days              |
| Yellow | Usable but attention warranted                               | ≥1 agent but stale beyond 7 days, OR some (not all) agents parse-error  |
| Red    | Can't read memory                                            | CLI exits nonzero (dir unreadable), OR every agent has a parse error    |

The freshness window (7 days) is the `FRESHNESS_WINDOW_DAYS` constant in
`lib.rs`.

## License

BSL 1.1 — see [`LICENSE`](LICENSE) (server-side of the MIT-cli / BSL-server
boundary).

See [`BUILD_NOTES.md`](BUILD_NOTES.md) for the compile-time assumptions and
known-risky spots (this was scaffolded before the Rust toolchain was available).
