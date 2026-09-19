//! la musica — Tauri shell.
//!
//! On desktop the shell spawns the Python FastAPI backend
//! (`server.main:app` on 127.0.0.1:8000) when the app starts and stops it on
//! exit, using the bundled `mlo-server` sidecar when there is one and the repo
//! checkout otherwise. The React UI (web/dist) is served by the Tauri webview
//! and talks to the backend over HTTP.
//!
//! On mobile (Android/iOS) that Python is *embedded* instead of spawned: the
//! build carries a CPython runtime, its standard library, the backend's own
//! sources and a site-packages tree (staged by `tools/mobile/bundle.py`), and
//! `mobile_backend.rs` starts `server.main:app` inside this process — on iOS
//! because an app cannot execute a second program at all, on Android because
//! there is no `python` executable to execute. The same port probe, the same
//! `/api/health` ownership check and the same shutdown route are used, so the
//! lifecycle guarantees do not fork with the target. What stays desktop-only is
//! the child-process machinery (a phone has no child to kill), the tray icon,
//! the autostart registry and the folder picker; and a mobile build whose bundle
//! cannot host a backend (no runtime staged, or a package missing from it) still
//! installs and still opens as the pure client it also is — the setup wizard is
//! told which of the two it has.

#[cfg(desktop)]
use std::path::PathBuf;
#[cfg(desktop)]
use std::process::{Child, Command};
#[cfg(desktop)]
use std::sync::Mutex;
#[cfg(any(desktop, mobile))]
use std::time::Duration;

#[cfg(desktop)]
use tauri::menu::{CheckMenuItem, Menu, MenuItem, PredefinedMenuItem};
#[cfg(desktop)]
use tauri::tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent};
#[cfg(desktop)]
use tauri::{Manager, RunEvent};
// The mobile path needs `Manager` too, for the one thing it does at startup:
// look up the window from the config and show it — and `RunEvent`, because the
// embedded backend is asked to exit through it.
#[cfg(mobile)]
use tauri::{Manager, RunEvent};
#[cfg(desktop)]
use tauri_plugin_autostart::{MacosLauncher, ManagerExt as AutostartManagerExt};
#[cfg(desktop)]
use tauri_plugin_dialog::DialogExt;

#[cfg(desktop)]
struct BackendState(Mutex<Option<Child>>);

/// The embedded local backend, and the only Tauri command a mobile build
/// registers: what the UI must ask before it offers "Host on this device".
#[cfg(mobile)]
mod mobile_backend;

/// The tray's "Start on Login" checkbox, kept in managed state so the
/// click handler can re-sync its visual with the registry after toggling.
#[cfg(desktop)]
struct AutostartItem(Mutex<Option<CheckMenuItem<tauri::Wry>>>);

#[cfg(any(desktop, mobile))]
pub(crate) const PORT: &str = "8000";

/// Try to locate the backend entry point.
///
/// Preference order:
///   1. bundled `mlo-server.exe` sidecar next to the app binary
///   2. `python`/`python3` on PATH running `-m uvicorn server.main:app`
///      from the repo checkout — the checkout must actually contain
///      `server/main.py`, or there is nothing to run
///
/// `None` means "no backend available"; a packaged build without the sidecar
/// has no checkout to point Python at, and spawning a bare `python` there
/// would only produce a process that dies immediately.
#[cfg(desktop)]
fn find_backend(app: &tauri::AppHandle) -> Option<(String, Vec<String>, Option<PathBuf>)> {
    // 1. bundled executable (PyInstaller one-file build of server.main)
    if let Ok(dir) = app.path().resource_dir() {
        for name in ["mlo-server.exe", "mlo-server"] {
            let cand = dir.join(name);
            if cand.is_file() {
                return Some((cand.to_string_lossy().to_string(), Vec::new(), None));
            }
        }
    }

    // 2. repo checkout: python -m uvicorn server.main:app
    let root = project_root();
    if !root.join("server").join("main.py").is_file() {
        return None;
    }
    let args = vec![
        "-m".into(),
        "uvicorn".into(),
        "server.main:app".into(),
        "--host".into(),
        "127.0.0.1".into(),
        "--port".into(),
        PORT.into(),
    ];
    Some((which_python(), args, Some(root)))
}

