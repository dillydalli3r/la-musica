# la musica 5.3.0 - the app updates itself, and its window works

An installed la musica now learns about a new release by itself and, on the
desktop, installs it from inside the app: a signed download, a real percentage,
a restart into the new build. Other clients get the notice that fits what they
actually are — a browser and a phone a link, the Docker image the updater it
already has. Alongside it, the bug that made 5.2.0's title bar look broken:
on a packaged install the window could not be moved, minimized, maximized or
closed, because the page the shell shows had no permission to ask for any of it.

## Update, from the app

The desktop shell carries Tauri's updater: it asks
`releases/latest/download/latest.json` for a manifest signed with the release
key, compares it against **its own** version (not the server's — in remote mode
that number says nothing about this binary), and offers what it finds in
Settings → Security, in the row `tools/check_update_row.mjs` pins in a browser.
On Windows the plugin hands the download to the NSIS installer (`installMode:
quiet`) and the installer restarts the app as the new build; on macOS and Linux
the bundle is swapped in place and the shell re-execs itself, the way those
platforms expect.

At launch the check is silent unless there is something to say: one tray entry
per version (unread, idempotent, still there on the next launch) and one toast,
with the first announcement only. Told, not nagged.

The progress bar is the part that had to be earned. `on_chunk`, the plugin's
download callback, reports the **size of each chunk** — not a running total —
so a threshold built on it never fires: the first version of this shipped that
way and a 40 MB download reported nothing at all. It accumulates now, and the
stream a real update produced reads `1,2,4,…,99,100,installing`.

**One thing to know before this helps you**: the copies out there are 5.2.0,
which has no updater in it. This release is therefore a **one-time manual
install**; every release after it arrives in-app.

## The window's controls work on the page the shell actually shows

"the window won't move, but can be resized. Also none of the control buttons
work." That asymmetry was the whole diagnosis: resizing is native hit-testing
with no script involved, while dragging, minimize, maximize and close are
`plugin:window` commands — and a packaged install points the window at the
backend it spawned on loopback, which Tauri counts as a **remote** page. The
capability matched `URL: local` only, so the shell refused its own UI every one
of those calls. In development it never showed, because the dev URL is treated
as the app's own origin — which is also why 5.2.0's verification (the controls
round-tripped, in dev) did not catch it.

Two halves fix it, and they are both about being explicit:

* `capabilities/default.json` gains a `remote` list — `http://127.0.0.1:*` and
  `http://localhost:*`, the loopback origins this shell itself serves, and
  nothing else. A server on the LAN or over Tailscale serves its own page and
  still gets none of it; in remote mode the shell loads its own assets, so that
  page is local to begin with.
* `build.rs` declares the app-command manifest. Without it an app command has no
  permission to be *granted* at all — only plugin commands were covered, which
  is why the same refusal also killed `pick_folder`, `open_external`,
  `set_music_folder`, `shell_backend_choice`, the iOS bridge **and the updater's
  own `update_check`/`update_install`** on that page. Desktop grants all ten,
  mobile the five mobile calls, and its command list must stay equal to
  `generate_handler!`.

## Smaller

* The first-run chooser's second card reads **"Connect to a server"** (it said
  "Connect to a server I run"; the qualifier was doing no work), in all six
  locales.
* `AGENTS.md` records the rule this cost: the shell's capabilities must cover
  the page the window loads, and the app manifest and `generate_handler!` must
  name the same commands.
* The Windows media flyout still says "Unknown app" — unchanged from 5.2.0's
  note, which is where the measurement and the reason live. Nothing here
  touches it.

## What proved it

* **the window controls, against the OS rather than the return value**: on a
  loopback page in a built shell, each control clicked in turn and the window
  read from outside — maximize (1456×909 → the screen, the icon flipping to
  Restore and back), minimize (`IsIconic` true, 160×28), close (window off the
  screen; this shell hides to the tray by design), and `start_dragging`
  accepted where it used to be refused. Repeated in a **release** build, since
  that is the one that ships.
* **the whole app on a real server over loopback**, which is the shape a
  packaged install runs: `shell_backend_choice` answers, Settings → Security
  draws the update row, and its "Check now" reaches the signed updater.
* **a real update, end to end**: an installed 5.2.0 updated itself to a build
  served from loopback — the shell exited, the installer ran silently, the app
  came back as the new version (file version, registry entry and process all
  matching) — then twice more, with the progress stream read out of the second
  and third runs as `1,2,4,…,99,100,installing`.
* **the manifest tool**: `tools/test_updater_manifest.py` pins every platform
  key Tauri's updater asks for, the bare-key choice, and the loud failure when a
  platform's updater artifact is missing — because a platform missing from
  `latest.json` is the one failure that would go unnoticed.
* Suites (`tools/test_*.py`, `tools/test_*.cjs`), the frontend gate (`npx tsc
  -b`, `npx oxlint`, `npm run build`) and the update-row check pass.

Not verified here: the macOS and Linux **install** paths (this machine can only
build and drive the Windows one), and the workflow's signing step, which needs a
tag — this release's own run is what exercises it.