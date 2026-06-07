//! Bourdon Desktop Tray — Phase 0 (read-only) backend.
//!
//! Licensed under the Business Source License 1.1 (see ../../LICENSE).
//! Bourdon's tray is server-side of the MIT-cli / BSL-server boundary.
//!
//! Responsibilities:
//!   1. Invoke the Bourdon read verb (`bourdon agents --json`) via an ARGV
//!      ARRAY using std::process::Command — never a shell string. This is the
//!      whole exec-safety posture: no `sh -c`, no interpolation of any value
//!      into a command line.
//!   2. Parse the stable `bourdon.agents/v1` JSON contract into typed structs.
//!   3. Compute a health state (Grey/Green/Yellow/Red) from the parsed data.
//!   4. Expose `get_agents` as a #[tauri::command] for the frontend to render.
//!   5. Drive the system tray: a state-colored icon + a menu, and swap the
//!      icon whenever the health changes.
//!   6. Hide-to-tray on window close (tray-first app; closing != quitting).
//!
//! Phase 0 has ZERO daemon, ZERO IPC backend, ZERO polling. Refresh is manual
//! (tray "Refresh" item, or the frontend's Refresh button) plus a refresh on
//! window show.

use std::process::Command;
use std::sync::Mutex;

use serde::Serialize;
use tauri::image::Image;
use tauri::menu::{
    CheckMenuItem, IsMenuItem, Menu, MenuItem, PredefinedMenuItem, Submenu,
};
use tauri::tray::{TrayIcon, TrayIconBuilder};
use tauri::{App, AppHandle, Emitter, Manager, WindowEvent};

// ===========================================================================
// CONFIG POINT — how the tray invokes the Bourdon read CLI.
// ===========================================================================
//
// This is the SINGLE swappable place the CLI invocation lives. To point the
// tray at a different Bourdon install (e.g. the global `bourdon` once it is no
// longer stale, or a packaged binary), change ONLY this block.
//
// On this dev machine the global `bourdon` is a STALE build, so we invoke the
// repo's CLI module directly: `python -m cli.main agents --json` with the
// working directory set to the repo root. The argv is a fixed array — no part
// of it is ever derived from untrusted input.
//
// SECURITY: program + args are constants. We pass them to Command as a vector;
// std::process::Command does NOT use a shell, so there is no word-splitting,
// globbing, or metacharacter interpretation. This is the required posture.

/// Python executable. Override with `BOURDON_PYTHON` to point at a venv that has
/// the `mcp` deps required for `--federated` peer calls; defaults to bare
/// `python` (sufficient for local-only reads). The token for peers is read by
/// the CLI from the environment this process inherits (e.g. `BOURDON_PEER_TOKEN_*`).
fn cli_program() -> String {
    std::env::var("BOURDON_PYTHON")
        .ok()
        .filter(|s| !s.is_empty())
        .unwrap_or_else(|| "python".to_string())
}

/// Fixed argv tail (argv[1..]). Never interpolate runtime values here.
/// `--federated` is appended for the federated read only.
const CLI_BASE_ARGS: &[&str] = &["-m", "cli.main", "agents", "--json"];

/// Working directory for the CLI process. The repo root, because
/// `python -m cli.main` must resolve the `cli` package from there.
///
/// Override at runtime with the `BOURDON_REPO_DIR` env var (handy when the
/// tray binary is launched from outside the repo). Falls back to this literal,
/// which is correct for `npm run tauri dev` launched from `tray/`.
fn repo_root() -> std::path::PathBuf {
    if let Ok(dir) = std::env::var("BOURDON_REPO_DIR") {
        if !dir.is_empty() {
            return std::path::PathBuf::from(dir);
        }
    }
    // Dev default: this crate lives at <repo>/tray/src-tauri, so two parents up
    // is the repo root. CARGO_MANIFEST_DIR is baked at compile time.
    let manifest_dir = std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    manifest_dir
        .parent() // <repo>/tray
        .and_then(|p| p.parent()) // <repo>
        .map(|p| p.to_path_buf())
        .unwrap_or(manifest_dir)
}

/// Freshness window: activity newer than this many days counts as "fresh".
/// (Build plan §5 default = 7 days.)
const FRESHNESS_WINDOW_DAYS: i64 = 7;

