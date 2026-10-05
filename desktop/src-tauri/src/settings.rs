//! The shell's own persisted settings: which server it talks to, and where
//! the library is.
//!
//! The web UI keeps the server address itself (`localStorage: mlo.server`),
//! but two facts belong to the SHELL, not the page: whether the backend is
//! the shell's own child, and which music folder it was told about. Both are
//! needed before the webview exists (the backend has to be spawned with the
//! folder already in its env), so they live in a tiny JSON file in the
//! shell's per-user data dir rather than in a place the page owns.
//!
//! The file is optional: a shell that has never been configured (or a dev
//! run) defaults to local mode and no folder, exactly like a first launch.

use std::path::PathBuf;

use serde::{Deserialize, Serialize};

/// Where the shell's own state lives, next to the backend's per-user dir.
pub fn settings_file(app_data: &PathBuf) -> PathBuf {
    app_data.join("shell.json")
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum BackendMode {
    /// The shell spawns and owns the bundled backend.
    Local,
    /// The user pointed the shell at a server they run themselves.
    Remote,
}

impl Default for BackendMode {
    fn default() -> Self {
        BackendMode::Local
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ShellSettings {
    pub backend_mode: BackendMode,
    /// The music folder the last local backend was told about. "" = unknown
    /// (the server's own first-run wizard settles it).
    #[serde(default)]
    pub music_folder: String,
}

impl Default for ShellSettings {
    fn default() -> Self {
        ShellSettings {
            backend_mode: BackendMode::Local,
            music_folder: String::new(),
        }
    }
}

impl ShellSettings {
    pub fn is_local(&self) -> bool {
        self.backend_mode == BackendMode::Local
    }

    pub fn mode_str(&self) -> &'static str {
        match self.backend_mode {
            BackendMode::Local => "local",
            BackendMode::Remote => "remote",
        }
    }
}

/// Load the shell settings, or the defaults when none exist / are invalid.
pub fn load(app_data: &PathBuf) -> ShellSettings {
    match std::fs::read_to_string(settings_file(app_data)) {
        Ok(text) => serde_json::from_str(&text).unwrap_or_default(),
        Err(_) => ShellSettings::default(),
    }
}

/// Save the shell settings, atomically. Failures are logged, never fatal:
/// the settings are a convenience, not the app's state.
pub fn save(app_data: &PathBuf, settings: &ShellSettings) {
    let _ = std::fs::create_dir_all(app_data);
    let text = serde_json::to_string_pretty(settings).unwrap_or_default();
    let tmp = settings_file(app_data).with_extension("json.tmp");
    if std::fs::write(&tmp, text).is_ok() {
        let _ = std::fs::rename(tmp, settings_file(app_data));
    }
}
