//! Tauri's build script, plus the two link-time facts a mobile build needs.
//!
//! A mobile build embeds CPython, and both platforms want that said in the build
//! rather than in the source: iOS links `Python.framework` from the xcframework
//! the pipeline staged, Android links `libpython3.14.so` out of the staged
//! native-lib directory (`tools/mobile/bundle.py` produces both). Neither is
//! present in a desktop checkout or in CI's `cargo check` targets, so a missing
//! runtime is a warning here and an unresolved-symbol error at link time of the
//! mobile build that actually needs it — never a broken desktop build.
use std::path::PathBuf;

/// The staged runtime for the target being built, if it has been staged.
fn staged_dir(target_os: &str, target: &str) -> Option<PathBuf> {
    let crate_dir = PathBuf::from(std::env::var("CARGO_MANIFEST_DIR").ok()?);
    match target_os {
        // The pipeline puts the framework here; `bundle.iOS.frameworks` in
        // tauri.conf.json points at the same tree so Xcode also embeds it.
        "ios" => {
            let slice = if target.ends_with("-sim") {
                "ios-arm64_x86_64-simulator"
            } else {
                "ios-arm64"
            };
            let dir = crate_dir
                .join("resources/mobile/ios/Python.xcframework")
                .join(slice);
            (dir.join("Python.framework").is_dir()).then_some(dir)
        }
        // One ABI: the pipeline stages arm64-v8a only, and the generated Gradle
        // project is told to build that ABI alone (a universal APK would claim
        // four ABIs and carry a runtime for one).
        "android" => {
            let dir = crate_dir.join("resources/mobile/android/jniLibs/arm64-v8a");
            (dir.join("libpython3.14.so").is_file()).then_some(dir)
        }
        _ => None,
    }
}

fn main() {
    println!("cargo:rerun-if-changed=resources/mobile");

    let target_os = std::env::var("CARGO_CFG_TARGET_OS").unwrap_or_default();
    let target = std::env::var("TARGET").unwrap_or_default();
    match staged_dir(&target_os, &target) {
        Some(dir) => {
            println!("cargo:rustc-link-search=native={}", dir.display());
            if target_os == "ios" {
                println!("cargo:rustc-link-search=framework={}", dir.display());
                println!("cargo:rustc-link-lib=framework=Python");
            } else {
                println!("cargo:rustc-link-lib=dylib=python3.14");
            }
        }
        None if target_os == "ios" || target_os == "android" => {
            println!(
                "cargo:warning=no staged Python runtime for {target} — mobile_backend.rs \
                 links against one, so this build will fail to link. Stage it first: \
                 python tools/mobile/bundle.py {target_os}"
            );
        }
        None => {}
    }

    tauri_build::build()
}
