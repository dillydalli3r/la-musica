//! la musica — Tauri shell.
//!
//! The shell is a CLIENT of a la musica server. Two server stories exist:
//!
//!   * **Local (the default for a packaged install).** The shell bundles the
//!     app's own backend (`mlo-server`, a frozen Python service built by
//!     `pyinstaller/mlo-server.spec`) next to its binary, spawns it on a free
//!     loopback port and supervises it (see `backend`). The webview is
//!     pointed at THAT origin, which is the one where the session cookie
//!     works — same origin, same HttpOnly cookie, no cross-site drop.
//!   * **Remote (the classic mode).** The shell serves the React UI from
//!     `tauri://localhost` and talks HTTP to a server the user points it at:
//!     the Docker container on this machine (`127.0.0.1:8000`), another
//!     machine on the LAN, or a Tailscale name. It never starts a backend,
//!     never adopts one and never stops one — the server's lifecycle belongs
//!     to whoever runs it.
//!
//! Which mode a shell is in lives in the shell's own settings (`settings.rs`,
//! per-user `shell.json`), separate from the page's `localStorage: mlo.server`
//! so the choice exists before the webview does.
//!
//! What is left here is the desktop-only shell furniture: the tray icon, the
//! autostart registry, the native folder picker and the local backend. Android
//! and iOS compile this same crate without those — a phone has no tray and no
//! backend to own — and carry no Python at all: every mobile client is a
//! client of the same server every other client uses.

// A parking_lot Mutex for the tray checkbox state: it is locked on every tray
// menu click and never needs the poisoning dance.
#[cfg(desktop)]
use parking_lot::Mutex;

#[cfg(desktop)]
use tauri::menu::{CheckMenuItem, Menu, MenuItem, PredefinedMenuItem};
#[cfg(desktop)]
use tauri::tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent};
#[cfg(desktop)]
use tauri::{Manager, RunEvent};
// The mobile path needs `Manager` too, for the one thing it does at startup:
// look up the window from the config and show it.
#[cfg(mobile)]
use tauri::Manager;
#[cfg(desktop)]
use tauri_plugin_autostart::{MacosLauncher, ManagerExt as AutostartManagerExt};
#[cfg(desktop)]
use tauri_plugin_dialog::DialogExt;

// The iOS Now Playing star (Control Center / lock screen): MediaPlayer's
// `MPRemoteCommandCenter.likeCommand`. iOS alone has it — Android's
// now-playing notification follows the webview's Media Session, and the
// desktop targets have no such centre — so the module, and everything it
// calls, is compiled for iOS only. See src/ios_like.rs for the whole story.
#[cfg(target_os = "ios")]
mod ios_like;

// The iOS audio session (AVAudioSession, category `playback`): what keeps the
// music playing when the app is not in front, and what makes the Now Playing
// module — the card the star above is drawn on — exist at all. iOS only, for
// the same reason as the star: Android plays through its own audio path and the
// desktop targets have no AVAudioSession. See src/ios_audio.rs.
#[cfg(target_os = "ios")]
mod ios_audio;

// The desktop-only shell furniture: the bundled local backend (spawn,
// supervise, stop), the window-attach logic that points the webview at it,
// and the shell's own per-user settings.
#[cfg(desktop)]
mod backend;
#[cfg(desktop)]
mod backend_handle;
#[cfg(desktop)]
mod settings;

/// The tray's "Start on Login" checkbox, kept in managed state so the
/// click handler can re-sync its visual with the registry after toggling.
#[cfg(desktop)]
struct AutostartItem(Mutex<Option<CheckMenuItem<tauri::Wry>>>);

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
/// The webview tells the shell which music folder the local backend runs with
/// (Settings → the first-run wizard's folder step), so the next launch can
/// spawn the backend already knowing it. The server's own config remains the
/// authority; this is the shell's copy for the spawn env.
#[cfg(desktop)]
#[tauri::command]
fn set_music_folder(_app: tauri::AppHandle, folder: String) {
    let data = backend::default_app_data_dir();
    let mut settings = settings::load(&data);
    settings.music_folder = folder;
    settings::save(&data, &settings);
}