/// Tray icon asset bytes, embedded at compile time. One per health state.
/// Paths are relative to this source file (src/), so ../icons/.
const ICON_GREY: &[u8] = include_bytes!("../icons/tray-grey.png");
const ICON_GREEN: &[u8] = include_bytes!("../icons/tray-green.png");
const ICON_YELLOW: &[u8] = include_bytes!("../icons/tray-yellow.png");
const ICON_RED: &[u8] = include_bytes!("../icons/tray-red.png");

// ===========================================================================
// Typed contract — bourdon.agents/v1
// ===========================================================================

#[derive(Debug, Clone, Serialize, serde::Deserialize)]
pub struct RecentActivity {
    pub date: String,
    #[serde(default)]
    pub project_focus: Vec<String>,
    #[serde(default)]
    pub key_actions: Vec<String>,
    #[serde(default)]
    pub visibility: String,
}

#[derive(Debug, Clone, Serialize, serde::Deserialize)]
pub struct Agent {
    pub id: String,
    #[serde(default)]
    pub r#type: Option<String>,
    #[serde(default)]
    pub instance: Option<String>,
    #[serde(default)]
    pub role_narrative: Option<String>,
    #[serde(default)]
    pub last_updated: Option<String>,
    #[serde(default)]
    pub capability_count: Option<i64>,
    #[serde(default)]
    pub session_count: Option<i64>,
    /// NULL for agents with 0 sessions — the frontend must render that
    /// gracefully ("no sessions yet"), never as a blank/NaN.
    #[serde(default)]
    pub freshest_session_date: Option<String>,
    #[serde(default)]
    pub recent_activity: Vec<RecentActivity>,
    /// Per-manifest parse failure. The CLI still exits 0 in this case; the row
    /// is rendered as an error row, not a crash.
    #[serde(default)]
    pub parse_error: Option<String>,
    /// Federation: which machine this agent came from, and local vs peer.
    /// Present on every agent in `--federated` output; the local path tags it
    /// with this machine's name. Must be carried on the Rust struct or it is
    /// dropped on the round-trip to the frontend.
    #[serde(default)]
    pub source: Option<String>,
    #[serde(default)]
    pub source_kind: Option<String>,
}

/// One federated source machine (local or a peer).
#[derive(Debug, Clone, Serialize, serde::Deserialize)]
pub struct Source {
    pub name: String,
    #[serde(default)]
    pub kind: String,
    #[serde(default)]
    pub reachable: bool,
    #[serde(default)]
    pub agent_count: i64,
}

#[derive(Debug, Clone, Serialize, serde::Deserialize)]
pub struct AgentsReport {
    pub schema: String,
    #[serde(default)]
    pub generated_from: String,
    /// Local-mode: this machine's name. (Federated mode omits it in favor of `sources`.)
    #[serde(default)]
    pub machine: Option<String>,
    #[serde(default)]
    pub agents: Vec<Agent>,
    /// Federated mode: per-machine summary (local + each peer, reachable?).
    #[serde(default)]
    pub sources: Option<Vec<Source>>,
}

/// Health state machine (build plan §5).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum Health {
    /// 0 agents — installed, nothing published yet. Benign; visually distinct
    /// from red.
    Grey,
    /// >=1 agent, no parse_error anywhere, freshest activity within window.
    Green,
    /// >=1 agent but stale beyond window, OR some (not all) agents parse_error.
    Yellow,
    /// CLI exited nonzero (dir unreadable) OR every agent has parse_error.
    Red,
}

/// The full result the frontend renders, plus the computed health the tray
/// uses. `error` is populated only for the Red CLI-failure path (so the UI can
/// show stderr); on success it is None and `report` is Some.
#[derive(Debug, Clone, Serialize)]
pub struct AgentsResult {
    pub health: Health,
    pub report: Option<AgentsReport>,
    /// Human-facing error detail (CLI stderr or parse error) for Red states.
    pub error: Option<String>,
    /// The exact command we ran, for the UI's diagnostics/empty state.
    pub command: String,
}

/// Tray handle stored in Tauri state so menu/command handlers can swap the icon.
struct TrayState(Mutex<Option<TrayIcon>>);

// ===========================================================================
// Date helpers (no chrono dependency — the contract dates are ISO-ish strings)
// ===========================================================================

