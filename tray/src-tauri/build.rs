fn main() {
    // Tauri v2 build script. Generates the runtime context (config, capability
    // schemas under gen/schemas/, embedded assets) consumed by
    // tauri::generate_context!() in lib.rs. Must run before the crate compiles.
    tauri_build::build();
}
