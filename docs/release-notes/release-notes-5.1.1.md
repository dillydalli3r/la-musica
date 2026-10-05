# la musica 5.1.1 - an install that runs itself

5.1.0 is the release that stopped the desktop app from being only a client: the
Windows, macOS and Linux shells run the app's own backend. 5.1.1 is where that
is checked by *running* what ships rather than by reading it — and one fix for
what happens when you launch the shell twice.

## Every installer is started, not just opened

- **The bundled backend is launched out of the artifact.** CI unpacks each
  install — the deb and the AppImage on Linux, the `.app` on macOS — runs
  `mlo-server` from it exactly as the shell does (loopback, a scratch data dir)
  and asks it for `/api/health` and for the SPA it is supposed to serve. This is
  the check whose absence let the first 5.1.0 installers ship a backend that
  died before it bound a port: a frozen PyInstaller build has no console, so
  uvicorn was configuring a formatter for a stream that does not exist.
- **The installed Linux app is launched for real.** Under Xvfb and a session
  bus, the built app is started and the assertion is its OWN backend answering
  on 8011+. On Linux the backend's location is *derived* rather than stored —
  `/usr/lib/la musica/...` for a deb, `${APPDIR}/usr/lib/...` for an AppImage,
  both carrying the space in the product name — and nothing short of running the
  shell proves that resolution.
- **Windows** is covered by the payload check in CI and, on the machine these
  builds come from, by installing the NSIS build, launching it with no console
  and reading a 200 from `/api/health`.

## The first run asks which backend

- **A choice, not an assumption.** A desktop shell that has never been told
  which backend to use now shows two options instead of silently starting the
  bundled one: **use the built-in backend** (this app runs its own server —
  nothing to install) or **connect to a server you run** (the Docker container,
  a laptop, a home server). The answer is written to `shell.json` *before*
  anything is spawned or navigated, so a shell that already answered is never
  asked again, and an install that predates this keeps the mode it recorded.
  (A shell with no recorded mode — every install until now — is asked once.)
- **The same question lives in the tray.** "Use the built-in backend" switches
  back to the bundled server, and unchecking it hands the window back to the
  wizard that asks which server to talk to. That matters because a page served
  by a *remote* server cannot call the shell at all: the tray is the only way
  back to the built-in backend without editing a JSON file.

## One shell per machine

- A second launch brings the running window forward instead of starting
  anything. Two shells in local mode meant two backends writing the same
  `<music>/.mlo/data`, two servers on 8011/8012 and two tray icons — and the
  ordinary way to get two is the login auto-start plus a click on the icon.
  (`tauri-plugin-single-instance`, keyed on the bundle identifier, desktop
  only: a phone app gets one process from its OS.)

## Smaller

- **The bundle's resource map travels with the build that stages it**
  (`src-tauri/tauri.bundle.conf.json`, and CI's own `--config`), so a plain
  `cargo check` in a checkout that has never staged a 300 MB backend stays
  clean.
- **The deb's icon check looks at the icons the package installs** rather than
  at every PNG inside it: the bundled backend brings the SPA's own images, and
  a package path with a space in it does not survive a loop that splits on
  whitespace.
