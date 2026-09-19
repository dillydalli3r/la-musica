//! A local backend on a phone: embedded CPython, started in-process.
//!
//! The desktop path in `lib.rs` spawns `python -m uvicorn` and supervises the
//! child. A phone has no Python to spawn and — on iOS — no way to run a second
//! program at all, so the interpreter is *embedded*: the app links `libpython`
//! (CPython's Android embeddable package on Android, BeeWare's `Python.framework`
//! on iOS), points it at the runtime tree `tools/mobile/bundle.py` staged, and
//! runs the same `server.main:app` inside this process. docs.python.org says
//! embedding is "the only way you can use Python on Android" — there is no
//! `python` executable and no console there — and iOS has no `fork`/`exec`, so
//! one implementation covers both.
//!
//! It keeps every guarantee the desktop path has, because it reuses its
//! code: a listener that answers `/api/health` as ours is adopted rather than
//! duplicated, a foreign listener on the port is neither adopted nor killed, the
//! port is probed before anything starts, and the backend is asked to exit on the
//! way out (through the same env-gated `/api/shutdown` the desktop shell uses).
//! What it does *not* do is kill a process on quit: there is no child process —
//! the backend is this process, and it dies with the app.
//!
//! The one thing a phone gives up is spawning: iOS cannot execute another
//! program, so ffmpeg/flac/fpcalc/rsgain and everything built on them are simply
//! unavailable there. Android can (binaries in the APK's native-lib directory are
//! executable), so the shell exports `MLO_BUNDLED_TOOLS` for the interpreter to
//! find them. `backend_info` tells the webview which of the two it is talking to,
//! and why the local backend is unavailable when it is — that answer is what the
//! setup wizard needs to decide whether to offer "Host on this device" at all.

use std::path::{Path, PathBuf};
use std::time::Duration;

use parking_lot::Mutex;
use serde::{Deserialize, Serialize};
use tauri::{AppHandle, Emitter, Manager};

/// The port the backend listens on, shared with the desktop path in `lib.rs`.
use crate::PORT;

/// The Python source that starts the API. Kept as a file so it can be read and
/// reviewed as Python, and embedded so it cannot be missing at runtime.
const BOOTSTRAP: &str = include_str!("mobile_backend/bootstrap.py");

/// What the webview is told about the backend on this device.
///
/// `state` is the whole answer to "can this build host a backend?":
/// `hosting` (it is up, at `url`), `starting`, `unavailable` (with `reason`, for
/// the wizard to show) or `busy` (something that is not our backend holds the
/// port — the client path still works, it just points somewhere else).
#[derive(Clone, Serialize)]
pub struct BackendInfo {
    pub state: &'static str,
    pub url: String,
    pub reason: String,
    /// Always true here: this module only exists on the embedded path.
    pub embedded: bool,
    /// CLI tools bundled into this build (`ffmpeg`, `flac`, …) — Android only.
    pub tools: Vec<String>,
    /// Packages the bundle could not install (e.g. Pillow on Android).
    pub missing: Vec<String>,
    /// The user's "keep hosting in the background" setting, as the shell has it
    /// (the marker file, not the webview's own copy) — see [`keepalive`].
    pub keepalive: bool,
}

impl Default for BackendInfo {
    fn default() -> Self {
        Self {
            state: "starting",
            url: String::new(),
            reason: String::new(),
            embedded: true,
            tools: Vec::new(),
            missing: Vec::new(),
            keepalive: false,
        }
    }
}

pub struct State(pub Mutex<BackendInfo>);

impl Default for State {
    fn default() -> Self {
        Self(Mutex::new(BackendInfo::default()))
    }
}

/// What the webview asks for: whether this device hosts a backend, where, and
/// why not when it does not. Also emitted as the `mlo-backend` event whenever the
/// answer changes, so a UI that is already open does not have to poll.
#[tauri::command]
pub fn backend_info(state: tauri::State<'_, State>) -> BackendInfo {
    state.0.lock().clone()
}

