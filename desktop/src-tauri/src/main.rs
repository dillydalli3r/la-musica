// No console window on Windows — the tray icon is the interface, and a
// terminal would just sit blank while the app runs in the tray. Applies to
// dev builds too (debug logs are visible via `tauri dev`'s own terminal).
#![windows_subsystem = "windows"]

fn main() {
    mlo_desktop_lib::run()
}