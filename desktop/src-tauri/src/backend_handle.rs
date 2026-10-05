//! Backend-driven window state: where the webview is pointed, and when.
//!
//! In local mode the webview is NOT pointed at `tauri://localhost` (the
//! bundled asset protocol): it is pointed at the shell's own backend —
//! `http://127.0.0.1:<port>` — because that is the origin the session cookie
//! lives on. The first paint therefore has to WAIT for the backend to be
//! healthy, which is what `attach_local` does: probe /api/health until it
//! answers 200, then load the URL and tell the page the shell is running its
//! own local server.
//!
//! The page needs to know three facts the webview alone cannot see:
//!   * the backend is the SHELL's own (a user could also point at a remote
//!     server)  — so Settings can say so and offer to stop it;
//!   * the port it is on — so the service worker can cache media streams
//!     from the right origin instead of a hardcoded one;
//!   * the working state (starting / running / stopped) — so the first paint
//!     is not a page full of failed requests.
//! All three travel as a small `mlo-backend` event the page listens for.

use std::sync::atomic::Ordering;
use std::sync::Arc;

use tauri::{Emitter, WebviewWindow};

use super::backend::{probe_health, LocalBackend};
use super::settings::ShellSettings;

/// The event name the web side listens for (`web/src/lib/backendShell.ts`).
pub const BACKEND_EVENT: &str = "mlo-backend";

/// How long a cold backend gets to answer before we give up and paint a page
/// that will fail (the page then says "start your server" instead of hanging).
const READY_TIMEOUT: std::time::Duration = std::time::Duration::from_secs(90);

#[derive(Clone, Copy, PartialEq, Eq)]
pub enum BackendStatus {
    Starting,
    Running,
    Stopped,
}

impl BackendStatus {
    pub fn as_str(self) -> &'static str {
        match self {
            BackendStatus::Starting => "starting",
            BackendStatus::Running => "running",
            BackendStatus::Stopped => "stopped",
        }
    }
}

/// The payload the page sees for one `mlo-backend` event.
#[derive(Clone, serde::Serialize)]
pub struct BackendState<'a> {
    pub status: &'a str,
    pub origin: &'a str,
    pub port: u16,
    /// "local" | "remote" | "unset" — how this shell was told to find a
    /// server (`unset` = never asked).
    pub mode: &'a str,
    /// True while the shell has not been asked yet: the first-run screen owns
    /// the window and no backend has been spawned.
    pub needs_choice: bool,
    /// True while the shell is in local mode and the backend it spawned is
    /// still running (as opposed to a remote server the user pointed at).
    pub local: bool,
}

/// Point *window* at the shell's local backend.
///
/// *window* is the main window; it starts on the static splash
/// (`/splash.html`, part of the built SPA) and this replaces that with the
/// real URL — but ONLY once `probe_health` says the backend answers. Doing it
/// the other way (navigate immediately) is exactly the brief "couldn't reach
/// 127.0.0.1" page WebView2 paints before the server has finished booting: a
/// dead-URL navigation, then a good one. Waiting turns that into a blank-ish
/// splash for a couple of seconds and then the app, with no error page in
/// between.
pub fn attach_local(window: &WebviewWindow, backend: &Arc<LocalBackend>, settings: &ShellSettings) {
    let Some(port) = backend_port(backend) else {
        return;
    };
    let origin = format!("http://127.0.0.1:{port}");

    // Tell the page we are coming (status: starting), then wait for health.
    emit_state(window, backend, settings, port, BackendStatus::Starting);
    let win = window.clone();
    let b = Arc::clone(backend);
    let s = settings.clone();
    std::thread::spawn(move || {
        let start = std::time::Instant::now();
        while !probe_health(port, std::time::Duration::from_secs(1)) {
            if start.elapsed() >= READY_TIMEOUT {
                emit_state(&win, &b, &s, port, BackendStatus::Stopped);
                return;
            }
            std::thread::sleep(std::time::Duration::from_millis(300));
        }
        // Healthy: NOW swap the splash for the real app.
        if let Ok(url) = origin.parse() {
            let _ = win.navigate(url);
        }
        emit_state(&win, &b, &s, port, BackendStatus::Running);
        // A poll keeps the tray status truthful if the backend dies later.
        let win2 = win.clone();
        let b2 = Arc::clone(&b);
        let s2 = s.clone();
        std::thread::spawn(move || loop {
            std::thread::sleep(std::time::Duration::from_secs(5));
            let alive = probe_health(port, std::time::Duration::from_millis(800));
            let status = if alive {
                BackendStatus::Running
            } else {
                BackendStatus::Stopped
            };
            emit_state(&win2, &b2, &s2, port, status);
        });
    });
}

