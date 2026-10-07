//! Tauri's build script.
//!
//! Nothing else lives here any more. A mobile build used to embed CPython, and
//! this script carried the two link-time facts that needed (iOS linking
//! `Python.framework` from a staged xcframework, Android linking `libpython3.14.so`
//! from the staged native-lib directory) plus a `rerun-if-changed` on the tree
//! they were staged into. The shell is a client now: no target links a Python,
//! so there is no runtime to find, stage or warn about.
//!
//! What is left is the app-command manifest, and it is not optional. Without
//! it Tauri allows an app command from the shell's OWN pages and refuses every
//! one of them from a page served over http — and http is the page a packaged
//! install shows, because `backend_handle::attach_local` points the window at
//! the backend it spawned on loopback. That refusal is exactly what "the
//! window won't move and none of the controls work" looked like: dragging,
//! minimize, maximize and close are all `plugin:window` commands, but the
//! updater, the folder picker, the shell's own state and the iOS bridge are
//! app commands, and they were dead on that page too.
//!
//! Naming the commands here generates `allow-<command>` / `deny-<command>`
//! permissions, which `capabilities/default.json` then grants to the origins
//! the shell can actually load: its own assets (always) and loopback (the
//! backend it spawned). Keep this list equal to the one in `generate_handler!`
//! — a command missing here has no permission to be granted, so that page
//! cannot call it at all.
fn main() {
    tauri_build::try_build(
        tauri_build::Attributes::new().app_manifest(
            tauri_build::AppManifest::new().commands(&[
                "pick_folder",
                "set_music_folder",
                "open_external",
                "update_check",
                "update_install",
                "set_now_playing_liked",
                "set_playback_active",
                "ios_audio_state",
                "shell_backend_choice",
                "choose_backend",
            ]),
        ),
    )
    .expect("failed to run tauri-build");
}