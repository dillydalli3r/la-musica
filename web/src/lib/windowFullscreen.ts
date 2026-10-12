/** Putting the WINDOW fullscreen, for the desktop shell.
 *
 *  The fullscreen player's top-bar button used to call the document API
 *  everywhere (`documentElement.requestFullscreen`). In a browser that fills
 *  the screen — the browser hands its window over and hides its own chrome. A
 *  packaged install is a different animal: the page is a WebView2 webview, and
 *  the document API paints the WEBVIEW fullscreen while the OS window keeps its
 *  own size and the taskbar stays where it was, so the button read as dead
 *  (owner report). The shell's own window fullscreen (`setFullscreen`) is the
 *  one that actually covers the screen, so on the desktop the button drives
 *  THAT, and a plain browser keeps the document path.
 *
 *  A phone or tablet shell keeps the document path too: `IN_MOBILE_SHELL` is a
 *  device whose window already IS the screen, and `core:window`'s fullscreen
 *  commands are desktop-shaped.
 *
 *  A MAXIMIZED window is the trap, and it is the owner's own report
 *  (2026-10-12: "the fullscreen button doesn't fullscreen the window … black
 *  space at the bottom … and the top bar is still visible"). Asking tao for
 *  borderless fullscreen while the window is maximized moves and sizes the
 *  WINDOW to the monitor, but Windows keeps the MAXIMIZED state: the CLIENT
 *  area stays the work area (measured on the host shell at 1920×1040 of a
 *  1080p screen — the bottom 40 px of the window then paint as nothing, the
 *  black band in their screenshot) and NO resize fires, which is why the
 *  in-page title bar never hides (`TitleBar` only re-reads the state on a
 *  resize). The same call from a restored window measured innerHeight 1080 and
 *  `--mlo-titlebar-h` 0, i.e. the real thing. So entering fullscreen RESTORES
 *  the window first (`unmaximize`), and leaving puts the maximized state BACK —
 *  tao's exit restores the placement it captured when fullscreen began, which
 *  is the restored one by then, so without that the reader's maximized window
 *  would come back as a small window. Both commands are granted to this page
 *  in `desktop/src-tauri/capabilities/default.json` (`allow-unmaximize` /
 *  `allow-maximize`); without them the page's answer is a refusal, which is
 *  exactly what a missing permission looked like here.
 */
import { getCurrentWindow } from "@tauri-apps/api/window";
import { IN_MOBILE_SHELL, IN_TAURI } from "../api";

/** True when a fullscreen toggle must move the OS WINDOW, not the document.
 *  False in a browser and on a phone — both keep `document.fullscreenElement`
 *  as their source of truth. */
export const WINDOW_FULLSCREEN = IN_TAURI && !IN_MOBILE_SHELL;

/** Entering fullscreen from a MAXIMIZED window unmaximizes it (see the module
 *  doc), and this remembers that so LEAVING can restore the maximized state:
 *  tao's exit re-applies the placement it saved when fullscreen began, which
 *  is the restored placement. Set on every ENTER (false when the window was
 *  not maximized — a stale `true` must not maximize a window nobody
 *  maximized), cleared on every leave. */
let wasMaximized = false;

async function goFullscreen(on: boolean): Promise<void> {
  const win = getCurrentWindow();
  if (on) {
    wasMaximized = await win.isMaximized().catch(() => false);
    if (wasMaximized) await win.unmaximize().catch(() => { /* gone */ });
    await win.setFullscreen(true).catch(() => { /* gone */ });
    return;
  }
  await win.setFullscreen(false).catch(() => { /* gone */ });
  if (wasMaximized) {
    wasMaximized = false;
    await win.maximize().catch(() => { /* gone */ });
  }
}

/** Flip the window fullscreen and give back the state it landed in. Reading it
 *  afterwards rather than assuming the toggle took is what keeps the button
 *  honest on a platform that clamps a request — and when even the READ is
 *  refused (a shell without the permission), the honest answer is the state it
 *  started from, never an optimistic flip. */
export async function toggleWindowFullscreen(): Promise<boolean> {
  const win = getCurrentWindow();
  const on = await win.isFullscreen().catch(() => false);
  await goFullscreen(!on);
  return win.isFullscreen().catch(() => on);
}

/** Leave window fullscreen if it is on — the viewer's own close path, which
 *  must not disturb a merely windowed or maximized window. */
export function leaveWindowFullscreen(): void {
  const win = getCurrentWindow();
  win.isFullscreen()
    .then((fs) => { if (fs) return goFullscreen(false); })
    .catch(() => { /* gone */ });
}

/** Watch the window's fullscreen state: call `cb` with the real state now and
 *  on every resize. A fullscreen change IS a resize (the window takes the
 *  screen and gives it back), so this is what makes the button follow an F11 or
 *  an OS fullscreen the app never asked for — the app's own
 *  `fullscreenchange` never fires for a window the OS resized. Resolves to the
 *  unsubscribe. */
export async function watchWindowFullscreen(cb: (fs: boolean) => void): Promise<() => void> {
  const win = getCurrentWindow();
  const read = () => { void win.isFullscreen().then(cb).catch(() => {}); };
  read();
  return win.onResized(read);
}