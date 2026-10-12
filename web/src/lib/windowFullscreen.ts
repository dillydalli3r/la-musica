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
 *  commands are desktop-shaped. */
import { getCurrentWindow } from "@tauri-apps/api/window";
import { IN_MOBILE_SHELL, IN_TAURI } from "../api";

/** True when a fullscreen toggle must move the OS WINDOW, not the document.
 *  False in a browser and on a phone — both keep `document.fullscreenElement`
 *  as their source of truth. */
export const WINDOW_FULLSCREEN = IN_TAURI && !IN_MOBILE_SHELL;

/** Flip the window fullscreen and give back the state it landed in. Reading it
 *  afterwards rather than assuming the toggle took is what keeps the button
 *  honest on a platform that clamps a request. */
export async function toggleWindowFullscreen(): Promise<boolean> {
  const win = getCurrentWindow();
  const on = await win.isFullscreen().catch(() => false);
  await win.setFullscreen(!on).catch(() => {});
  return win.isFullscreen().catch(() => !on);
}

/** Leave window fullscreen if it is on — the viewer's own close path, which
 *  must not disturb a merely windowed or maximized window. */
export function leaveWindowFullscreen(): void {
  const win = getCurrentWindow();
  win.isFullscreen()
    .then((fs) => { if (fs) return win.setFullscreen(false); })
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