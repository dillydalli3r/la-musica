#!/usr/bin/env python3
"""The desktop shell's app commands: three lists that must agree.

Tauri refuses an APP command from the shell's own page unless that command
names a permission the capability grants — and a permission only exists for a
command the build script declared. So one command is really three entries:

  * ``desktop/src-tauri/build.rs``           the manifest list (what generates
    ``allow-<command>`` / ``deny-<command>`` at all)
  * ``desktop/src-tauri/src/lib.rs``         ``generate_handler!`` (what the
    shell can actually run)
  * ``desktop/src-tauri/capabilities/default.json``  the grants (what the page
    is allowed to call)

A command missing from the first list has no permission to be granted, so the
page gets "command not allowed" and the feature is dead in a packaged install
while working perfectly in a dev build that loads its own assets — which is
what "the window won't move and none of the controls work" looked like before
the manifest existed. This gate fails on any disagreement, in either direction,
so a new command cannot land with its grant forgotten.

Run: python tools/test_desktop_commands.py
Exit 0 = pass, 1 = failed, 2 = cannot run here (no desktop shell in this tree).
"""
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "desktop", "src-tauri")


def read(*parts):
    path = os.path.join(SRC, *parts)
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def manifest_commands():
    """The command list the build script hands tauri-build."""
    src = read("build.rs")
    block = re.search(r"\.commands\(&\[(.*?)\]\)", src, re.S)
    if not block:
        return None
    return set(re.findall(r'"([^"]+)"', block.group(1)))


def handler_commands():
    """`generate_handler![...]` — every command the shell registers."""
    src = read("src", "lib.rs")
    block = re.search(r"generate_handler!\[(.*?)\]", src, re.S)
    if not block:
        return None
    return set(re.findall(r"\b([a-z_][a-z0-9_]*)\s*,", block.group(1)))


def capability_grants():
    """The capability's permission list, split into app grants and plugin ones.

    An app command's grant is the bare ``allow-<name>``; a plugin permission
    always carries its namespace (``core:…``, ``dialog:…``), which is how the
    two are told apart without a second hand-kept list.
    """
    cap = json.loads(read("capabilities", "default.json"))
    perms = cap.get("permissions", [])
    app = {p[len("allow-"):].replace("-", "_") for p in perms if p.startswith("allow-")}
    plugin = [p for p in perms if not p.startswith("allow-")]
    return app, plugin, cap


def main():
    if not os.path.isdir(SRC):
        print("Cannot run here: desktop/src-tauri is not in this tree")
        return 2
    fail = 0

    def check(label, ok, detail=""):
        nonlocal fail
        print(("  ok   " if ok else "  FAIL ") + label + ("" if ok else "  " + detail))
        if not ok:
            fail += 1

    manifest = manifest_commands()
    handler = handler_commands()
    app_grants, plugin_grants, cap = capability_grants()

    print("desktop app commands")
    check("build.rs declares a command manifest", bool(manifest), "no .commands(&[...]) found")
    check("lib.rs registers commands in generate_handler!", bool(handler), "no generate_handler![...] found")
    if not manifest or not handler:
        print(f"\n{'FAIL' if fail else 'PASS'} — {fail} problem(s)")
        return 1 if fail else 0

    check(
        "build.rs manifest == generate_handler! (both directions)",
        manifest == handler,
        f"manifest-only={sorted(manifest - handler)} handler-only={sorted(handler - manifest)}",
    )
    check(
        "every registered command is granted by the capability",
        handler <= app_grants,
        f"ungranted={sorted(handler - app_grants)}",
    )
    check(
        "no grant names a command the shell no longer has",
        app_grants <= handler,
        f"stale grants={sorted(app_grants - handler)}",
    )

    # The remote grant is what makes any of this reachable from the page a
    # packaged install shows (the backend on loopback): without it every one of
    # these commands is refused there.
    urls = (cap.get("remote") or {}).get("urls") or []
    check(
        "the capability grants loopback (the page a packaged install loads)",
        any(u.startswith("http://127.0.0.1") for u in urls),
        f"remote.urls={urls}",
    )
    check("the capability names the main window", "main" in (cap.get("windows") or []), str(cap.get("windows")))
    check("plugin permissions keep their namespace", all(":" in p for p in plugin_grants), str(plugin_grants))

    # The wizard reaches the shell's pickers through these two, and the
    # multi-folder one is the reason a web folder input is not enough: it can
    # only ever ask for ONE folder.
    wizard = open(os.path.join(ROOT, "web", "src", "pages", "ImportWizard.tsx"), encoding="utf-8").read()
    for cmd in ("pick_folders", "pick_folder"):
        check(
            f"the wizard calls {cmd}",
            f'"{cmd}"' in wizard,
            f"ImportWizard never invokes {cmd}",
        )
    check(
        "the wizard falls back to the single-folder command",
        re.search(r'invoke<string\[\]>\("pick_folders"\).*?catch.*?invoke<string \| null>\("pick_folder"\)', wizard, re.S)
        is not None,
        "no fallback for a shell built before pick_folders",
    )
    # The drop zone's click is the SAME act as dropping on it: in the shell
    # that means the native picker (files named by path, moved into the
    # library by the server), not the browser's folder input, which would
    # upload their bytes instead — and can only ask for one folder.
    check(
        "the drop zone prefers the native picker inside the shell",
        re.search(r'onClick=\{\(\) => \(IN_TAURI \? pickFolderNative\(\) : pickFolderBrowser\(\)\)\}', wizard)
        is not None,
        "the drop zone's click no longer routes to the native picker in the shell",
    )

    print(f"\n{'PASS' if not fail else 'FAIL'} — {fail} problem(s)")
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())