/// Start the embedded backend (or adopt the one already listening).
///
/// Called from the setup hook, i.e. on the main thread: `Py_Initialize` and
/// thread creation must happen there, and only the *wait* for the server to
/// answer is moved to a thread of its own — importing FastAPI on a phone takes
/// long enough that doing it on the main thread would delay the window.
pub fn start(app: AppHandle) {
    // The keep-alive setting is applied before the backend is even considered:
    // on iOS the audio session has to be active before the first suspend, and on
    // Android the marker file is what the activity's service reads at launch.
    if stored_keepalive(&app) {
        keepalive::apply(&app, true);
    }
    // Adopt a backend that is already ours before considering the runtime: a
    // previous run's server, or one started from a shell, must not be duplicated
    // (two servers cannot share the port, and the loser's failure looks like the
    // app's).
    if crate::backend_is_ours() {
        return publish(
            &app,
            hosting(&format!(
                "adopted the la musica backend already listening on {PORT}"
            )),
        );
    }
    // Something else holds the port. It is neither adopted nor killed: the app
    // still works, the user points it at a server instead.
    if crate::backend_port_open() {
        let mut info = unavailable(&format!(
            "port {PORT} is in use by another app (not la musica), so nothing was \
             started — connect to a server instead"
        ));
        info.state = "busy";
        return publish(&app, info);
    }

    let runtime = match Runtime::resolve(&app) {
        Ok(runtime) => runtime,
        // Legible, not fatal: a build without a runnable Python still installs,
        // still opens, and still lets the user point at a server.
        Err(reason) => return publish(&app, unavailable(&reason)),
    };
    let tools = runtime.manifest.bundled_tools.clone();
    let missing = runtime.manifest.missing_optional.clone();
    runtime.apply_env(&app);

    if unsafe { ffi::Py_IsInitialized() } != 0 {
        // Cannot happen on the mobile path (nothing else embeds Python in this
        // process), but a second initialisation would be undefined behaviour, so
        // refuse instead of corrupting the interpreter.
        return publish(
            &app,
            unavailable("the embedded Python is already initialised in this process"),
        );
    }
    unsafe { ffi::Py_Initialize() };
    if unsafe { ffi::Py_IsInitialized() } == 0 {
        return publish(
            &app,
            unavailable(
                "the embedded Python did not initialise — the runtime tree in this \
                 bundle is incomplete (reinstall the app)",
            ),
        );
    }
    let source = match std::ffi::CString::new(BOOTSTRAP) {
        Ok(source) => source,
        Err(_) => return publish(&app, unavailable("the bundled bootstrap is malformed")),
    };
    // Runs on the main thread, with the GIL held by this thread. Returns
    // immediately: the bootstrap hands the server to a thread of its own.
    let rc = ffi::ffi_run(source.as_ptr());
    if rc != 0 {
        return publish(
            &app,
            unavailable(
                "the embedded backend failed to start — the device log carries the \
                 Python traceback",
            ),
        );
    }
    // Hand the GIL to the Python thread that is now serving, and never touch the
    // interpreter from this thread again: this is the documented embedding
    // pattern, and it is why shutdown goes through HTTP instead of Py_Finalize.
    ffi::ffi_release_gil();

    let handle = app.clone();
    std::thread::spawn(move || {
        // The server is up when it answers like ours. A slow first import of
        // FastAPI is the normal case, hence the generous window.
        for _ in 0..60 {
            if crate::backend_is_ours() {
                let mut info = hosting(&format!("serving on 127.0.0.1:{PORT}"));
                info.tools = tools;
                info.missing = missing;
                return publish(&handle, info);
            }
            std::thread::sleep(Duration::from_millis(500));
        }
        publish(
            &handle,
            unavailable(
                "the embedded backend started but never answered /api/health — the \
                 device log carries the Python traceback",
            ),
        );
    });
}

/// Ask the backend to exit on the way out.
///
/// Only ever called when the listener answers as ours (the desktop shell's rule,
/// kept verbatim). There is no child process to kill and none of this process's
/// memory outlives it, so a backend that ignores this still leaves nothing
/// running.
pub fn stop(_app: &AppHandle) {
    if crate::backend_is_ours() {
        crate::request_backend_shutdown();
        std::thread::sleep(Duration::from_millis(300));
    }
}

