//! The desktop shell's own local backend: spawn, supervise, stop.
//!
//! The shell is a CLIENT of a server it can also run itself. Since 3.2.0 the
//! desktop build carried no backend at all — every client talked to the user's
//! own container or a LAN host. That stays the default when the user points
//! the app anywhere, and it stays the only mode on mobile. What this module
//! adds is the OTHER option: a "Local backend" the shell spawns on demand, so
//! a la musica install that has no Docker container (or whose music folder
//! lives on this machine) works out of the box.
//!
//! The backend is a frozen Python service (`mlo-server`, built by
//! `pyinstaller/mlo-server.spec` on each OS at release time). The shell finds
//! it next to its own binary, picks a free loopback port from 8011 up, starts
//! the process with the env it needs and watches it. It is a CHILD of this
//! shell — the shell's own lifecycle ends it (or the user stops it from the
//! tray) — which is the one lifecycle a client is allowed to own: the local
//! backend exists for this app alone.
//!
//! Supervision is deliberately minimal: the child is healthy while the TCP
//! probe answers, and a child that dies is restarted with a short backoff. No
//! REST endpoints, no privilege game, no second service on the machine.

use parking_lot::Mutex;
use std::env;
use std::io::Read;
use std::net::{Ipv4Addr, TcpListener, TcpStream};
use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::thread;
use std::time::{Duration, Instant};

/// The first loopback port we try. 8000 is the user's own live install (the
/// Docker container) and the dev bed owns 8011+ while it runs, so a free
/// port from 8011 up is the shell's own lane (AGENTS.md).
pub const FIRST_PORT: u16 = 8011;
/// How many ports we scan before giving up (the bed asks up to +40; this is
/// the same budget without the user to answer).
const PORT_SCAN: u16 = 40;
/// How long we wait before restarting a crashed child.
const RESTART_BACKOFF: Duration = Duration::from_secs(3);

/// One supervised backend.
pub struct LocalBackend {
    state: Arc<Mutex<State>>,
    /// Set once at construction; checked by ExitRequested handlers. Kept on
    /// the struct so the tray menu event can flip it without needing the
    /// handle.
    pub keep_running: Arc<AtomicBool>,
}

struct State {
    child: Option<Child>,
    port: Option<u16>,
    last_started: Option<Instant>,
}

impl LocalBackend {
    /// Start the bundled backend. None only when no `mlo-server` is bundled
    /// next to this binary (a dev build before `desktop/bundle` is staged).
    pub fn start(bundle: &BackendBundle) -> Option<Arc<LocalBackend>> {
        let (child, port) = match spawn_child(bundle) {
            Ok(pair) => pair,
            Err(e) => {
                eprintln!("[mlo-desktop] {e}");
                return None;
            }
        };
        let backend = Arc::new(LocalBackend {
            state: Arc::new(Mutex::new(State {
                child: Some(child),
                port: Some(port),
                last_started: Some(Instant::now()),
            })),
            keep_running: Arc::new(AtomicBool::new(false)),
        });
        // The supervisor owns the lifecycle now: a dead child is restarted in
        // place, and the keep_running flag is how an exit request stops it.
        let w = Arc::clone(&backend);
        let bundle = bundle.clone();
        thread::Builder::new()
            .name("mlo-local-backend".into())
            .spawn(move || w.supervise(bundle))
            .ok();
        Some(backend)
    }

    /// The port this backend is bound to (None when it has none).
    pub fn port(&self) -> Option<u16> {
        self.state.lock().port
    }

    /// The supervisor loop. Runs until the shell process exits; the child is
    /// restarted in place while the shell runs, because a local app without
    /// its backend is a dead app and the user asked for a local install.
    fn supervise(&self, bundle: BackendBundle) {
        loop {
            // If the user quit, stop the child and end the loop.
            if self.keep_running.load(Ordering::Relaxed) {
                let mut s = self.state.lock();
                if let Some(mut c) = s.child.take() {
                    let _ = c.kill();
                    let _ = c.wait();
                }
                s.port = None;
                return;
            }

            let child = {
                let mut s = self.state.lock();
                s.child.take()
            };
            let Some(mut child) = child else {
                return; // no child to watch; the shell stopped the backend
            };

            // Drain the child's stdout/stderr into the void so a child that
            // writes a lot cannot block on a full pipe.
            let mut out = child.stdout.take();
            let mut err = child.stderr.take();
            let _ = thread::Builder::new()
                .name("mlo-backend-drain".into())
                .spawn(move || {
                    let mut buf = [0u8; 4096];
                    if let Some(mut o) = out.take() {
                        while o.read(&mut buf).is_ok() {}
                    }
                    if let Some(mut e) = err.take() {
                        while e.read(&mut buf).is_ok() {}
                    }
                });

            let exited = loop {
                match child.try_wait() {
                    Ok(Some(status)) => break Some(status),
                    Ok(None) => {
                        if self.keep_running.load(Ordering::Relaxed) {
                            let _ = child.kill();
                            let _ = child.wait();
                            return;
                        }
                        thread::sleep(Duration::from_millis(250));
                    }
                    Err(e) => {
                        eprintln!("[mlo-desktop] local backend poll: {e}");
                        let _ = child.kill();
                        let _ = child.wait();
                        break None;
                    }
                }
            };
            if let Some(status) = exited {
                eprintln!("[mlo-desktop] local backend exited: {status}");
            }

            // The child is gone (or could not be polled): respawn in place.
            thread::sleep(RESTART_BACKOFF);
            match spawn_child(&bundle) {
                Ok((fresh, port)) => {
                    let mut s = self.state.lock();
                    s.child = Some(fresh);
                    s.port = Some(port);
                    s.last_started = Some(Instant::now());
                }
                Err(e) => {
                    // Nothing else records this: the supervisor ends here and
                    // the shell is left with no backend (the tray says so).
                    // The reason goes to the log, which is where a user
                    // looking at "Backend: unavailable" can find why.
                    eprintln!("[mlo-desktop] local backend respawn failed: {e}");
                    return;
                }
            }
        }
    }
}