/// Parse a `YYYY-MM-DD` (optionally with a trailing `T...` time) into a day
/// number (days since 1970-01-01, proleptic Gregorian). Returns None if the
/// leading 10 chars are not a valid date. We only need day granularity for the
/// freshness window, so time/zone are ignored deliberately.
fn parse_ymd_to_days(s: &str) -> Option<i64> {
    let date_part = s.get(0..10)?;
    let mut it = date_part.split('-');
    let y: i64 = it.next()?.parse().ok()?;
    let m: i64 = it.next()?.parse().ok()?;
    let d: i64 = it.next()?.parse().ok()?;
    if !(1..=12).contains(&m) || !(1..=31).contains(&d) {
        return None;
    }
    // Howard Hinnant's days_from_civil algorithm.
    let y = if m <= 2 { y - 1 } else { y };
    let era = if y >= 0 { y } else { y - 399 } / 400;
    let yoe = (y - era * 400) as i64; // [0, 399]
    let doy = (153 * (if m > 2 { m - 3 } else { m + 9 }) + 2) / 5 + d - 1; // [0,365]
    let doe = yoe * 365 + yoe / 4 - yoe / 100 + doy; // [0, 146096]
    Some(era * 146097 + doe - 719468)
}

/// Today as a day number, in UTC, derived from SystemTime (no chrono).
fn today_days() -> i64 {
    use std::time::{SystemTime, UNIX_EPOCH};
    let secs = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs() as i64)
        .unwrap_or(0);
    secs / 86_400
}

// ===========================================================================
// Health computation
// ===========================================================================

fn compute_health(report: &AgentsReport) -> Health {
    let agents = &report.agents;
    if agents.is_empty() {
        return Health::Grey;
    }

    let any_ok = agents.iter().any(|a| a.parse_error.is_none());
    if !any_ok {
        // Every agent has a parse_error → can't read memory.
        return Health::Red;
    }

    let any_error = agents.iter().any(|a| a.parse_error.is_some());

    // Freshest activity across all agents that have a freshest_session_date.
    let today = today_days();
    let freshest_age: Option<i64> = agents
        .iter()
        .filter_map(|a| a.freshest_session_date.as_deref())
        .filter_map(parse_ymd_to_days)
        .map(|d| today - d)
        .min(); // smallest age = most recent

    let fresh = match freshest_age {
        // Newest activity is within the window (negative age = future date,
        // also "fresh"). None = no agent has any session date at all → stale.
        Some(age) => age <= FRESHNESS_WINDOW_DAYS,
        None => false,
    };

    if fresh && !any_error {
        Health::Green
    } else {
        // Stale beyond window, or a partial parse_error among otherwise-fine
        // agents → usable but attention warranted.
        Health::Yellow
    }
}

// ===========================================================================
// CLI invocation (argv array — NO shell)
// ===========================================================================

fn run_cli(federated: bool) -> AgentsResult {
    let program = cli_program();
    let mut args: Vec<&str> = CLI_BASE_ARGS.to_vec();
    if federated {
        args.push("--federated");
    }
    let cmd_str = format!("{} {}", program, args.join(" "));

    // SECURITY: argv array, fixed constants, no shell. cwd is a path constant.
    // The process inherits this app's environment (so BOURDON_PEER_TOKEN_* and
    // BOURDON_PYTHON flow through); no value is interpolated into a shell line.
    let output = Command::new(&program)
        .args(&args)
        .current_dir(repo_root())
        .output();

    let output = match output {
        Ok(o) => o,
        Err(e) => {
            // CLI not found / not executable → Red.
            return AgentsResult {
                health: Health::Red,
                report: None,
                error: Some(format!(
                    "Failed to launch Bourdon CLI ({program}): {e}.\n\
                     Is Python on PATH and is the repo at the configured root?"
                )),
                command: cmd_str,
            };
        }
    };

    // Exit code 2 = agents dir missing/unreadable (the contract's only nonzero).
    // Any nonzero → Red, surface stderr.
    if !output.status.success() {
        let stderr = String::from_utf8_lossy(&output.stderr).trim().to_string();
        let code = output
            .status
            .code()
            .map(|c| c.to_string())
            .unwrap_or_else(|| "signal".into());
        return AgentsResult {
            health: Health::Red,
            report: None,
            error: Some(format!(
                "Bourdon CLI exited with status {code}.\n{}",
                if stderr.is_empty() {
                    "(no stderr output)"
                } else {
                    &stderr
                }
            )),
            command: cmd_str,
        };
    }

    let stdout = String::from_utf8_lossy(&output.stdout);
    match serde_json::from_str::<AgentsReport>(&stdout) {
        Ok(report) => {
            let health = compute_health(&report);
            AgentsResult {
                health,
                report: Some(report),
                error: None,
                command: cmd_str,
            }
        }
        Err(e) => {
            // Malformed JSON despite exit 0 → Red (can't trust the read).
            AgentsResult {
                health: Health::Red,
                report: None,
                error: Some(format!("Could not parse CLI JSON output: {e}")),
                command: cmd_str,
            }
        }
    }
}