fn hosting(why: &str) -> BackendInfo {
    BackendInfo {
        state: "hosting",
        url: format!("http://127.0.0.1:{PORT}"),
        reason: why.to_string(),
        ..BackendInfo::default()
    }
}

fn unavailable(reason: &str) -> BackendInfo {
    BackendInfo {
        state: "unavailable",
        url: String::new(),
        reason: reason.to_string(),
        ..BackendInfo::default()
    }
}

/// Record the answer, tell the event listeners, and put the address in the
/// webview so the UI can talk to this device's backend without being told twice.
fn publish(app: &AppHandle, mut info: BackendInfo) {
    // The marker file is the truth for the toggle: read it here so every answer
    // the UI gets (command or event) carries the same value the shell would act
    // on after a relaunch.
    info.keepalive = stored_keepalive(app);
    println!("[mlo-mobile] backend {}: {}", info.state, info.reason);
    if let Some(state) = app.try_state::<State>() {
        *state.0.lock() = info.clone();
    }
    let _ = app.emit("mlo-backend", info.clone());
    if let Some(window) = app.get_webview_window("main") {
        // A JS assignment with a JSON literal: the URL comes from our own
        // formatting, but it is still escaped as data rather than spliced in.
        let url = serde_json::to_string(&info.url).unwrap_or_else(|_| "\"\"".into());
        let _ = window.eval(format!("window.__MLO_BACKEND_URL__ = {url};"));
    }
}

// --------------------------------------------------------------------------- //
// Keep-alive: hosting while the app is in the background
// --------------------------------------------------------------------------- //

/// Both mobile platforms suspend an app that goes to the background, and a
/// suspended app serves nothing: the embedded backend simply stops answering
/// until the app is resumed (the client must reconnect, not treat it as dead).
/// This setting asks the platform to keep the process alive anyway, and it means
/// something different on each:
///
/// * **Android** — a foreground service (`tools/mobile/android/MloKeepAlive.kt`,
///   injected into the generated Gradle project by
///   `tools/mobile/android/inject.py`) keeps the process running behind a
///   persistent notification. That is the platform's own mechanism for this, and
///   it is why the Rust side only has to keep the marker file honest — starting
///   a Service needs the JVM, and this crate deliberately carries no JNI bridge.
/// * **iOS** — there is no such mechanism. iOS grants background execution only
///   for a fixed list of *modes*, and `audio` is the one this app claims
///   honestly: it is a music player, and while audio plays the process keeps
///   running and the backend keeps serving. With nothing playing, this setting
///   holds a silent playback audio session open — the "silent audio to stay
///   alive" trick. It works, it costs battery, and Apple's review guidance names
///   it as abuse, so: off by default, opt-in, and only defensible because this
///   build is sideloaded. An App Store submission must not ship it on.
///
/// The setting is a marker file rather than memory because it has to survive a
/// relaunch: Android's service is started at launch by the activity, and iOS's
/// session must be active before the first suspend.
fn marker_path(app: &AppHandle) -> Result<PathBuf, String> {
    app.path()
        .app_data_dir()
        .map(|dir| dir.join("keepalive"))
        .map_err(|e| format!("no writable app directory for the keep-alive marker: {e}"))
}

fn stored_keepalive(app: &AppHandle) -> bool {
    marker_path(app).map(|path| path.is_file()).unwrap_or(false)
}

/// The UI's background-hosting switch lands here.
///
/// Persists the choice, applies it to the platform, and answers with the same
/// `BackendInfo` shape the UI already reads.
#[tauri::command]
pub fn set_backend_keepalive(
    keep: bool,
    app: AppHandle,
    state: tauri::State<'_, State>,
) -> BackendInfo {
    match marker_path(&app) {
        Ok(path) => {
            let written = if keep {
                std::fs::write(&path, b"1").map_err(|e| e.to_string())
            } else {
                std::fs::remove_file(&path).map_err(|e| e.to_string())
            };
            if let Err(e) = written {
                eprintln!("[mlo-mobile] keep-alive setting not saved: {e}");
            }
        }
        Err(e) => eprintln!("[mlo-mobile] {e}"),
    }
    keepalive::apply(&app, keep);
    let info = {
        let mut current = state.0.lock().clone();
        current.keepalive = keep;
        current
    };
    publish(&app, info.clone());
    info
}

