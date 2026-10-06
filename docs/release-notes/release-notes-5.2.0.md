# la musica 5.2.0 - the window is the app's, and the ratings are yours

The desktop window was the last part of this app that the app did not draw: a
stock Windows title bar, links that did nothing the moment they left the library,
and a rating number you could read but not write. 5.2.0 takes the window over,
hands every link out to your browser, and makes the number beside the stars a
field. A terminal no longer flashes on the Dependencies page, the fullscreen
player's outgoing lyric line stops coming back to life a second later, and the
dev bed an agent runs now has a container form.

## The window belongs to the app

The shell runs **undecorated** (`decorations: false`) and draws its own title
bar — `web/src/components/TitleBar.tsx`: the icon and name on the left, minimize,
maximize/restore and close on the right, and everything between them dragging the
window (Tauri's own `data-tauri-drag-region` handler, so a double click still
maximizes). It is rendered on every screen the shell has, the setup wizard and the
address/login screens included: an undecorated window with no bar there would have
had no way left to be moved **or closed**. It sits above the fullscreen player and
above dialogs, because the window controls are the one thing that must never be
painted over.

Its three glyphs are drawn here rather than taken from the icon set. Lucide's
`Minus`, `Square` and `X` fill 58 %, 75 % and 50 % of their own 24×24 box, so at
one icon size the cross read smaller and lighter than the two beside it — the
owner's "the x doesn't seem as consistent as the other buttons". Window chrome has
its own metrics instead: one 10×10 box, one stroke, `crispEdges` on the two
axis-aligned ones.

The macOS build draws the same bar with its controls on the **left**, in its own
order. The point of a custom bar is that it looks native, and a Mac with Windows'
three buttons in the corner is the tell that it is not.

## Links out leave the app

Every "leaves the app" link — the credits in the sidebar's corner, MusicBrainz,
the notification tray's release notes — was an ordinary `target="_blank"` anchor,
and inside a webview those did **nothing at all**: a webview has no tabs, and wry
denies the new-window request outright. The page hands external URLs to the shell
now (`web/src/lib/externalLinks.ts` → `open_external` in
`desktop/src-tauri/src/lib.rs`: `ShellExecuteW` on Windows, `open`/`xdg-open`
elsewhere, and only `http`, `https` and `mailto` — a command that feeds the OS
opener is a door worth keeping narrow). Modified clicks (Ctrl/Shift/Cmd) stay the
browser's business, and same-origin links are untouched: the router's own anchors
are not "outside".

The tray's link goes through the same opener by a registered seam
(`registerOpener`), because `lib/notifications.ts` must stay loadable with no DOM
at all — `tools/test_notifications.cjs` loads it bare.

## The rating number is a field

The number beside the stars was a label. It is an input now — click or Tab into
it, type, **Enter** sets it, **Escape** puts it back — everywhere the *user's own*
rating is printed on a control they can change: the track and album pages, the
player bar, the table rows. The web reading (the "3.9 Album Web" a script
fetched) stays a label: it is not yours to type. A display-only control (an album
card, whose rating is derived) keeps the plain text it always had.