#[cfg(desktop)]
fn which_python() -> String {
    for cand in ["python", "python3"] {
        if let Ok(out) = Command::new(cand).arg("--version").output() {
            if out.status.success() {
                return cand.to_string();
            }
        }
    }
    "python".to_string()
}

#[cfg(desktop)]
fn project_root() -> PathBuf {
    let mut dir = std::env::current_dir().unwrap_or_default();
    // During `tauri dev` cwd is desktop/src-tauri; climb to the repo root.
    if dir.file_name().map(|n| n == "src-tauri").unwrap_or(false) {
        dir.pop();
        if dir.file_name().map(|n| n == "desktop").unwrap_or(false) {
            dir.pop();
        }
    }
    dir
}

#[cfg(any(desktop, mobile))]
pub(crate) fn backend_port_open() -> bool {
    std::net::TcpStream::connect(("127.0.0.1", 8000)).is_ok()
}

/// True only when the listener on the backend port answers like OUR backend.
///
/// A TCP connect is not ownership — any other app can hold :8000, and the
/// quit path force-kills whatever is listening there. The probe therefore
/// requires the API's own `/api/health` reply, JSON `{"status":"ok"}`, which
/// is exactly the check `start_app.py` and `tray.py` make before they adopt,
/// shut down or kill a backend. A foreign listener is left completely alone.
#[cfg(any(desktop, mobile))]
pub(crate) fn backend_is_ours() -> bool {
    use std::io::{Read, Write};
    let Ok(mut stream) = std::net::TcpStream::connect(("127.0.0.1", 8000)) else {
        return false;
    };
    let _ = stream.set_read_timeout(Some(Duration::from_secs(2)));
    let _ = stream.set_write_timeout(Some(Duration::from_secs(2)));
    let req = "GET /api/health HTTP/1.1\r\nHost: 127.0.0.1:8000\r\n\
               Connection: close\r\n\r\n";
    if stream.write_all(req.as_bytes()).is_err() {
        return false;
    }
    // A non-UTF-8 body is not our API's JSON; failing the probe is the safe
    // direction (nothing gets adopted or killed over a guess).
    let mut buf = String::new();
    if stream.read_to_string(&mut buf).is_err() {
        return false;
    }
    if !buf.starts_with("HTTP/1.1 200") && !buf.starts_with("HTTP/1.0 200") {
        return false;
    }
    buf.contains("\"status\":\"ok\"") || buf.contains("\"status\": \"ok\"")
}

/// Ask a running backend to exit via the env-gated shutdown endpoint
/// (works for backends this shell didn't spawn, e.g. after a restart).
///
/// Only ever called after `backend_is_ours()` said the listener is ours — a
/// foreign app on the port never receives this POST.
#[cfg(any(desktop, mobile))]
pub(crate) fn request_backend_shutdown() {
    use std::io::{Read, Write};
    if let Ok(mut stream) = std::net::TcpStream::connect(("127.0.0.1", 8000)) {
        let req = "POST /api/shutdown HTTP/1.1\r\nHost: 127.0.0.1:8000\r\n\
                   Content-Length: 0\r\nConnection: close\r\n\r\n";
        let _ = stream.write_all(req.as_bytes());
        let mut buf = String::new();
        let _ = stream.set_read_timeout(Some(Duration::from_secs(2)));
        let _ = stream.read_to_string(&mut buf);
    }
}