/// The port a backend is bound to (None when it has none) — the free-function
/// form the window-attach code uses so it never needs to name the private
/// field.
pub fn port_of(backend: &LocalBackend) -> Option<u16> {
    backend.port()
}

/// Stop the child and mark the shell as not keeping it.
pub fn stop(backend: &LocalBackend) {
    let mut s = backend.state.lock();
    if let Some(mut c) = s.child.take() {
        let _ = c.kill();
        let _ = c.wait();
    }
    s.port = None;
}

/// Spawn one backend child for *bundle* on a free port, with the shell's env.
fn spawn_child(bundle: &BackendBundle) -> Result<(Child, u16), String> {
    let exe = bundle
        .server_exe()
        .ok_or_else(|| "bundled backend (mlo-server) is missing".to_string())?;
    let port = pick_free_port().ok_or_else(|| {
        format!(
            "no free loopback port between {} and {}",
            FIRST_PORT,
            FIRST_PORT + PORT_SCAN
        )
    })?;
    let mut cmd = Command::new(&exe);
    cmd.env("MLO_SERVER_HOST", "127.0.0.1")
        .env("MLO_SERVER_PORT", port.to_string())
        .stdin(Stdio::null());
    if let Some(app_data) = bundle.app_data_dir() {
        cmd.env("MLO_APP_DATA_DIR", &app_data);
    }
    if let Some(music_dir) = bundle.music_dir() {
        cmd.env("MLO_MUSIC_FOLDER", &music_dir);
    }
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        const CREATE_NO_WINDOW: u32 = 0x0800_0000;
        cmd.creation_flags(CREATE_NO_WINDOW);
    }
    let child = cmd
        .spawn()
        .map_err(|e| format!("failed to start local backend: {e}"))?;
    Ok((child, port))
}
/// Where the bundled backend, its SPA and the shell's own data live.
#[derive(Clone)]
pub struct BackendBundle {
    /// The onedir folder containing `mlo-server[.exe]`.
    pub server_dir: PathBuf,
    /// The per-user data dir the server writes its config into.
    pub app_data_dir: PathBuf,
    /// The music folder the server should be told about, if the shell knows
    /// one (the wizard writes it into the shell's own store).
    pub music_dir: Option<PathBuf>,
}

impl BackendBundle {
    pub fn server_exe(&self) -> Option<PathBuf> {
        for name in ["mlo-server.exe", "mlo-server"] {
            let cand = self.server_dir.join(name);
            if cand.is_file() {
                return Some(cand);
            }
        }
        None
    }

    pub fn app_data_dir(&self) -> Option<PathBuf> {
        Some(self.app_data_dir.clone())
    }

    pub fn music_dir(&self) -> Option<PathBuf> {
        self.music_dir.clone()
    }
}

/// The per-user data dir the shell and the bundled backend SHARE. The
/// backend launcher (backend_launcher/__main__.py) uses `%LOCALAPPDATA%/la
/// musica` on Windows, `~/Library/Application Support/la musica` on macOS and
/// `~/.local/share/la musica` on Linux — so this must be the same folder, or
/// the shell's spawn env would point the backend at one place and its own
/// settings read from another. `data_local_dir()` is LocalAppData on Windows
/// and the platform's data dir elsewhere, exactly what the launcher does.
pub fn default_app_data_dir() -> PathBuf {
    if let Some(d) = dirs::data_local_dir() {
        return d.join("la musica");
    }
    env::current_dir()
        .unwrap_or_else(|_| PathBuf::from("."))
        .join(".la-musica")
}

/// The first free loopback port at or above FIRST_PORT.
fn pick_free_port() -> Option<u16> {
    for port in FIRST_PORT..FIRST_PORT + PORT_SCAN {
        if let Ok(l) = TcpListener::bind((Ipv4Addr::LOCALHOST, port)) {
            drop(l);
            return Some(port);
        }
    }
    None
}

/// Ask 127.0.0.1:port /api/health and get a 200 within *timeout*.
pub fn probe_health(port: u16, timeout: Duration) -> bool {
    let start = Instant::now();
    loop {
        let mut s = match TcpStream::connect((Ipv4Addr::LOCALHOST, port)) {
            Ok(s) => s,
            Err(_) => {
                if start.elapsed() >= timeout {
                    return false;
                }
                thread::sleep(Duration::from_millis(200));
                continue;
            }
        };
        s.set_read_timeout(Some(Duration::from_secs(1)))
            .unwrap_or_default();
        let _ = std::io::Write::write_all(
            &mut s,
            b"GET /api/health HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n",
        );
        let mut buf = [0u8; 64];
        match s.read(&mut buf) {
            Ok(n) if n > 0 => {
                let head = String::from_utf8_lossy(&buf[..n]);
                // A 404 is served by something else on this port: not ours.
                return head.contains("200");
            }
            _ => {
                thread::sleep(Duration::from_millis(200));
                if start.elapsed() >= timeout {
                    return false;
                }
            }
        }
    }
}