// ===========================================================================
// Tray icon swap
// ===========================================================================

fn icon_bytes_for(health: Health) -> &'static [u8] {
    match health {
        Health::Grey => ICON_GREY,
        Health::Green => ICON_GREEN,
        Health::Yellow => ICON_YELLOW,
        Health::Red => ICON_RED,
    }
}

fn health_label(health: Health) -> &'static str {
    match health {
        Health::Grey => "No agents published yet",
        Health::Green => "Memory present & fresh",
        Health::Yellow => "Usable — attention warranted",
        Health::Red => "Can't read memory",
    }
}

/// Swap the tray icon + tooltip to match a health state. Best-effort: logged on
/// failure but never panics (a tray-update failure must not crash the app).
fn apply_health_to_tray(app: &AppHandle, health: Health, agent_count: usize) {
    let state = app.state::<TrayState>();
    let guard = match state.0.lock() {
        Ok(g) => g,
        Err(_) => return,
    };
    if let Some(tray) = guard.as_ref() {
        match Image::from_bytes(icon_bytes_for(health)) {
            Ok(img) => {
                let _ = tray.set_icon(Some(img));
            }
            Err(e) => eprintln!("tray: failed to decode icon for {health:?}: {e}"),
        }
        let tip = format!(
            "Bourdon — {} ({} agent{})",
            health_label(health),
            agent_count,
            if agent_count == 1 { "" } else { "s" }
        );
        let _ = tray.set_tooltip(Some(tip));
    }
}

// ===========================================================================
// Tauri commands
// ===========================================================================

/// Run a read, update the tray icon to match the health, return the result.
fn read_and_apply(app: &AppHandle, federated: bool) -> AgentsResult {
    let result = run_cli(federated);
    let agent_count = result
        .report
        .as_ref()
        .map(|r| r.agents.len())
        .unwrap_or(0);
    apply_health_to_tray(app, result.health, agent_count);
    result
}

/// Frontend-facing local read (fast — this machine only). Default on load.
#[tauri::command]
fn get_agents(app: AppHandle) -> AgentsResult {
    read_and_apply(&app, false)
}

/// Frontend-facing federated read (this machine + peers, source-tagged). Slower
/// (network fan-out) — invoked only when the user switches to the Federated scope.
#[tauri::command]
fn get_agents_federated(app: AppHandle) -> AgentsResult {
    read_and_apply(&app, true)
}

/// Show + focus the main window (used by tray "Open Bourdon" and left-click).
#[tauri::command]
fn show_main_window(app: AppHandle) {
    if let Some(win) = app.get_webview_window("main") {
        let _ = win.show();
        let _ = win.set_focus();
        let _ = win.unminimize();
    }
}

// ===========================================================================
// Tray construction
// ===========================================================================