/// Force-kill whatever process is LISTENING on the backend port (last
/// resort for backends spawned without MLO_ALLOW_SHUTDOWN=1). Windows only.
///
/// Refuses unless the port answers /api/health as ours: a foreign app on
/// :8000 must never be force-killed.
#[cfg(all(desktop, windows))]
fn kill_port_listener() {
    if !backend_is_ours() {
        eprintln!("[mlo-desktop] :8000 is not our backend — left alone");
        return;
    }
    use std::os::windows::process::CommandExt;
    let out = Command::new("netstat")
        .arg("-aon")
        .creation_flags(0x08000000)
        .output();
    if let Ok(out) = out {
        let text = String::from_utf8_lossy(&out.stdout);
        for line in text.lines() {
            if !line.contains("LISTENING") {
                continue;
            }
            let parts: Vec<&str> = line.split_whitespace().collect();
            if parts.len() >= 5 && parts[1].ends_with(":8000") {
                if let Some(pid) = parts.last() {
                    let _ = Command::new("taskkill")
                        .args(["/F", "/PID", pid])
                        .creation_flags(0x08000000)
                        .output();
                }
            }
        }
    }
}

#[cfg(desktop)]
fn spawn_backend(app: &tauri::AppHandle) {
    // Adopt an already-running backend (tray app, previous run) instead of
    // spawning a duplicate that fails to bind and leaves confusion behind.
    if backend_is_ours() {
        println!("[mlo-desktop] adopting already-running backend");
        return;
    }
    // Something else holds the port: it is not ours, so it is neither adopted
    // nor (on quit) killed — say so instead of spawning a doomed backend.
    if backend_port_open() {
        let msg = format!(
            "Port {PORT} is in use by another app (not la musica) — stop that \
             app or free the port. Nothing was started or stopped."
        );
        eprintln!("[la musica] {msg}");
        app.dialog()
            .message(msg)
            .title("la musica — port busy")
            .blocking_show();
        return;
    }
    let (exe, args, cwd) = match find_backend(app) {
        Some(found) => found,
        None => {
            let msg = "la musica cannot start its backend: no bundled mlo-server \
                       next to the app binary and no server/main.py in the working \
                       directory. Install the packaged build, or launch from the \
                       repository checkout.";
            eprintln!("[la musica] {msg}");
            app.dialog()
                .message(msg)
                .title("la musica — backend not found")
                .blocking_show();
            return;
        }
    };
    let mut cmd = Command::new(&exe);
    cmd.args(&args);
    if let Some(dir) = cwd {
        cmd.current_dir(dir);
    }
    cmd.env("MLO_ALLOW_SHUTDOWN", "1");
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        cmd.creation_flags(0x08000000); // CREATE_NO_WINDOW
    }
    match cmd.spawn() {
        Ok(child) => {
            if let Some(state) = app.try_state::<BackendState>() {
                *state.0.lock().unwrap() = Some(child);
            }
            println!("[mlo-desktop] backend spawned: {exe}");
        }
        Err(e) => eprintln!("[mlo-desktop] failed to start backend ({exe}): {e}"),
    }
}

#[cfg(desktop)]
fn stop_backend(app: &tauri::AppHandle) {
    if let Some(state) = app.try_state::<BackendState>() {
        if let Some(mut child) = state.0.lock().unwrap().take() {
            let _ = child.kill();
            let _ = child.wait();
            println!("[mlo-desktop] backend stopped");
        }
    }
    // Also stop any backend on the port that we didn't spawn (adopted or
    // orphaned from an earlier run) so Quit really clears the background —
    // but only when the listener is ours: a foreign app is left running.
    if backend_is_ours() {
        request_backend_shutdown();
        std::thread::sleep(Duration::from_millis(700));
    }
    #[cfg(windows)]
    kill_port_listener();
}

/// Native folder picker (also reachable from the web UI via invoke when
/// running inside the Tauri webview).
///
/// Desktop only: mobile has no folder to pick, and the dialog plugin's
/// blocking folder API does not exist there at all — only `blocking_pick_file`
/// does — so registering this command would not even compile for Android/iOS.
#[cfg(desktop)]
#[tauri::command]
fn pick_folder(app: tauri::AppHandle) -> Option<String> {
    app.dialog()
        .file()
        .blocking_pick_folder()
        .and_then(|p| p.into_path().ok())
        .map(|p| p.to_string_lossy().to_string())
}