/// Open a link out in the user's own browser.
///
/// Every "leaves the app" link in the UI is an ordinary `<a target="_blank">`
/// anchor, and inside a Tauri webview those did nothing at all: a webview has
/// no tabs, and wry denies a new-window request unless a handler is installed,
/// so the click was swallowed — the report that started this. The page now
/// hands the URL here (`web/src/lib/externalLinks.ts`) and the shell asks the
/// OS to open it, which is also what makes the link land in the browser the
/// user actually browses with, session and all.
///
/// Only http/https/mailto get through. The page is our own code, but a command
/// that feeds an arbitrary string to the OS opener is a door worth keeping
/// narrow: without the check a `file:` or custom-scheme URL would be a way out
/// of the app that nothing in it asked for.
#[cfg(desktop)]
#[tauri::command]
fn open_external(url: String) -> Result<(), String> {
    let scheme = url
        .split_once(':')
        .map(|(s, _)| s.to_ascii_lowercase())
        .unwrap_or_default();
    if !matches!(scheme.as_str(), "http" | "https" | "mailto") {
        return Err(format!("refusing to open a {scheme:?} link"));
    }
    platform_open(&url)
}

/// Hand a URL to the OS opener of the platform this is built for.
///
/// No new dependency, for the same reason `main.rs` calls
/// `SetCurrentProcessExplicitAppUserModelID` by hand: on Windows this is one
/// call into an already-linked library, and on the other two it is the opener
/// every desktop already has.
#[cfg(all(desktop, target_os = "windows"))]
fn platform_open(url: &str) -> Result<(), String> {
    use std::ffi::OsStr;
    use std::os::windows::ffi::OsStrExt;

    #[link(name = "shell32")]
    extern "system" {
        fn ShellExecuteW(
            hwnd: *mut core::ffi::c_void,
            operation: *const u16,
            file: *const u16,
            parameters: *const u16,
            directory: *const u16,
            show_cmd: i32,
        ) -> *mut core::ffi::c_void;
    }
    const SW_SHOWNORMAL: i32 = 1;

    let wide = |s: &str| -> Vec<u16> {
        OsStr::new(s).encode_wide().chain(std::iter::once(0)).collect()
    };
    let operation = wide("open");
    let target = wide(url);
    // ShellExecuteW answers an HINSTANCE-shaped value that is NOT a handle:
    // anything above 32 means "started", and the value itself is never used.
    let result = unsafe {
        ShellExecuteW(
            std::ptr::null_mut(),
            operation.as_ptr(),
            target.as_ptr(),
            std::ptr::null(),
            std::ptr::null(),
            SW_SHOWNORMAL,
        )
    };
    if result as usize > 32 {
        Ok(())
    } else {
        Err(format!("the OS could not open the link ({})", result as usize))
    }
}

#[cfg(all(desktop, not(target_os = "windows")))]
fn platform_open(url: &str) -> Result<(), String> {
    let opener = if cfg!(target_os = "macos") { "open" } else { "xdg-open" };
    // Detached on purpose: the browser outlives this call, and nothing here
    // wants its exit status. The streams go to null so a chatty opener cannot
    // fill a pipe nobody drains.
    std::process::Command::new(opener)
        .arg(url)
        .stdin(std::process::Stdio::null())
        .stdout(std::process::Stdio::null())
        .stderr(std::process::Stdio::null())
        .spawn()
        .map(|_| ())
        .map_err(|e| format!("could not start {opener}: {e}"))
}

/// Tell the shell whether the track playing right now is favourited.
///
/// This is how the iOS Now Playing star (Control Center / lock screen) is kept
/// in step with the app's own hearts: the web UI calls it whenever the current
/// track or its liked state changes (`web/src/lib/iosFavs.ts`), and on iOS the
/// answer is mirrored onto `MPFeedbackCommand.active` — the OS's "the user
/// already likes this item", which is what draws the star filled rather than
/// hollow (see src/ios_like.rs).
///
/// Registered on EVERY target, with an empty body off iOS, for one reason: the
/// web UI must be able to make this call unconditionally. In a plain browser
/// `invoke` is never reached at all (the bridge is inert), while the desktop
/// and Android shells have no such star — there the call is a no-op, not an
/// error, so nothing in the player has to know which shell it is running in.
#[tauri::command]
fn set_now_playing_liked(liked: bool) {
    #[cfg(target_os = "ios")]
    ios_like::set_liked(liked);
    // Everywhere else there is nothing that mirrors the state; the argument is
    // taken (and here explicitly dropped) so the command's signature — and
    // therefore the web UI's call — is identical on all five targets.
    #[cfg(not(target_os = "ios"))]
    let _ = liked;
}