mod keepalive {
    use super::*;

    /// Android's half is the injected foreground service, which follows the
    /// marker file the command just wrote (it re-checks every few seconds, so a
    /// live toggle takes effect without a restart, and stops itself when the
    /// file is gone).
    #[cfg(target_os = "android")]
    pub fn apply(_app: &AppHandle, _keep: bool) {}

    /// iOS's half is the audio session. `UIBackgroundModes: [audio]` in
    /// Info.plist is what makes the session count as a reason to keep running;
    /// activating it is what this does.
    #[cfg(target_os = "ios")]
    pub fn apply(_app: &AppHandle, keep: bool) {
        use objc2_avf_audio::{AVAudioSession, AVAudioSessionCategoryPlayback};

        // SAFETY: AVAudioSession is a process-wide singleton, these are its
        // documented calls in their documented order (category first, then
        // activation), and both report failure as an NSError rather than by
        // throwing.
        unsafe {
            let session = AVAudioSession::sharedInstance();
            if keep {
                if let Some(category) = AVAudioSessionCategoryPlayback {
                    if let Err(e) = session.setCategory_error(category) {
                        eprintln!("[mlo-mobile] audio session category: {e}");
                    }
                }
                if let Err(e) = session.setActive_error(true) {
                    eprintln!("[mlo-mobile] audio session activation: {e}");
                }
            } else if let Err(e) = session.setActive_error(false) {
                eprintln!("[mlo-mobile] audio session deactivation: {e}");
            }
        }
    }
}

// --------------------------------------------------------------------------- //
// The runtime tree
// --------------------------------------------------------------------------- //

/// What `tools/mobile/bundle.py` measured about the tree it staged.
#[derive(Clone, Default, Deserialize)]
#[serde(default)]
struct Manifest {
    python: String,
    runtime: String,
    backend_importable: bool,
    reason: String,
    missing_required: Vec<String>,
    missing_optional: Vec<String>,
    /// Hash of the bundled `app/` tree: the writable copy is refreshed when it
    /// changes, which is what makes an app update replace its own backend code.
    app_sha256: String,
    #[serde(alias = "tools")]
    bundled_tools: Vec<String>,
}

struct Runtime {
    /// PYTHONHOME: the directory holding `lib/pythonX.Y`.
    home: PathBuf,
    /// The standard library, its extension modules, and the backend's code (in
    /// that order: the writable copy of the code has to win).
    python_path: Vec<PathBuf>,
    /// Where the bundled CLI tools live, when this platform has any.
    tools_dir: Option<PathBuf>,
    manifest: Manifest,
}

impl Runtime {
    fn resolve(app: &AppHandle) -> Result<Self, String> {
        #[cfg(target_os = "ios")]
        {
            ios::resolve(app)
        }
        #[cfg(target_os = "android")]
        {
            android::resolve(app)
        }
    }

    fn apply_env(&self, app: &AppHandle) {
        std::env::set_var("PYTHONHOME", &self.home);
        std::env::set_var(
            "PYTHONPATH",
            std::env::join_paths(&self.python_path)
                .map(|p| p.to_string_lossy().to_string())
                .unwrap_or_default(),
        );
        // UTF-8 and unbuffered stdio, and no bytecode: the tree is read-only on
        // iOS and writing .pyc into the app bundle would fail on every import.
        std::env::set_var("PYTHONUTF8", "1");
        std::env::set_var("PYTHONUNBUFFERED", "1");
        std::env::set_var("PYTHONDONTWRITEBYTECODE", "1");
        // The backend may be asked to exit through the same env-gated endpoint
        // the desktop shell uses, and reports itself as the embedded kind.
        std::env::set_var("MLO_ALLOW_SHUTDOWN", "1");
        std::env::set_var("MLO_EMBEDDED", "1");
        if let Some(dir) = &self.tools_dir {
            std::env::set_var("MLO_BUNDLED_TOOLS", dir);
        }
        #[cfg(target_os = "android")]
        {
            // Android sets TMPDIR only from API 33 on, and gives the app no HOME
            // at all; anything that expands `~` (or writes a temp file for a
            // large cover) needs both.
            if let Ok(cache) = app.path().app_cache_dir() {
                std::env::set_var("TMPDIR", &cache);
            }
            if let Ok(data) = app.path().app_data_dir() {
                std::env::set_var("HOME", &data);
            }
        }
        #[cfg(not(target_os = "android"))]
        {
            let _ = app;
        }
    }
}

