//! la musica — Tauri shell.
//!
//! The shell is a CLIENT and nothing else. It serves the React UI (web/dist)
//! out of the webview and talks HTTP to a la musica server the user points it
//! at: the Docker container on this machine (`127.0.0.1:8000`), another machine
//! on the LAN, or a Tailscale name. It never starts a backend, never adopts one
//! and never stops one — the server's lifecycle belongs to whoever runs the
//! container, and a client that could kill it would break every other client
//! talking to the same server.
//!
//! Which server that is, is a per-device setting the UI owns
//! (`localStorage: mlo.server`): a shell that has never been configured opens
//! the setup wizard instead of the app (`web/src/pages/ClientSetup.tsx`), and
//! one whose saved address does not answer lands on the sign-in screen with the
//! address field rather than rendering a shell full of failed requests.
//!
//! What is left here is the desktop-only shell furniture: the tray icon, the
//! autostart registry and the native folder picker. Android and iOS compile
//! this same crate without those — a phone has no tray and the OS owns the
//! window — and carry no Python at all: every client, desktop and mobile
//! alike, is a client of the same Docker server.

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
        app, "autostart", "Auto-start on login", true, autostart_on, None::<&str>,
    )?;
    let quit_i = MenuItem::with_id(app, "quit", "Exit la musica", true, None::<&str>)?;
    let menu = Menu::with_items(
        app,
        &[&open_i, &autostart_i, &PredefinedMenuItem::separator(app)?, &quit_i],
    )?;

    if let Some(state) = app.try_state::<AutostartItem>() {
        *state.0.lock() = Some(autostart_i);
    }

    let mut builder = TrayIconBuilder::with_id("main-tray")
        .menu(&menu)
        .tooltip("la musica")
        .on_menu_event(|app, event| match event.id.as_ref() {
            "open" => show_main_window(app),
            "autostart" => toggle_autostart(app),
            "quit" => app.exit(0),
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
        .invoke_handler(tauri::generate_handler![pick_folder, set_now_playing_liked])
        .manage(AutostartItem(Mutex::new(None)))
        .setup(|app| {
            // The window opens visible (tauri.conf.json `visible: true`): its
            // first run is the wizard's server-address screen, which a
            // tray-only launch would put behind an icon the user has not been
            // told about. Closing it hides it to the tray, as before.
            if let Err(e) = setup_tray(app.handle()) {
                eprintln!("[mlo-desktop] tray setup failed: {e}");
            }
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
    // The phone shells also register `set_now_playing_liked` — the same command
    // the desktop shell does — because that is the ONE command a phone needs:
    // the favourite state the web UI pushes for the OS's now-playing UI. On
    // Android the body is empty (its media notification follows the webview's
    // own Media Session); on iOS it drives the star registered just below.
    #[cfg(mobile)]
    let builder = builder
        .invoke_handler(tauri::generate_handler![set_now_playing_liked])
        .setup(|app| {
            if let Some(window) = app.get_webview_window("main") {
                let _ = window.show();
            }
            // The audio session comes FIRST: a playback category is what keeps
            // playback alive once the app is not in front, and what makes the
            // Now Playing card — the star's home — exist at all (see
            // src/ios_audio.rs). Logged, never fatal, exactly as the star is.
            #[cfg(target_os = "ios")]
            ios_audio::activate();
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
        .run(|_app, _event| {
            // Desktop stays alive in the tray when the last window goes away;
            // only an explicit exit (Quit menu / process kill) ends the app.
            // Nothing happens on the way out: the server is the user's own
            // container, not a child of this shell, so there is nothing here to
            // stop — and nothing of the user's to kill.
            #[cfg(desktop)]
            if let RunEvent::ExitRequested { code: None, api, .. } = _event {
                api.prevent_exit();
            }
        });
}
