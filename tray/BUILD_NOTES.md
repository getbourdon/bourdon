# Bourdon Tray — Build Notes & Compile-Time Assumptions

This scaffold was hand-written **before** the Rust + MSVC toolchain finished
installing, so **nothing here has been compiled or run** (the icons and the
Python CLI verb _were_ run and verified — see below). Every Tauri v2 API was
checked against the current official docs (versions/URLs below), but a first
`cargo build` is the real proof. This file is the checklist for that pass.

## Verified-against documentation (Tauri v2, fetched 2026-06-06)

- System tray API (`TrayIconBuilder`, `.icon`, `.menu`, `.on_menu_event`,
  `.on_tray_icon_event`, `show_menu_on_left_click`, the `tray-icon` Cargo
  feature): <https://tauri.app/learn/system-tray/>
- `TrayIcon::set_icon` / `set_tooltip` / `set_menu` signatures:
  <https://docs.rs/tauri/latest/tauri/tray/struct.TrayIcon.html>
  - `set_icon(&self, icon: Option<Image<'_>>) -> Result<()>`
  - `set_tooltip<S: AsRef<str>>(&self, tooltip: Option<S>) -> Result<()>`
- Menu constructors:
  - `CheckMenuItem::with_id(manager, id, text, enabled, checked, accelerator)`
    — <https://docs.rs/tauri/latest/tauri/menu/struct.CheckMenuItem.html>
  - `Submenu::with_items(manager, text, enabled, items: &[&dyn IsMenuItem<R>])`
    — <https://docs.rs/tauri/latest/tauri/menu/struct.Submenu.html>
  - `MenuItem::with_id`, `Menu::with_items`, `PredefinedMenuItem::quit/separator`
    — <https://tauri.app/learn/system-tray/>
- `#[tauri::command]` + `invoke_handler(generate_handler![...])` + JS
  `invoke` from `@tauri-apps/api/core`: <https://tauri.app/develop/calling-rust/>
- Window hide-on-close (`WindowEvent::CloseRequested { api, .. }` +
  `api.prevent_close()` + `window.hide()` via `.on_window_event`):
  Tauri discussions #1856 / #5334 (pattern), confirmed shape.
- `tauri.conf.json` v2 schema (productName, version, identifier, build,
  app.windows[].visible, app.withGlobalTauri, app.security.csp, bundle.icon):
  <https://tauri.app/reference/config/> and <https://schema.tauri.app/config/2>
- Capabilities/permissions (`src-tauri/capabilities/default.json`, `core:*`
  permission strings): <https://tauri.app/security/capabilities/>
- macOS accessory (no-Dock) policy via `app.set_activation_policy(
  tauri::ActivationPolicy::Accessory)` in `setup()`: confirmed current API.
- Pinned versions (crates.io / npm, 2026-06-06):
  - `tauri` = **2.11.2** (latest stable)
  - `@tauri-apps/cli` = **2.11.2**
  - `@tauri-apps/api` = **2.11.2**
  - `tauri-build` = **2.6.2** (verified latest stable on crates.io; tauri-build
    versions independently from tauri — 2.6.x pairs with tauri 2.11.x).

## Already verified by running (NOT assumptions)

- **The CLI read verb works**: `python -m cli.main agents --json` from the repo
  root exits 0 and emits `bourdon.agents/v1` with 5 agents (claude-code,
  cascade, codex, copilot, cursor); cascade/copilot/cursor have
  `freshest_session_date: null` (0 sessions). The Rust structs + health logic
  were written against this exact real output.
- **The icon generator runs**: `tray/src-tauri/icons/generate_icons.py` (Pillow
  12.2.0) produced all tray/app PNGs + `icon.ico`. Re-run it to regenerate.

## Compile-time assumptions to check (ranked by risk)

1. **`Submenu::with_items` / `Menu::with_items` element coercion** —
   PRE-EMPTIVELY HANDLED: both calls mix concrete item types (CheckMenuItem vs
   MenuItem; MenuItem/Submenu/PredefinedMenuItem), so I cast the FIRST element
   to `&dyn IsMenuItem<_>` in each slice to force the element type. This is the
   single highest-confidence "would-have-failed" spot. IF the compiler still
   objects (e.g. wants `Wry` instead of `_` for the generic, or wants every
   element cast), make the runtime generic explicit: `&dyn IsMenuItem<
   tauri::Wry>` and/or cast all elements. The `IsMenuItem` trait is imported.
