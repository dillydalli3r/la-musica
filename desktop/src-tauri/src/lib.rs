//! la musica — Tauri desktop shell.
//!
//! Spawns the Python FastAPI backend (`server.main:app` on 127.0.0.1:8000)
//! when the app starts and stops it on exit, using the bundled `mlo-server`
//! sidecar when there is one and the repo checkout otherwise. The React UI
//! (web/dist) is served by the Tauri webview and talks to the backend over
//! HTTP.

use std::path::PathBuf;
use std::process::{Child, Command};
use std::sync::Mutex;
use std::time::Duration;

use tauri::menu::{CheckMenuItem, Menu, MenuItem, PredefinedMenuItem};
use tauri::tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent};
use tauri::{Manager, RunEvent};
use tauri_plugin_autostart::{MacosLauncher, ManagerExt as AutostartManagerExt};
use tauri_plugin_dialog::DialogExt;

struct BackendState(Mutex<Option<Child>>);

/// The tray's "Start on Login" checkbox, kept in managed state so the
/// click handler can re-sync its visual with the registry after toggling.
struct AutostartItem(Mutex<Option<CheckMenuItem<tauri::Wry>>>);

const PORT: &str = "8000";

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

fn backend_port_open() -> bool {
    std::net::TcpStream::connect(("127.0.0.1", 8000)).is_ok()
}

/// Ask a running backend to exit via the env-gated shutdown endpoint
/// (works for backends this shell didn't spawn, e.g. after a restart).
fn request_backend_shutdown() {
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
#[cfg(windows)]
fn kill_port_listener() {
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

fn spawn_backend(app: &tauri::AppHandle) {
    // Adopt an already-running backend (tray app, previous run) instead of
    // spawning a duplicate that fails to bind and leaves confusion behind.
    if backend_port_open() {
        println!("[mlo-desktop] adopting already-running backend");
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

fn stop_backend(app: &tauri::AppHandle) {
    if let Some(state) = app.try_state::<BackendState>() {
        if let Some(mut child) = state.0.lock().unwrap().take() {
            let _ = child.kill();
            let _ = child.wait();
            println!("[mlo-desktop] backend stopped");
        }
    }
    // Also stop any backend on the port that we didn't spawn (adopted or
    // orphaned from an earlier run) so Quit really clears the background.
    if backend_port_open() {
        request_backend_shutdown();
        std::thread::sleep(Duration::from_millis(700));
    }
    #[cfg(windows)]
    if backend_port_open() {
        kill_port_listener();
    }
}

/// Native folder picker (also reachable from the web UI via invoke when
/// running inside the Tauri webview).
#[tauri::command]
fn pick_folder(app: tauri::AppHandle) -> Option<String> {
    app.dialog()
        .file()
        .blocking_pick_folder()
        .and_then(|p| p.into_path().ok())
        .map(|p| p.to_string_lossy().to_string())
}

/// Show and focus the main window (tray click / tray menu "Open").
fn show_main_window(app: &tauri::AppHandle) {
    if let Some(win) = app.get_webview_window("main") {
        let _ = win.unminimize();
        let _ = win.show();
        let _ = win.set_focus();
    }
}

fn sync_autostart_item(app: &tauri::AppHandle) {
    let enabled = app.autolaunch().is_enabled().unwrap_or(false);
    if let Some(state) = app.try_state::<AutostartItem>() {
        if let Some(item) = state.0.lock().unwrap().as_ref() {
            let _ = item.set_checked(enabled);
        }
    }
}

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

pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_autostart::init(
            MacosLauncher::LaunchAgent,
            None,
        ))
        .manage(BackendState(Mutex::new(None)))
        .manage(AutostartItem(Mutex::new(None)))
        .invoke_handler(tauri::generate_handler![pick_folder])
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
        })
        .on_window_event(|window, event| {
            // Closing the window hides it to the tray; the app keeps
            // running (and the icon stays) until Quit is used.
            if let tauri::WindowEvent::CloseRequested { api, .. } = event {
                api.prevent_close();
                let _ = window.hide();
            }
        })
        .build(tauri::generate_context!())
        .expect("error while building la musica")
        .run(|app, event| match event {
            // Stay alive in the tray when the last window goes away; only
            // an explicit exit (Quit menu / process kill) ends the app.
            RunEvent::ExitRequested { code: None, api, .. } => {
                api.prevent_exit();
            }
            RunEvent::Exit => stop_backend(app),
            _ => {}
        });
}