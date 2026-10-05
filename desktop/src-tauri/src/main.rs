// No console window on Windows — the tray icon is the interface, and a
// terminal would just sit blank while the app runs in the tray. Applies to
// dev builds too (debug logs are visible via `tauri dev`'s own terminal).
#![windows_subsystem = "windows"]

/// The identifier `tauri.conf.json` uses, and the one the NSIS installer
/// already writes onto the Start Menu / desktop shortcuts (`PKEY_AppUserModel_ID`).
/// Kept in sync with `"identifier"` there by hand: it is one string, and the
/// process-side call below is the only place Rust names it.
#[cfg(target_os = "windows")]
const APP_USER_MODEL_ID: &str = "com.musiclibraryoptimizer.lamusica";

/// Name this process as the app the shortcuts are registered to.
///
/// Windows 11's media flyout ("now playing") labels a media session by
/// resolving its AppUserModelID to a registered shell app. Tauri sets no AUMID
/// on the process, and WebView2's Media Session carries none either, so the
/// shell had nothing to resolve and called the session "unknown app" — even
/// though the installer had already stamped the AUMID onto the shortcuts and
/// `mlo-desktop.exe` carries "la musica" in its version info. Setting the
/// explicit process AUMID is what joins the session to that registration.
///
/// Raw FFI on purpose: the alternative is a new `windows`/`windows-sys`
/// dependency for one call, and `shell32` is linked by every Windows target.
#[cfg(target_os = "windows")]
fn set_app_user_model_id() {
    use std::ffi::OsStr;
    use std::os::windows::ffi::OsStrExt;

    #[link(name = "shell32")]
    extern "system" {
        fn SetCurrentProcessExplicitAppUserModelID(app_id: *const u16) -> i32;
    }

    let wide: Vec<u16> = OsStr::new(APP_USER_MODEL_ID)
        .encode_wide()
        .chain(std::iter::once(0))
        .collect();
    // Failure is not fatal: the app runs, the flyout just keeps its generic
    // name, which is exactly the state this call exists to leave behind.
    unsafe {
        SetCurrentProcessExplicitAppUserModelID(wide.as_ptr());
    }
}

fn main() {
    #[cfg(target_os = "windows")]
    set_app_user_model_id();
    mlo_desktop_lib::run()
}