What a typed value *means* lives in `lib/ratings.ts`, beside the rest of the
scale's rules, and it is deliberately conservative: the value snaps onto the
half-star grid (`3,7` is 3.5, `3.78` is 4, `2.25` is 2.5), a **comma is a decimal
point** (half the world's locales type it that way), and anything that is not a
rating at all — empty, whitespace, `abc`, `.`, `NaN`, a negative — writes
**nothing**. 0 means "unrated" here, so a typo must never clear a rating. Above
the top is plainly "the most": it lands on 5.

Two layout rules came out of the check that guards this surface
(`tools/check_rating_alignment.mjs`): a **row** keeps its reserved readout box, so
its stars cannot drift as the readout changes shape; an **inline** control (the
album header) keeps its natural width. That check needed one honest change to see
the new shape at all — a field's value is not text, so `textContent` and `Range`
cannot measure it; it is read as its value, in the readout's own font.

## The previous lyric line stops coming back

In the fullscreen player, the line that just left used to come back to life about
a second later. The inactive line carried a `:hover` reveal, and the pane follows
the clock: every line change glides the words up one row, which parks the outgoing
line underneath whatever pointer was resting over the pane. Nothing the reader did
had changed — a stationary pointer was moving the emphasis. A line's emphasis is a
function of the clock alone now. The keyboard half (`:focus-within`) stays,
because that one follows a deliberate act.

## A terminal no longer flashes

`mlo/deps.py`'s spawn probe was the only console child in `mlo/` and `server/`
launched without `CREATE_NO_WINDOW`. The server runs windowed and owns no console,
so a bare `cmd.exe` allocated one of its own — a terminal flash on the first
Dependencies request of a run, which is exactly where it was reported. It now
passes the same flag (and the same reason) every other console child in the app
already carried.

## Smaller

* The sidebar's right border is gone. Tone separates the rail from the content
  (`bg-panel` against `bg-bg`), which is what the rest of the app's separations do.
* The dev bed an agent runs has a container form: `docker-compose.dev.yml` — the
  app on **8011**, vite with hot reload on **5181**, the scratch library at
  `local/dev/music`, its own project/container/port namespace, loopback-only
  publishing and no watchtower, so it can run beside the real install on 8000
  without either seeing the other's state. Two details are load-bearing rather
  than cosmetic: vite shares the app's network namespace (anything but loopback
  and the default gateway is a CLIENT to the login gate, so a second container
  proxying over the compose network would have made the dev URL ask for a
  password nobody set), and `node_modules` is a named volume (`npm ci` inside
  Linux over a bind-mounted `web/` would have replaced the host's copy with Linux
  binaries and broken the host's own `npm run dev`). **It is not proved by this
  release**: no container runtime was available on the machine that wrote it, so
  the file is parsed and reviewed rather than run —
  `docker compose -f docker-compose.dev.yml up -d --build` is what proves it.
* `AGENTS.md` records what came out of this release: a console child gets
  `CREATE_NO_WINDOW`; the shell is undecorated and the web app draws its window,
  so no window the app did not open may appear; links out go through the shell;
  and the container bed above, with the two things a container cannot do here —
  the desktop shell (a Tauri/Windows app, host-only by nature) and anything about
  console windows, the title bar or the tray.

## The media flyout still says "Unknown app"

Reported alongside the rest, and **not** fixed, because it is not fixable from
where it stands. Windows resolves a media session's app name from its
`SourceAppUserModelId`, and the session that plays the music belongs to the
WebView2 runtime, not to this shell: a live query on the running app answers
`msedgewebview2.exe`, while the shell's own AUMID (set in `main.rs`, and already
stamped on the Start Menu shortcut the installer writes) never reaches it. There
is no lever to hand one over either — Chromium has no `--app-user-model-id` switch
(checked in `chrome_switches.h` and in the runtime's own binary) — and the
WebView2 issue that tracks this (MicrosoftEdge/WebView2Feedback#2236) has been
open since 2022 with no work happening. A real fix means owning the System Media
Transport Controls natively *and* moving playback out of the webview: a project of
its own, not a patch.

## What proved it

Every claim above was checked against the running app rather than reasoned about:

* **the link**: clicking the sidebar's MusicBrainz link in the desktop shell
  opened it in the system browser (the window title became MusicBrainz's) while
  the webview stayed on the library;
* **the window**: `is_decorated` false, minimize and maximize (the icon swaps to
  Restore) and close (hides to the tray) round-trip, and a capture of the window
  shows no OS chrome;
* **the field**: typed `3,7` against a scratch server and read **7/10** back from
  `/api/ratings`; then `7` → 5, `abc` → no write, `0.2` → cleared, Escape →
  restored;
* **the lyric line**: an A/B run in a real browser with the pointer parked over
  the pane — with the old `hover:` half restored the hovered-but-inactive line
  computed `opacity 1 / blur none`, and with the fix `0.9 / blur(1px)`, geometry
  identical in both;
* **the terminal**: spawned from a console-less parent, the child reported a
  console window without the flag and none with it.

Suites (`tools/test_*.py`, `tools/test_*.cjs`), the frontend gate (`npx tsc -b`,
`npx oxlint`, `npm run build`) and every payload-driven UI check for a touched
surface (rating alignment, backend choice, lyric clock, lyrics kind, library a–z,
accent) pass, and `python tools/check_versions.py v5.2.0` says all ten copies of
the version agree.