/// Tell the shell whether the player is producing sound right now.
///
/// This is the iOS audio session's input (`src/ios_audio.rs`): playback
/// STARTING activates the session, which is Apple's own guidance ("defer this
/// call until your app begins audio playback… to ensure that you won't
/// prematurely interrupt any other background audio"), and playback stopping
/// deliberately does NOT hand it back — a pause landing in the same second as a
/// start once took the session away mid-startup, which is the owner's "pressing
/// play just makes them pause immediately" (R268). The web UI calls it as the
/// player's `playing` state changes (`web/src/lib/iosAudio.ts`).
///
/// Registered on EVERY target, with an empty body off iOS, for the same reason
/// as the star's command above: the web UI calls it unconditionally, and the
/// desktop and Android shells have no such session, so the call is a no-op
/// rather than an error.
#[tauri::command]
fn set_playback_active(active: bool) {
    #[cfg(target_os = "ios")]
    ios_audio::set_playing(active);
    #[cfg(not(target_os = "ios"))]
    let _ = active;
}

/// What the shell's iOS audio state IS — the readout behind Settings →
/// Downloads & playback → Playback diagnostics.
///
/// Registered on EVERY target, exactly like the other two iOS commands: the
/// web UI calls it unconditionally whenever it is inside a Tauri shell, and
/// off iOS the list comes back empty (nothing about `AVAudioSession` exists
/// there). See `ios_audio::state` for what each row means and why a readout
/// exists at all.
#[tauri::command]
fn ios_audio_state() -> Vec<(String, String)> {
    #[cfg(target_os = "ios")]
    let state = ios_audio::state();
    #[cfg(not(target_os = "ios"))]
    let state = Vec::new();
    state
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
        if let Some(item) = state.0.lock().as_ref() {
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
        app,
        "autostart",
        "Auto-start on login",
        true,
        autostart_on,
        None::<&str>,
    )?;
    // "Keep backend running" — quitting the app leaves the local backend up
    // (so a phone or a browser can still talk to it), instead of taking it
    // down with the shell. Off by default: the backend is this shell's child,
    // and a background service the user did not ask to keep is a surprise.
    let keep_i = CheckMenuItem::with_id(
        app,
        "keep-backend",
        "Keep backend running after quit",
        true,
        false,
        None::<&str>,
    )?;
    // The local-backend row: disabled, its label is the live status. Only
    // shown while the shell owns a backend (local mode).
    let backend_status_i = MenuItem::with_id(
        app,
        "backend-status",
        "Backend: starting…",
        false,
        None::<&str>,
    )?;
    // "Use the built-in backend" — the first-run screen's question, asked
    // again from the tray. A page served by a REMOTE server cannot call the
    // shell at all (its origin is not the app's own), so this is the way back
    // to the bundled backend without editing shell.json by hand. Unchecking
    // it hands the window back to the app's own build, where the wizard asks
    // which server to talk to.
    let builtin_on = settings::load(&backend::default_app_data_dir()).is_local();
    let builtin_i = CheckMenuItem::with_id(
        app,
        "use-builtin",
        "Use the built-in backend",
        true,
        builtin_on,
        None::<&str>,
    )?;
    let quit_i = MenuItem::with_id(app, "quit", "Exit la musica", true, None::<&str>)?;
    let menu = Menu::with_items(
        app,
        &[
            &open_i,
            &autostart_i,
            &keep_i,
            &builtin_i,
            &backend_status_i,
            &PredefinedMenuItem::separator(app)?,
            &quit_i,
        ],
    )?;
    // The keep-backend checkbox must be reachable from the exit path.
    app.manage(KeepBackendItem(keep_i));
    // …and the mode checkbox from the tray handler, so the first-run screen
    // and the tray never disagree about what this shell is doing.
    app.manage(BuiltinBackendItem(builtin_i));

    if let Some(state) = app.try_state::<AutostartItem>() {
        *state.0.lock() = Some(autostart_i);
    }
    store_tray_backend_item(app, backend_status_i);

    let mut builder = TrayIconBuilder::with_id("main-tray")
        .menu(&menu)
        .tooltip("la musica")
        .on_menu_event(|app, event| match event.id.as_ref() {
            "open" => show_main_window(app),
            "autostart" => toggle_autostart(app),
            "use-builtin" => {
                let on = app
                    .try_state::<BuiltinBackendItem>()
                    .map(|s| s.0.is_checked().unwrap_or(false))
                    .unwrap_or(false);
                let _ = set_backend_mode(
                    app,
                    if on {
                        settings::BackendMode::Local
                    } else {
                        settings::BackendMode::Remote
                    },
                );
            }
            "quit" => {
                // Quitting the shell is quitting the app; the local backend
                // is a child of the shell and must go with it, unless the
                // user checked "Keep backend running after quit".
                let keep = app
                    .try_state::<KeepBackendItem>()
                    .map(|s| s.0.is_checked().unwrap_or(false))
                    .unwrap_or(false);
                if let Some(b) = app.try_state::<BackendSlot>() {
                    if let Some(backend) = b.0.lock().as_ref() {
                        if backend
                            .keep_running
                            .load(std::sync::atomic::Ordering::Relaxed)
                        {
                            // already asked to keep it; nothing to do
                        } else if !keep {
                            backend_handle::stop_backend(&backend);
                        }
                    }
                }
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

/// The tray's backend row: the disabled menu item whose text is the live
/// status, kept in managed state so the startup code and the event poll can
/// update it. The backend itself lives in `BackendSlot` beside it.
#[cfg(desktop)]
struct TrayBackendState {
    status_item: MenuItem<tauri::Wry>,
}

/// Managed state holding the running local backend, if any.
#[cfg(desktop)]
struct BackendSlot(parking_lot::Mutex<Option<std::sync::Arc<backend::LocalBackend>>>);

/// The tray's "Keep backend running after quit" checkbox, read from the exit
/// path: when checked, quitting the shell leaves the local backend up.
#[cfg(desktop)]
struct KeepBackendItem(CheckMenuItem<tauri::Wry>);

/// The tray's "Use the built-in backend" checkbox: the same question the
/// first-run screen asks, kept in managed state so the screen, the tray and
/// `shell.json` never disagree.
#[cfg(desktop)]
struct BuiltinBackendItem(CheckMenuItem<tauri::Wry>);

/// Store the status menu item for later updates.
#[cfg(desktop)]
fn store_tray_backend_item(app: &tauri::AppHandle, item: MenuItem<tauri::Wry>) {
    app.manage(TrayBackendState { status_item: item });
}

/// Update the tray's backend row text.
#[cfg(desktop)]
pub fn set_tray_backend(app: &tauri::AppHandle, text: &str) {
    if let Some(state) = app.try_state::<TrayBackendState>() {
        let _ = state.status_item.set_text(text.to_string());
    }
}

/// Point the shell's window at the backend its settings ask for — called once
/// at startup, and again whenever the answer changes (the first-run screen's
/// two buttons, the tray's "Use the built-in backend"). Answers whether the
/// local backend it was asked for came up: the first-run screen has to be able
/// to say so, rather than leave a user watching "starting…" at a server that
/// never arrived.
#[cfg(desktop)]
fn enter_backend_mode(app: &tauri::AppHandle, settings: &settings::ShellSettings) -> bool {
    if !settings.is_local() {
        // Remote — and not-yet-asked — shells both show the app's own build:
        // the page asks for an address in the first case, and which of the two
        // this install is in the second (`web/src/lib/backendShell.ts` reads
        // the mode through `shell_backend_choice`). Nothing is spawned, and
        // nothing is navigated to a server that has not been named.
        if let Some(window) = app.get_webview_window("main") {
            backend_handle::attach_remote(&window, settings, None);
        }
        set_tray_backend(
            app,
            if settings.needs_choice() {
                "Backend: not chosen yet"
            } else {
                "Backend: a server you run"
            },
        );
        return true;
    }
    let bundle = backend::BackendBundle {
        server_dir: app
            .path()
            .resource_dir()
            .unwrap_or_else(|_| std::path::PathBuf::from("."))
            .join("mlo-server"),
        app_data_dir: backend::default_app_data_dir(),
        music_dir: if settings.music_folder.is_empty() {
            None
        } else {
            Some(std::path::PathBuf::from(&settings.music_folder))
        },
    };
    let Some(slot) = app.try_state::<BackendSlot>() else {
        return false;
    };
    // OFF THE SPLASH FIRST, and this is the line that matters on a cold start.
    // The splash is a page about a server; when there IS no server (no bundled
    // backend, no free port) nothing in the shell can leave it — the window
    // used to sit on "Starting the local server…" forever with an empty tray
    // row and no process anywhere, which is exactly what it looked like. The
    // app's own build renders the reason from the state `attach_local` (or the
    // failure branch below) emits, and "no backend" is a state the page can
    // show. `enter_backend_mode` runs from `setup()`, before the webview has
    // finished loading the splash, so this navigation wins the race.
    let window = app.get_webview_window("main");
    if let Some(w) = &window {
        let _ = backend_handle::show_app_page(w);
    }
    // A backend this shell already owns is REUSED, not spawned again: the
    // question can be answered twice while the first answer's server is up,
    // and two servers over one library is the thing to avoid.
    let existing = slot.0.lock().clone();
    let backend = match existing {
        Some(b) if backend::port_of(&b).is_some() => Some(b),
        _ => backend::LocalBackend::start(&bundle),
    };
    match backend {
        Some(backend) => {
            *slot.0.lock() = Some(backend.clone());
            if let Some(window) = window {
                backend_handle::attach_local(&window, &backend, settings);
            }
            true
        }
        None => {
            // No bundled backend, or no free loopback port. The tray says so,
            // and so does the page — the window is already on the app's own
            // build by now, and a state with no origin is what it renders as
            // "the local server did not start".
            set_tray_backend(app, "Backend: unavailable");
            if let Some(w) = &window {
                backend_handle::emit_unavailable(w, settings);
            }
            false
        }
    }
}

/// What this shell has been told about its backend, for the first-run screen:
/// "local" (spawn the app's own server), "remote" (a server the user runs) or
/// "unset" — a shell that has never been asked, which is what makes that
/// screen ask instead of assuming.
///
/// `status` carries the LIVE state of a local backend ("starting" while it is
/// booting, "running" once /api/health answers, "stopped" when there is none),
/// because an event cannot be the only source of it: the shell reloads the page
/// as it enters local mode, and a fresh document has missed every `mlo-backend`
/// event the shell emitted before it. Without this, that document fell through
/// to the client wizard and asked a locally-hosted install for a server
/// ADDRESS while its own server was still booting.
#[cfg(desktop)]
#[derive(serde::Serialize)]
struct ShellChoice {
    mode: &'static str,
    music_folder: String,
    status: &'static str,
}

#[cfg(desktop)]
#[tauri::command]
fn shell_backend_choice(app: tauri::AppHandle) -> ShellChoice {
    let settings = settings::load(&backend::default_app_data_dir());
    ShellChoice {
        mode: settings.mode_str(),
        music_folder: settings.music_folder.clone(),
        status: local_status(&app, &settings),
    }
}

/// The live state of the backend this shell owns, as one word:
///
/// * `running` — a local backend is up and /api/health answers (the probe is
///   short: this runs on a page's first render, and a hung probe would hang the
///   screen it is feeding);
/// * `starting` — a local backend exists but has not answered yet, which is the
///   window the first-run screen shows its spinner for;
/// * `stopped` — local mode with no backend at all: the start failed, and the
///   screen says so and offers the ways out;
/// * `remote` — not this shell's backend to describe.
#[cfg(desktop)]
fn local_status(app: &tauri::AppHandle, settings: &settings::ShellSettings) -> &'static str {
    if !settings.is_local() {
        return "remote";
    }
    let Some(slot) = app.try_state::<BackendSlot>() else {
        return "stopped";
    };
    let port = slot.0.lock().as_ref().and_then(|b| backend::port_of(b));
    match port {
        Some(port) if backend::probe_health(port, std::time::Duration::from_millis(800)) => "running",
        Some(_) => "starting",
        None => "stopped",
    }
}

/// Record an answer and act on it. The recording comes first, so a crash
/// between the two leaves the shell asking again rather than guessing. Answers
/// whether the choice took effect (the local backend came up).
#[cfg(desktop)]
fn set_backend_mode(app: &tauri::AppHandle, mode: settings::BackendMode) -> bool {
    let dir = backend::default_app_data_dir();
    let mut settings = settings::load(&dir);
    if settings.backend_mode != mode {
        settings.backend_mode = mode;
        settings::save(&dir, &settings);
    }
    if let Some(item) = app.try_state::<BuiltinBackendItem>() {
        let _ = item.0.set_checked(mode == settings::BackendMode::Local);
    }
    enter_backend_mode(app, &settings)
}

/// The first-run screen's answer (and the tray checkbox's, which asks the same
/// question in one click). A local answer fails when the bundled backend
/// cannot be started — no resource tree, no free loopback port — and the
/// screen is told, rather than left waiting for a server that is not coming.
#[cfg(desktop)]
#[tauri::command]
fn choose_backend(app: tauri::AppHandle, mode: String) -> Result<(), String> {
    let mode = match mode.as_str() {
        "local" => settings::BackendMode::Local,
        "remote" => settings::BackendMode::Remote,
        other => return Err(format!("unknown backend mode: {other}")),
    };
    if set_backend_mode(&app, mode) {
        Ok(())
    } else {
        Err("the bundled backend did not start".to_string())
    }
}

/// Tauri entry point.
///
/// On mobile this is called by the generated Android/iOS project: the
/// `mobile_entry_point` macro emits the JNI / Objective-C glue that boots the
/// Tauri runtime and then calls this function, so it must stay public under
/// exactly this name.
#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    // ONE shell per machine, and it is the very first thing registered: a
    // second launch must reach nothing that touches state. The login
    // auto-start plus a click on the icon is the ordinary way to get two
    // shells, and two shells in local mode mean two backends writing the same
    // `<music>/.mlo/data` over each other, two servers on 8011/8012 and two
    // tray icons. The second launch instead brings the running window
    // forward — which is what a double-click on the icon was asking for.
    // Desktop only: a phone app gets one process from its OS, and the crate
    // has no mobile backend at all.
    let builder = tauri::Builder::default();
    #[cfg(desktop)]
    let builder = builder.plugin(tauri_plugin_single_instance::init(|app, _argv, _cwd| {
        if let Some(window) = app.get_webview_window("main") {
            let _ = window.unminimize();
            let _ = window.show();
            let _ = window.set_focus();
        }
    }));
    // Plugins every target has: native notifications, which the web UI sends
    // for "wish found", "download done" and "import ready" on desktop and
    // mobile alike, and the dialog plugin (its `pick_folder` command below is
    // desktop-only, but the plugin itself builds everywhere).
    let builder = builder
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_notification::init());
    // Desktop additionally owns the tray icon, the autostart registry and the
    // folder picker — all things with no mobile counterpart
    // (tauri-plugin-autostart does not even compile for Android or iOS, its
    // lib.rs is `#![cfg(not(any(target_os = "android", target_os = "ios")))]`).
    // The one command BOTH shells register is `set_now_playing_liked`: it is
    // how the web UI tells the shell what the current track's favourite state
    // is, and off iOS its body does nothing (see its docs above — the desktop
    // and Android now-playing UI is the webview's own Media Session).
    #[cfg(desktop)]
    let builder = builder
        .plugin(tauri_plugin_autostart::init(
            MacosLauncher::LaunchAgent,
            None,
        ))
        .invoke_handler(tauri::generate_handler![
            pick_folder,
            set_music_folder,
            open_external,
            set_now_playing_liked,
            set_playback_active,
            ios_audio_state,
            shell_backend_choice,
            choose_backend,
        ])
        .manage(AutostartItem(Mutex::new(None)))
        .manage(BackendSlot(parking_lot::Mutex::new(None)))
        .setup(|app| {
            // The window opens visible (tauri.conf.json `visible: true`): its
            // first run is the setup screen, which a tray-only launch would
            // put behind an icon the user has not been told about. Closing it
            // hides it to the tray, as before.
            if let Err(e) = setup_tray(app.handle()) {
                eprintln!("[mlo-desktop] tray setup failed: {e}");
            }

            // The local backend, when this shell owns one. The server's own
            // settings live in <music>/.mlo/data; the SHELL's mode (local vs
            // remote vs not asked yet) lives in per-user shell.json so it
            // exists before the webview does.
            let settings = settings::load(&backend::default_app_data_dir());
            // The tray carries the outcome at startup ("Backend: unavailable");
            // there is no screen to answer to yet.
            let _ = enter_backend_mode(app.handle(), &settings);
            Ok(())
        });

    // Mobile: the OS owns the window and there is no tray to show it from, so
    // the shell shows it exactly once, here, and never touches it again.
    // `tauri.conf.json`'s window flags are a DESKTOP concern and the mobile
    // runtime happens to ignore `visible` today (tao's iOS `Window::new`
    // carries a TODO for it, Android's `set_visible` is a no-op), so this is the
    // explicit form of a guarantee that must not rest on an upstream TODO: an
    // unseen window is a phone with no setup screen, i.e. no way to type the
    // server address that screen exists to collect. Nothing on the mobile path
    // may create, recreate, hide or reload this window — a webview torn down and
    // rebuilt is exactly the "the app keeps refreshing" a user sees as the app
    // restarting.
    //
    // The phone shells also register `set_now_playing_liked` and
    // `set_playback_active` — the same two commands the desktop shell does —
    // because those are the ones a phone needs: the favourite state the web UI
    // pushes for the OS's now-playing UI, and the player's play/pause the iOS
    // audio session follows. On Android both bodies are empty (its media
    // notification follows the webview's own Media Session, and it has no
    // AVAudioSession); on iOS they drive the star and the session registered
    // just below.
    #[cfg(mobile)]
    let builder = builder
        .invoke_handler(tauri::generate_handler![
            set_now_playing_liked,
            set_playback_active,
            ios_audio_state
        ])
        .setup(|app| {
            if let Some(window) = app.get_webview_window("main") {
                let _ = window.show();
            }
            // The audio session comes FIRST: a playback category is what keeps
            // playback alive once the app is not in front, and what makes the
            // Now Playing card — the star's home — exist at all. `configure`
            // only sets the category; the session itself is activated when the
            // web player says it is playing (`set_playback_active`), and
            // `register` keeps it that way across backgrounding, interruptions
            // and a restart of the audio server (see src/ios_audio.rs).
            // Logged, never fatal, exactly as the star is.
            #[cfg(target_os = "ios")]
            ios_audio::configure();
            #[cfg(target_os = "ios")]
            ios_audio::register();
            // iOS additionally owns the OS's Now Playing star. Setup is the one
            // place the runtime hands us the app handle before any track can
            // play, which is what the star's handler needs to reach the webview
            // (see src/ios_like.rs). A failure here is logged, never fatal:
            // losing the OS star must not cost the user the app.
            #[cfg(target_os = "ios")]
            ios_like::register(app.handle());
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
        .run(|app, event| {
            // Desktop stays alive in the tray when the last window goes away;
            // only an explicit exit (Quit menu / process kill) ends the app.
            // The local backend is a CHILD of this shell, so the shell's own
            // exit stops it — unless the user asked to keep it running. A
            // remote server is never touched: it belongs to whoever runs it.
            #[cfg(desktop)]
            if let RunEvent::ExitRequested {
                code: None, api, ..
            } = event
            {
                let keep = app
                    .try_state::<KeepBackendItem>()
                    .map(|s| s.0.is_checked().unwrap_or(false))
                    .unwrap_or(false);
                if let Some(slot) = app.try_state::<BackendSlot>() {
                    if let Some(backend) = slot.0.lock().as_ref() {
                        if !backend
                            .keep_running
                            .load(std::sync::atomic::Ordering::Relaxed)
                            && !keep
                        {
                            backend_handle::stop_backend(&backend);
                        }
                    }
                }
                api.prevent_exit();
            }
            #[cfg(not(desktop))]
            let _ = (app, event);
        });
}