/// Show and focus the main window (tray click / tray menu "Open").
#[cfg(desktop)]
fn show_main_window(app: &tauri::AppHandle) {
    if let Some(win) = app.get_webview_window("main") {
        let _ = win.unminimize();
        let _ = win.show();
        let _ = win.set_focus();
    }
}

#[cfg(desktop)]
fn sync_autostart_item(app: &tauri::AppHandle) {
    let enabled = app.autolaunch().is_enabled().unwrap_or(false);
    if let Some(state) = app.try_state::<AutostartItem>() {
        if let Some(item) = state.0.lock().unwrap().as_ref() {
            let _ = item.set_checked(enabled);
        }
    }
}

#[cfg(desktop)]
fn toggle_autostart(app: &tauri::AppHandle) {
    let autolaunch = app.autolaunch();
    // Flip according to the registry (the source of truth), then re-sync
    // the checkbox with whatever actually happened.
    let result = if autolaunch.is_enabled().unwrap_or(false) {
        autolaunch.disable()
    } else {
        autolaunch.enable()
    };
    if let Err(e) = result {
        eprintln!("[mlo-desktop] autostart toggle failed: {e}");
    }
    sync_autostart_item(app);
}

#[cfg(desktop)]
fn setup_tray(app: &tauri::AppHandle) -> tauri::Result<()> {
    let open_i = MenuItem::with_id(app, "open", "Open la musica", true, None::<&str>)?;
    let autostart_on = app.autolaunch().is_enabled().unwrap_or(false);
    let autostart_i = CheckMenuItem::with_id(
        app, "autostart", "Auto-start on login", true, autostart_on, None::<&str>,
    )?;
    let quit_i = MenuItem::with_id(app, "quit", "Exit (stop backend)", true, None::<&str>)?;
    let menu = Menu::with_items(
        app,
        &[&open_i, &autostart_i, &PredefinedMenuItem::separator(app)?, &quit_i],
    )?;

    if let Some(state) = app.try_state::<AutostartItem>() {
        *state.0.lock().unwrap() = Some(autostart_i);
    }

    let mut builder = TrayIconBuilder::with_id("main-tray")
        .menu(&menu)
        .tooltip("la musica")
        .on_menu_event(|app, event| match event.id.as_ref() {
            "open" => show_main_window(app),
            "autostart" => toggle_autostart(app),
            "quit" => {
                // RunEvent::Exit stops the backend.
                app.exit(0);
            }
            _ => {}
        })
        .on_tray_icon_event(|tray, event| {
            if let TrayIconEvent::Click {
                button: MouseButton::Left,
                button_state: MouseButtonState::Up,
                ..
            } = event
            {
                show_main_window(tray.app_handle());
            }
        });
    // Explicit compile-time-embedded icon: never depend on how the bundler
    // resolved default_window_icon (this was showing a stale tray icon).
    match tauri::image::Image::from_bytes(include_bytes!("../icons/icon.png")) {
        Ok(icon) => {
            builder = builder.icon(icon);
        }
        Err(e) => {
            eprintln!("[mlo-desktop] embedded tray icon failed to decode: {e}");
            if let Some(icon) = app.default_window_icon() {
                builder = builder.icon(icon.clone());
            }
        }
    }
    // Left click opens the window; the menu lives on right click.
    builder = builder.show_menu_on_left_click(false);
    builder.build(app)?;
    Ok(())
}

