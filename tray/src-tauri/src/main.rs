// Prevents an extra console window on Windows in release. DO NOT REMOVE.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

fn main() {
    // Headless doctor/CI smoke: `bourdon-tray --selftest` runs the read path and
    // exits without launching the GUI/tray.
    if std::env::args().any(|a| a == "--selftest") {
        std::process::exit(bourdon_tray_lib::selftest());
    }
    bourdon_tray_lib::run();
}
