// Prevents an extra console window on Windows in release builds. Does nothing on
// other platforms. The real logic lives in the library crate so the mobile
// entry point and `generate_context!` share one code path.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

fn main() {
    tcg_desktop_lib::run()
}