2. **`Image::from_bytes`** — RESOLVED: confirmed `tauri::image::Image::from_bytes`
   + `include_bytes!` is the documented pattern, and `image-png` / `image-ico`
   are the correct Cargo feature names that gate PNG/ICO decode (png+ico moved
   behind features in tauri-apps/tauri#3588). Both features are enabled in
   Cargo.toml. Low risk now.
3. **`tray.app_handle()`** in `on_tray_icon_event` — assumed `&TrayIcon` exposes
   `.app_handle() -> &AppHandle`. If not, capture an `AppHandle` clone in the
   closure instead (move it in like the menu-event closure does).
4. **`event.id().as_ref()`** in `on_menu_event` — RESOLVED: verified against
   docs.rs that `MenuEvent` has both a public field `pub id: MenuId` and a
   method `pub fn id(&self) -> &MenuId`, and `MenuId: AsRef<str>`. So
   `event.id().as_ref()` (what I wrote) compiles. `event.id.as_ref()` (the
   tray-doc form) is equally valid. Low risk.
5. **`PredefinedMenuItem::quit(app, Some("Quit Bourdon"))`** — the predefined
   "quit" handles app exit automatically; assumed it does NOT also emit a menu
   event with id "quit" (so the `_ => {}` arm is fine). If a duplicate quit id
   surfaces, it's harmless.
6. **`State<TrayState>` / `Manager::state`** — `app.state::<TrayState>()` and
   `handle.state::<TrayState>()` assumed available via the `Manager` trait
   (imported). `App` and `AppHandle` both impl `Manager`. Should be fine.
7. **`Emitter` trait for `app.emit(...)`** — imported `tauri::Emitter`. In v2,
   `emit` moved onto the `Emitter` trait. VERIFY the import resolves; if
   `app.emit` is not found, that import is the fix.
8. **`withGlobalTauri` + `window.__TAURI__.core.invoke`** — the frontend uses
   the global injected API (no bundler). Assumed `window.__TAURI__.core.invoke`
   and `window.__TAURI__.event.listen` are the correct global paths in 2.11.
   If the window console shows `__TAURI__ is undefined`, confirm
   `app.withGlobalTauri: true` took effect and the paths (`.core`, `.event`).
9. **CSP `asset:` / `data:` img-src** — the app icon hex is pure CSS, so no
    external images are loaded; the CSP is permissive enough. If a future asset
    load is blocked, widen `img-src`.
10. **`frontendDist: "../src"` with no `devUrl`** — assumed `tauri dev` serves
    the static dir directly. If `tauri dev` insists on a dev server, add a
    trivial static server as `beforeDevCommand` + `devUrl`, or just use
    `tauri build` to validate.
11. **`crate-type` / lib+bin split** — `main.rs` calls
    `bourdon_tray_lib::run()`; the lib name is `bourdon_tray_lib` (Cargo.toml
    `[lib] name`). Verify the `#![cfg_attr(not(debug_assertions),
    windows_subsystem = "windows")]` in main.rs doesn't conflict with the lib
    entry point. Standard Tauri v2 layout — low risk.

## Things deliberately NOT done (Phase 0 scope)

- No polling / file-watching (manual refresh + focus-refresh only).
- No `tauri-plugin-shell` (std::process::Command is the exec path).
- No write/IPC-to-daemon paths.
- No lockfiles committed yet (`Cargo.lock`, `package-lock.json`) — generate and
  commit them on first successful build for reproducibility.
- The tray menu **header** item shows a static "reading…" string; live
  health/count is surfaced via the tray **tooltip** (updated on every read) and
  the window UI, because rebuilding the whole menu to relabel one item each
  refresh is more machinery than Phase 0 warrants. A later phase can swap the
  header `MenuItem` text via `set_text` if desired.

## First compile pass — suggested order

1. `cd tray/src-tauri && cargo build` — expect items #1, #2, #5, #8 to be the
   first to surface. Fix versions/casts/field-vs-method as they appear.
2. From `tray/`: `npm install` then `npm run tauri dev`.
3. Manually verify: tray icon appears grey → window-open triggers a read →
   icon swaps to green (claude-code is fresh as of the data captured) → 5 agent
   cards render, 3 showing "no sessions yet" → Recent activity panel populated →
   close window hides to tray → tray Refresh re-reads → Quit exits.