fn build_tray(app: &App) -> tauri::Result<()> {
    let handle = app.handle();

    // --- menu items ---------------------------------------------------------
    // Header line: a disabled item that shows health + agent count. Rebuilt on
    // each refresh would require rebuilding the menu; for Phase 0 we keep a
    // static header and lean on the tooltip + window UI for live counts. The
    // header text is set to a neutral starting value.
    let header = MenuItem::with_id(
        app,
        "header",
        "Bourdon — reading memory…",
        false, // disabled (informational)
        None::<&str>,
    )?;

    let open_i = MenuItem::with_id(app, "open", "Open Bourdon", true, None::<&str>)?;
    let refresh_i = MenuItem::with_id(app, "refresh", "Refresh", true, None::<&str>)?;

    // Scope submenu: "This machine" (checked), "Federated" (disabled w/ hint).
    let scope_local = CheckMenuItem::with_id(
        app,
        "scope_local",
        "This machine",
        true,  // enabled
        true,  // checked
        None::<&str>,
    )?;
    let scope_federated = MenuItem::with_id(
        app,
        "scope_federated",
        "Federated — all peers  (Configure a peer to enable)",
        false, // DISABLED — Phase 0 has no federation surface in the tray
        None::<&str>,
    )?;
    // NOTE: the elements have different concrete types (CheckMenuItem vs
    // MenuItem), so array-literal unsizing won't infer &dyn IsMenuItem — we
    // cast the first element explicitly to fix the slice's element type.
    let scope_submenu = Submenu::with_items(
        app,
        "Scope",
        true,
        &[
            &scope_local as &dyn IsMenuItem<_>,
            &scope_federated,
        ],
    )?;

    let sep1 = PredefinedMenuItem::separator(app)?;
    let sep2 = PredefinedMenuItem::separator(app)?;
    let quit_i = PredefinedMenuItem::quit(app, Some("Quit Bourdon"))?;

    // Mixed concrete types (MenuItem / Submenu / PredefinedMenuItem) — cast the
    // first element so the slice resolves to &[&dyn IsMenuItem<_>].
    let menu = Menu::with_items(
        app,
        &[
            &header as &dyn IsMenuItem<_>,
            &sep1,
            &open_i,
            &scope_submenu,
            &refresh_i,
            &sep2,
            &quit_i,
        ],
    )?;

    // --- tray icon ----------------------------------------------------------
    // Start grey (unknown / not-yet-read). get_agents() on window show will
    // swap it to the real state.
    let start_icon = Image::from_bytes(ICON_GREY)?;

    let tray = TrayIconBuilder::with_id("bourdon-tray")
        .icon(start_icon)
        .tooltip("Bourdon — reading memory…")
        .menu(&menu)
        .show_menu_on_left_click(false)
        .on_menu_event(move |app, event| match event.id().as_ref() {
            "open" => {
                show_main_window(app.clone());
            }
            "refresh" => {
                // Re-run the read; this updates the tray icon as a side effect
                // and pushes a fresh payload to the frontend via an event so an
                // open window re-renders. (Tray refresh = local/fast.)
                let result = run_cli(false);
                let count = result
                    .report
                    .as_ref()
                    .map(|r| r.agents.len())
                    .unwrap_or(0);
                apply_health_to_tray(app, result.health, count);
                let _ = app.emit("agents-refreshed", &result);
            }
            // "quit" handled by PredefinedMenuItem::quit automatically.
            _ => {}
        })
        .on_tray_icon_event(|tray, event| {
            // Left-click the tray icon opens the window (common desktop idiom).
            use tauri::tray::{MouseButton, MouseButtonState, TrayIconEvent};
            if let TrayIconEvent::Click {
                button: MouseButton::Left,
                button_state: MouseButtonState::Up,
                ..
            } = event
            {
                show_main_window(tray.app_handle().clone());
            }
        })
        .build(app)?;

    // Stash the tray so commands can swap its icon later.
    handle.state::<TrayState>().0.lock().unwrap().replace(tray);
    Ok(())
}

// ===========================================================================
// App entry point
// ===========================================================================

/// Headless self-test / doctor: run the read path, print the full result as
/// JSON plus a one-line summary, and return a process exit code (0 unless
/// health is Red). Invoked via `bourdon-tray --selftest` — NO GUI, no tray.
/// Useful for CI smoke, and for diagnosing the CLI wiring on a fresh machine
/// without launching the desktop app.
pub fn selftest() -> i32 {
    let result = run_cli(false);
    let count = result
        .report
        .as_ref()
        .map(|r| r.agents.len())
        .unwrap_or(0);
    match serde_json::to_string_pretty(&result) {
        Ok(j) => println!("{j}"),
        Err(e) => eprintln!("selftest: could not serialize result: {e}"),
    }
    eprintln!(
        "selftest: health={:?} agents={} command=[{}]",
        result.health, count, result.command
    );
    if result.health == Health::Red {
        1
    } else {
        0
    }
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .manage(TrayState(Mutex::new(None)))
        .invoke_handler(tauri::generate_handler![
            get_agents,
            get_agents_federated,
            show_main_window
        ])
        .setup(|app| {
            // macOS: tray-first app should not own a Dock icon. Accessory
            // policy = menubar/tray presence only. Set at runtime (robust to
            // schema drift; verified API: app.set_activation_policy in setup).
            #[cfg(target_os = "macos")]
            {
                let _ = app.set_activation_policy(tauri::ActivationPolicy::Accessory);
            }
            build_tray(app)?;
            Ok(())
        })
        .on_window_event(|window, event| {
            // Tray-first app: closing the window HIDES it instead of quitting.
            // Quit is only via the tray menu's Quit item.
            if let WindowEvent::CloseRequested { api, .. } = event {
                api.prevent_close();
                let _ = window.hide();
            }
        })
        .run(tauri::generate_context!())
        .expect("error while running Bourdon tray application");
}
