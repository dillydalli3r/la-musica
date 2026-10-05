# la musica 5.1.0 - the desktop app is the whole app

Until now the desktop window was a client: it wanted a server you had started
yourself (Docker, a laptop, a home server) and told you so with a sign-in
screen and an address field. This release makes the Windows, macOS and Linux
apps run the backend themselves, and fixes what a packaged webview needs to be
a first-class client.

## The desktop shell ships its own backend

- **One installer, no Docker.** The Tauri shell spawns the app's own backend —
  `mlo-server`, the whole server frozen per-OS by PyInstaller and packed beside
  the shell binary — on a free loopback port from 8011 up, waits for
  `/api/health`, and then points the window at it. The SPA and its API come from
  the same origin, so the packaged app behaves exactly like the browser build.
- **A browser or a phone keeps the classic flow.** Remote mode is unchanged: a
  client still points at the same server every other client uses, and the
  "which server?" wizard still exists for it.
- **Tray.** Live local-server status (running / starting / stopped), Pause and
  Resume, Open logs, and **Keep backend running after quit** for anyone who
  wants the server to outlive the window. Closing the window hides the app to
  the tray; quitting stops the backend it started, unless you asked it not to.
- **It is an app, not a service.** All state still lives in the `.mlo` folder
  beside your music; the shell keeps only its own settings (local/remote, the
  tray choices) in your user profile.

## Cookies, RYM and importing work in the packaged app

- **The session cookie round-trips.** Because the bundled backend serves the SPA
  and its API from one origin, the `HttpOnly` session cookie is sent on every
  call inside the webview — the thing that made cookie-gated behaviour fail in a
  packaged shell, where the page was one origin and the API another.
- **RYM behaves.** The RateYourMusic cookie (session plus `cf_clearance`) is
  imported from your browser, stored in `<music>/.mlo/data/config.json` with its
  expiry recorded, and sent with each RYM request; foreign or junk cookies are
  rejected rather than written.
- **Import walks the same path it always did**, now end to end inside the
  bundled app: drop an archive, unpack, identify, import — the album lands on
  disk.

## Smaller

- **No more startup flash.** The window opens on a static splash and only
  navigates once the backend answers, so a booting server no longer paints
  WebView2's "can't reach 127.0.0.1" page for a moment first.
- The bundle is staged automatically by `tauri build`
  (`tools/stage_desktop_bundle.py`) and built per-OS in CI; the staged output is
  gitignored, never committed.
- Mobile is untouched: an Android or iOS build has no Python and no backend — a
  phone points at the server, exactly as before.