/// Tauri entry point.
///
/// On mobile this is called by the generated Android/iOS project: the
/// `mobile_entry_point` macro emits the JNI / Objective-C glue that boots the
/// Tauri runtime and then calls this function, so it must stay public under
/// exactly this name.
#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    // Plugins every target has: native notifications, which the web UI sends
    // for "wish found", "download done" and "import ready" on desktop and
    // mobile alike, and the dialog plugin (its `pick_folder` command below is
    // desktop-only, but the plugin itself builds everywhere).
    let builder = tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_notification::init());

    // Desktop additionally owns the backend lifecycle, the tray icon, the
    // autostart registry and the folder picker — all things with no mobile
    // counterpart (tauri-plugin-autostart does not even compile for Android or
    // iOS, its lib.rs is `#![cfg(not(any(target_os = "android", target_os =
    // "ios")))]`). Mobile therefore registers no commands at all: the login
    // screen asks for the server address, nothing asks for a folder.
    #[cfg(desktop)]
    let builder = builder
        .plugin(tauri_plugin_autostart::init(
            MacosLauncher::LaunchAgent,
            None,
        ))
        .invoke_handler(tauri::generate_handler![pick_folder])
        .manage(BackendState(Mutex::new(None)))
        .manage(AutostartItem(Mutex::new(None)))
        .setup(|app| {
            let handle = app.handle().clone();
            std::thread::spawn(move || {
                // Give the backend a moment to boot before the UI polls it.
                std::thread::sleep(Duration::from_secs(2));
                spawn_backend(&handle);
            });
            // The app opens to the tray: the main window starts hidden
            // (visible: false in tauri.conf.json) and is shown from the
            // tray icon / menu.
            if let Err(e) = setup_tray(app.handle()) {
                eprintln!("[mlo-desktop] tray setup failed: {e}");
            }
            Ok(())
        });

    // Mobile: the OS owns the window and there is no tray to show it from, so
    // the shell shows it exactly once, here, and never touches it again.
    // `visible: false` in tauri.conf.json is a DESKTOP concern — the desktop
    // app opens into the tray and is shown on demand — and the mobile runtime
    // happens to ignore that flag today (tao's iOS `Window::new` carries a TODO
    // for it, Android's `set_visible` is a no-op), so this is the explicit form
    // of a guarantee that must not rest on an upstream TODO: an unseen window is
    // a phone with no login screen, i.e. no way to type the server address that
    // screen exists to collect. Nothing on the mobile path may create, recreate,
    // hide or reload this window — a webview torn down and rebuilt is exactly
    // the "the app keeps refreshing" a user sees as the app restarting.
    //
    // The same hook starts the embedded backend. It is deliberately *after* the
    // window is shown: the interpreter's initialisation is millisecond-scale and
    // the API's first import is not, so the login/setup screen must be able to
    // paint while the server is still coming up, and `backend_info` answers
    // `starting` until it does.
    #[cfg(mobile)]
    let builder = builder
        .manage(mobile_backend::State::default())
        .invoke_handler(tauri::generate_handler![
            mobile_backend::backend_info,
            mobile_backend::set_backend_keepalive
        ])
        .setup(|app| {
            if let Some(window) = app.get_webview_window("main") {
                let _ = window.show();
            }
            mobile_backend::start(app.handle().clone());
            Ok(())
        });

    builder
        .on_window_event(|_window, _event| {
            // Desktop: closing the window hides it to the tray — the app keeps
            // running (and the icon stays) until Quit is used. Mobile has no
            // tray to restore the window from and the OS owns window
            // lifecycle, so nothing is intercepted there.
            #[cfg(desktop)]
            if let tauri::WindowEvent::CloseRequested { api, .. } = _event {
                api.prevent_close();
                let _ = _window.hide();
            }
        })
        .build(tauri::generate_context!())
        .expect("error while building la musica")
        .run(|_app, _event| {
            // Desktop stays alive in the tray when the last window goes away;
            // only an explicit exit (Quit menu / process kill) ends the app —
            // and that exit is where the spawned backend gets stopped. Mobile
            // has no child process to stop, but the embedded backend still gets
            // asked to exit cleanly on the way out.
            #[cfg(desktop)]
            match _event {
                RunEvent::ExitRequested { code: None, api, .. } => api.prevent_exit(),
                RunEvent::Exit => stop_backend(_app),
                _ => {}
            }
            #[cfg(mobile)]
            if let RunEvent::Exit = _event {
                mobile_backend::stop(_app);
            }
        });
}