fn read_manifest(python_root: &Path) -> Result<Manifest, String> {
    let path = python_root.join("runtime.json");
    let text = std::fs::read_to_string(&path).map_err(|e| {
        format!(
            "this build carries no Python runtime ({}: {e}) — a mobile build stages \
             one with tools/mobile/bundle.py",
            path.display()
        )
    })?;
    let manifest: Manifest = serde_json::from_str(&text)
        .map_err(|e| format!("the bundled runtime.json is unreadable: {e}"))?;
    if !manifest.backend_importable {
        let why = if manifest.reason.is_empty() {
            format!(
                "this build's Python cannot import the backend (missing: {})",
                manifest.missing_required.join(", ")
            )
        } else {
            manifest.reason.clone()
        };
        return Err(why);
    }
    Ok(manifest)
}

/// Copy a directory tree without pulling in a crate for it (the iOS runtime is
/// read-only bundle resources, so its code has to be copied out to be writable —
/// see [`materialize_app_code`]; nothing else on the mobile path needs a
/// recursive copy, so this and the function below exist for iOS alone).
#[cfg(target_os = "ios")]
fn copy_tree(src: &Path, dest: &Path) -> Result<(), String> {
    std::fs::create_dir_all(dest).map_err(|e| format!("{}: {e}", dest.display()))?;
    for entry in std::fs::read_dir(src).map_err(|e| format!("{}: {e}", src.display()))? {
        let entry = entry.map_err(|e| format!("{}: {e}", src.display()))?;
        let to = dest.join(entry.file_name());
        let kind = entry.file_type().map_err(|e| format!("{}: {e}", to.display()))?;
        if kind.is_dir() {
            copy_tree(&entry.path(), &to)?;
        } else {
            std::fs::copy(entry.path(), &to).map_err(|e| format!("{}: {e}", to.display()))?;
        }
    }
    Ok(())
}

/// Make the backend's own Python code writable, and keep it current.
///
/// iOS ships the bundle read-only, and `mlo.paths` puts `config.json`, the
/// caches and the `.dependencies` folder next to that code — so the code has to
/// be copied into the app's data directory once and refreshed whenever the
/// bundled copy changes (an app update replaces its backend, which is the whole
/// point of shipping one).
#[cfg(target_os = "ios")]
fn materialize_app_code(bundled: &Path, writable: &Path, want_hash: &str) -> Result<PathBuf, String> {
    let stamp = writable.join(".bundle-stamp");
    let current = std::fs::read_to_string(&stamp).unwrap_or_default();
    let installed = writable.join("server").join("main.py").is_file();
    if installed && (want_hash.is_empty() || current.trim() == want_hash) {
        return Ok(writable.to_path_buf());
    }
    if writable.exists() {
        std::fs::remove_dir_all(writable)
            .map_err(|e| format!("cannot replace {}: {e}", writable.display()))?;
    }
    copy_tree(bundled, writable)?;
    let _ = std::fs::write(&stamp, want_hash);
    println!("[mlo-mobile] backend code installed at {}", writable.display());
    Ok(writable.to_path_buf())
}

// --------------------------------------------------------------------------- //
// CPython
// --------------------------------------------------------------------------- //

