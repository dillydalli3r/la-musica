#!/usr/bin/env python3
"""Do dependency installs work in the DESKTOP app? Run the packed backend and see.

The desktop installs ship their own backend (`mlo-server`, frozen per OS), and
that backend is where a user presses Install in the Dependencies step. Nothing
else in CI presses that button: the suites call `fetchdeps` in-process, from a
checkout, on one platform. This check runs the *packaged* backend the way the
shell does — loopback, a scratch music folder and app-data dir — and exercises
the real endpoints:

  * Windows / Linux: install a small tool (`oxipng`, ~2 MB) through
    `POST /api/dependencies/install` and prove the binary LANDS under
    `<music>/.mlo/tools/<tool> v<version>/` and comes back `ok` in the rows.
    On Linux the same bundle also has to report the distro tools as `system`
    with the package named, never as a missing row with a dead Install button.
  * macOS: nothing is downloaded there by design (the rows are Homebrew
    formulas), so the check proves the rows SAY so — every brew-backed tool
    carries `brew install <formula>` and no row names an apt package.

Usage:  python tools/check_desktop_deps.py [--bundle desktop/bundle/mlo-server]
Exit codes: 0 pass, 1 a check failed, 2 cannot run here (no frozen backend).
"""
import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from mlo import fetchdeps  # noqa: E402

FAILURES = []


def check(label, cond, detail=""):
    tag = "ok  " if cond else "FAIL"
    print(f"  {tag} {label}{'' if cond or not detail else f'  — {detail}'}")
    if not cond:
        FAILURES.append(label)


def server_binary(bundle):
    for name in ("mlo-server.exe", "mlo-server"):
        cand = os.path.join(bundle, name)
        if os.path.isfile(cand):
            return cand
    return None


def free_port(start=8019):
    for port in range(start, start + 40):
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    return None


def get(url, timeout=30):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.load(r)


def post(url, body, timeout=300):
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"},
        method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def wait_health(base, proc, timeout=120):
    end = time.time() + timeout
    while time.time() < end:
        if proc.poll() is not None:
            return None
        try:
            with urllib.request.urlopen(f"{base}/api/health", timeout=3) as r:
                if r.status == 200:
                    return json.load(r)
        except Exception:
            time.sleep(1)
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bundle", default=os.path.join(ROOT, "desktop", "bundle", "mlo-server"))
    args = ap.parse_args()

    exe = server_binary(args.bundle)
    if not exe:
        print(f"check_desktop_deps: no frozen backend in {args.bundle} — stage it first")
        return 2

    platform = fetchdeps.host_platform()
    print(f"desktop dependency installs — {platform}, backend {exe}")

    tmp = tempfile.mkdtemp(prefix="mlo-deps-check-")
    music = os.path.join(tmp, "music")
    data = os.path.join(tmp, "data")
    os.makedirs(music)
    os.makedirs(data)
    port = free_port()
    if port is None:
        print("check_desktop_deps: no free loopback port")
        return 2
    base = f"http://127.0.0.1:{port}"

    env = dict(os.environ)
    env.update({
        "MLO_MUSIC_FOLDER": music,
        "MLO_APP_DATA_DIR": data,
        "MLO_SERVER_HOST": "127.0.0.1",
        "MLO_SERVER_PORT": str(port),
    })
    proc = subprocess.Popen([exe], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        health = wait_health(base, proc)
        if not health:
            print(f"check_desktop_deps: the backend never answered on {base}")
            return 1
        check(f"the packaged backend answers /api/health ({health.get('version')})", True)

        # The endpoint names them `tools` (+ `checking` while the upstream pass
        # is in flight); the rows themselves are computed synchronously.
        rows = get(f"{base}/api/dependencies").get("tools") or []
        by_key = {r["key"]: r for r in rows}
        check(f"the rows cover the toolchain ({len(rows)} rows)", len(rows) >= 10, str(len(rows)))

        # Every row has to answer "can this machine install it?" without
        # pretending: a missing tool with no note and no Install button is the
        # bug this whole area exists to prevent.
        guessing = [r["key"] for r in rows
                    if r.get("state") == "missing" and not r.get("installable")
                    and not r.get("install_note")]
        check("no missing row is left without a reason", not guessing, ", ".join(guessing))

        if platform == "macos":
            for key, formula in sorted(fetchdeps.BREW_PACKAGES.items()):
                row = by_key.get(key) or {}
                note = str(row.get("install_note") or "")
                check(f"{key}: the row says brew install {formula}",
                      (row.get("state") == "ok" or f"brew install {formula}" in note),
                      note or "(no note)")
            apt_rows = [r["key"] for r in rows if "apt-get" in str(r.get("install_note") or "")]
            check("no macOS row names a Debian package", not apt_rows, ", ".join(apt_rows))
        else:
            tool = "oxipng"
            res = post(f"{base}/api/dependencies/install", {"keys": [tool]})
            results = res.get("results") or []
            entry = results[0] if results else {}
            version = entry.get("version") or ""
            check(f"{tool} installs through the packaged backend ({entry.get('ok')}, {version})",
                  bool(entry.get("ok")), json.dumps(entry)[:200])
            installed = os.path.join(music, ".mlo", "tools", f"{tool} v{version}")
            listing = os.listdir(installed) if os.path.isdir(installed) else []
            binary = next((n for n in listing if n.startswith(tool)), "")
            check(f"...and its binary is on disk ({binary})", bool(binary), str(listing)[:200])
            row = (entry.get("row") or {})
            check("...and the row reads ok afterwards",
                  row.get("state") == "ok" and bool(row.get("path")), json.dumps(row)[:200])
            if platform == "linux":
                system_rows = [r for r in rows if r.get("install_kind") == "system"]
                check(f"distro tools read as system packages ({len(system_rows)})", bool(system_rows))
                pkg_named = all("apt-get install" in str(r.get("install_note") or "")
                                for r in system_rows)
                check("...each naming the package to install", pkg_named,
                      "; ".join(str(r.get("install_note"))[:60] for r in system_rows[:3]))
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()
        shutil.rmtree(tmp, ignore_errors=True)

    if FAILURES:
        print(f"{len(FAILURES)} failure(s)")
        return 1
    print("OK check_desktop_deps")
    return 0


if __name__ == "__main__":
    sys.exit(main())