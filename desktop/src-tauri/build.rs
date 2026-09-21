//! Tauri's build script.
//!
//! Nothing else lives here any more. A mobile build used to embed CPython, and
//! this script carried the two link-time facts that needed (iOS linking
//! `Python.framework` from a staged xcframework, Android linking `libpython3.14.so`
//! from the staged native-lib directory) plus a `rerun-if-changed` on the tree
//! they were staged into. The shell is a client now: no target links a Python,
//! so there is no runtime to find, stage or warn about.
fn main() {
    tauri_build::build()
}