/// The four entry points of the C embedding API this module uses.
///
/// Only these: the full `PyConfig` API would mean reproducing CPython's struct
/// layout for this interpreter's exact version, which is a build-time dependency
/// on the Python headers for a handful of settings that environment variables
/// express anyway (`PYTHONHOME`, `PYTHONPATH`, `PYTHONUTF8`,
/// `PYTHONDONTWRITEBYTECODE`).
mod ffi {
    use std::os::raw::{c_char, c_int, c_void};

    extern "C" {
        pub fn Py_Initialize();
        pub fn Py_IsInitialized() -> c_int;
        fn PyRun_SimpleStringFlags(code: *const c_char, flags: *mut c_void) -> c_int;
        fn PyEval_SaveThread() -> *mut c_void;
    }

    pub fn ffi_run(source: *const c_char) -> c_int {
        // NULL flags: PyRun_SimpleString is this call with no flags.
        unsafe { PyRun_SimpleStringFlags(source, std::ptr::null_mut()) }
    }

    /// Drop the GIL held by the calling thread and keep that thread state for
    /// the rest of this thread's life — the documented way to hand control to a
    /// Python thread and carry on in native code.
    pub fn ffi_release_gil() {
        unsafe {
            PyEval_SaveThread();
        }
    }
}

// --------------------------------------------------------------------------- //
// iOS
// --------------------------------------------------------------------------- //

#[cfg(target_os = "ios")]
mod ios {
    use super::*;

    /// The bundle is a real filesystem tree here: Tauri resources land in
    /// `<app>.app/assets`, so `resource_dir()` is a path Python can open.
    pub fn resolve(app: &AppHandle) -> Result<Runtime, String> {
        let resources = app
            .path()
            .resource_dir()
            .map_err(|e| format!("no resource directory in this bundle: {e}"))?;
        let python_root = resources.join("mobile").join("python");
        let manifest = read_manifest(&python_root)?;
        let stdlib = python_root
            .join("lib")
            .join(format!("python{}", manifest.python));
        if !stdlib.is_dir() {
            return Err(format!(
                "this build carries no Python standard library at {} (reinstall the app)",
                stdlib.display()
            ));
        }
        let data = app
            .path()
            .app_data_dir()
            .map_err(|e| format!("no writable app directory: {e}"))?;
        let code = materialize_app_code(
            &python_root.join("app"),
            &data.join("backend"),
            &manifest.app_sha256,
        )?;
        Ok(Runtime {
            home: python_root,
            python_path: vec![code, stdlib.clone(), stdlib.join("lib-dynload")],
            // iOS cannot execute another program, so no tools are bundled and
            // none are looked for.
            tools_dir: None,
            manifest,
        })
    }
}

// --------------------------------------------------------------------------- //
// Android
// --------------------------------------------------------------------------- //

#[cfg(target_os = "android")]
mod android {
    use super::*;
    use std::os::raw::{c_char, c_int, c_void};

    /// Where the APK unpacked this app's native libraries.
    ///
    /// Asked of the dynamic linker rather than of the JVM: `dladdr` on an
    /// address in this crate answers with the path of the library it lives in,
    /// which sits in the native-lib directory next to `libpython` and the
    /// bundled tools. No JNI, no JVM, no manifest parsing.
    fn native_lib_dir() -> Result<PathBuf, String> {
        #[repr(C)]
        struct DlInfo {
            dli_fname: *const c_char,
            dli_fbase: *mut c_void,
            dli_sname: *const c_char,
            dli_saddr: *mut c_void,
        }
        extern "C" {
            fn dladdr(addr: *const c_void, info: *mut DlInfo) -> c_int;
        }
        fn anchor() {}

        let mut info = DlInfo {
            dli_fname: std::ptr::null(),
            dli_fbase: std::ptr::null_mut(),
            dli_sname: std::ptr::null(),
            dli_saddr: std::ptr::null_mut(),
        };
        let found = unsafe { dladdr(anchor as *const c_void, &mut info) };
        if found == 0 || info.dli_fname.is_null() {
            return Err("this app's native library directory cannot be located".into());
        }
        let path = unsafe { std::ffi::CStr::from_ptr(info.dli_fname) }
            .to_string_lossy()
            .to_string();
        if path.contains("!/") {
            // AGP can store native libraries uncompressed inside the APK and load
            // them from there; then there is no directory, and neither the Python
            // tree nor the bundled tools have a path to be executed from.
            return Err(
                "this APK loads its native libraries straight from the archive \
                 (extractNativeLibs/useLegacyPackaging is off), so the bundled \
                 Python has no files to read — rebuild with \
                 `jniLibs.useLegacyPackaging = true`"
                    .into(),
            );
        }
        let dir = Path::new(&path)
            .parent()
            .ok_or("the native library path has no parent directory")?
            .to_path_buf();
        if !dir.is_dir() {
            return Err(format!("{} is not a readable directory", dir.display()));
        }
        Ok(dir)
    }

