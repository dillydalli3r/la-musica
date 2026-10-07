// No console window on Windows — the tray icon is the interface, and a
// terminal would just sit blank while the app runs in the tray. Applies to
// dev builds too (debug logs are visible via `tauri dev`'s own terminal).
#![windows_subsystem = "windows"]

/// The identifier `tauri.conf.json` uses, and the one the NSIS installer
/// already writes onto the Start Menu / desktop shortcuts (`PKEY_AppUserModel_ID`).
/// Kept in sync with `"identifier"` there by hand: it is one string, and this
/// file is the only place Rust names it (both calls below take it as given).
#[cfg(target_os = "windows")]
const APP_USER_MODEL_ID: &str = "com.musiclibraryoptimizer.lamusica";

fn main() {
    // The process's identity, before the runtime creates a single window:
    // Windows 11's media flyout resolves the media session's
    // `SourceAppUserModelId` to a registered shell app, and this id is what
    // makes the card read "la musica" rather than "Unknown app" — the whole
    // story (and why the webview cannot supply it) is in `win_media`.
    #[cfg(target_os = "windows")]
    mlo_desktop_lib::win_media::name_the_process(APP_USER_MODEL_ID);
    // …and the name that id resolves to, in the per-user AUMID registry. A
    // packaged install already resolves through the Start Menu shortcut the
    // installer stamped with the same id; a dev build has no shortcut at all,
    // and this key is what keeps its card from being anonymous for a
    // different reason.
    #[cfg(target_os = "windows")]
    mlo_desktop_lib::win_media::register_shell_name(APP_USER_MODEL_ID, "la musica");
    mlo_desktop_lib::run()
}