/// Point the window at the app's own build — away from the boot splash.
///
/// The splash (`/splash.html`) is a page about a server starting; it has no
/// navigation of its own. So every path that will NOT end in a running server
/// has to leave it explicitly: remote mode (the wizard asks for an address),
/// the first-run question (the chooser needs the page), and a local start that
/// failed (the page renders the reason). `index.html` is served by the shell's
/// own asset protocol — an app origin, never loopback — which is exactly what
/// makes the page treat itself as a CLIENT shell with setup still to do
/// (`web/src/lib/clientSetup.ts::isClientShell`).
pub fn show_app_page(window: &WebviewWindow) {
    // `on_page_load` is a one-shot hook registered on the window builder, so it
    // cannot be attached here — the window already exists. What CAN be done at
    // any time is a navigation guarded by the load event: this call installs an
    // initialization script that runs in the splash document and moves it to
    // `index.html` on the spot. `window.navigate` from JS keeps the Tauri
    // context (an `<a>` click would not), and an absolute path is used so a
    // future splash at a nested path cannot resolve it relative to itself.
    let _ = window.eval(
        "if (!window.__mloLeavingSplash) { window.__mloLeavingSplash = 1; window.location.replace('index.html'); }",
    );
}

/// Tell a page that the local backend this shell was asked for is NOT running.
///
/// `status: "stopped"` with no origin is the one state the page cannot
/// misread: a shell in local mode with no server. It is emitted on the failure
/// path of `enter_backend_mode`, after the window has been sent to the app's
/// own build, so the user sees that sentence instead of an endless
/// "Starting the local server…".
pub fn emit_unavailable(window: &WebviewWindow, settings: &ShellSettings) {
    let state = BackendState {
        status: BackendStatus::Stopped.as_str(),
        origin: "",
        port: 0,
        mode: settings.mode_str(),
        needs_choice: settings.needs_choice(),
        local: true,
    };
    let _ = window.emit(BACKEND_EVENT, state);
}

/// The window is pointed at a REMOTE server (the classic mode): emit the
/// state once so the page knows this shell did not spawn the backend.
pub fn attach_remote(window: &WebviewWindow, settings: &ShellSettings, port_hint: Option<u16>) {
    // The window starts on `/splash.html` (tauri.conf.json). In remote mode
    // there is no backend to wait for — the SPA's own wizard asks for the
    // server address — so leave the splash for the app straight away.
    //
    // "Straight away" has to mean AT FIRST PAINT, not from this call: the
    // window's configured `url` is applied by the runtime AFTER `setup()`
    // returns, so a navigate() from here is simply overwritten by the splash.
    // The redirect is therefore armed now and fires on the splash's own load
    // event, which is the earliest moment the runtime will not undo it.
    //
    // Skipping the splash at the config level was the other option and it is
    // worse: the local path must keep the splash (a cold backend takes seconds
    // to answer, and navigating at it too early is the WebView2 error page),
    // and the mode is not known until `settings::load` has run.
    show_app_page(window);
    let state = BackendState {
        status: BackendStatus::Running.as_str(),
        origin: "",
        port: port_hint.unwrap_or(0),
        mode: settings.mode_str(),
        needs_choice: settings.needs_choice(),
        local: false,
    };
    let _ = window.emit(BACKEND_EVENT, state);
}

fn emit_state(
    window: &WebviewWindow,
    backend: &LocalBackend,
    settings: &ShellSettings,
    port: u16,
    status: BackendStatus,
) {
    if backend.keep_running.load(Ordering::Relaxed) {
        return; // the shell is quitting; nobody is listening any more
    }
    let origin = format!("http://127.0.0.1:{port}");
    let state = BackendState {
        status: status.as_str(),
        origin: &origin,
        port,
        mode: settings.mode_str(),
        // A local backend exists because the question was answered.
        needs_choice: false,
        local: true,
    };
    let _ = window.emit(BACKEND_EVENT, state);
}

/// The port this backend is bound to (None when it has none).
pub fn backend_port(backend: &LocalBackend) -> Option<u16> {
    super::backend::port_of(backend)
}

/// Stop the child and mark the shell as not keeping it.
pub fn stop_backend(backend: &LocalBackend) {
    super::backend::stop(backend);
}