    /// The payload's identity as a file: enough to notice that a new build ships
    /// a new runtime, and cheap enough to check on every launch (hashing a 40 MB
    /// archive before the UI opens is not).
    fn payload_stamp(payload: &Path) -> Result<String, String> {
        let meta = std::fs::metadata(payload).map_err(|e| {
            format!(
                "this APK carries no Python payload ({}: {e}) — a mobile build stages \
                 one with tools/mobile/bundle.py",
                payload.display()
            )
        })?;
        let modified = meta
            .modified()
            .ok()
            .and_then(|t| t.duration_since(std::time::UNIX_EPOCH).ok())
            .map(|d| d.as_secs())
            .unwrap_or(0);
        Ok(format!("{}:{}", meta.len(), modified))
    }

    /// Unpack the Python tree into the app's own storage, once per payload.
    fn extract(payload: &Path, home: &Path, stamp: &str) -> Result<(), String> {
        // ponytail: full re-extract on a new payload (~40 MB, once per install).
        // An incremental unpack would need per-file stamps; the app's data
        // directory is not worth that until someone ships a metered-data build.
        if home.exists() {
            std::fs::remove_dir_all(home)
                .map_err(|e| format!("cannot replace {}: {e}", home.display()))?;
        }
        std::fs::create_dir_all(home).map_err(|e| format!("{}: {e}", home.display()))?;
        let file = std::fs::File::open(payload)
            .map_err(|e| format!("cannot open {}: {e}", payload.display()))?;
        let gz = flate2::read::GzDecoder::new(file);
        let mut archive = tar::Archive::new(gz);
        archive
            .unpack(home)
            .map_err(|e| format!("cannot unpack the bundled Python into {}: {e}", home.display()))?;
        let _ = std::fs::write(home.join(".payload-stamp"), stamp);
        println!("[mlo-mobile] Python runtime unpacked into {}", home.display());
        Ok(())
    }

    pub fn resolve(app: &AppHandle) -> Result<Runtime, String> {
        let lib_dir = native_lib_dir()?;
        let payload = lib_dir.join("libmlopy.so");
        let stamp = payload_stamp(&payload)?;
        let data = app
            .path()
            .app_data_dir()
            .map_err(|e| format!("no writable app directory: {e}"))?;
        let home = data.join("python");
        let current = std::fs::read_to_string(home.join(".payload-stamp")).unwrap_or_default();
        if current.trim() != stamp {
            extract(&payload, &home, &stamp)?;
        }
        let manifest = read_manifest(&home)?;
        let stdlib = home.join("lib").join(format!("python{}", manifest.python));
        if !stdlib.is_dir() {
            return Err(format!(
                "the unpacked Python has no standard library at {} (reinstall the app)",
                stdlib.display()
            ));
        }
        // The tools sit in the native-lib directory beside the payload, where
        // Android permits executing them; the backend resolves them there because
        // it is told so (MLO_BUNDLED_TOOLS) rather than having to know the ABI
        // directory layout. The app's own code is already writable on Android,
        // so no copy is needed (unlike iOS).
        let tools_dir = (!manifest.bundled_tools.is_empty()).then(|| lib_dir.clone());
        Ok(Runtime {
            home: home.clone(),
            python_path: vec![home.join("app"), stdlib.clone(), stdlib.join("lib-dynload")],
            tools_dir,
            manifest,
        })
    }
}
