import { invoke } from "@tauri-apps/api/core";
import { IN_TAURI } from "../api";

/** Follow a link that leaves the app. In the DESKTOP shell that means the OS
 *  browser, through the shell's own `open_external` command
 *  (desktop/src-tauri/src/lib.rs). Webviews have no tabs and wry denies a
 *  `target="_blank"` window outright, so these clicks used to do nothing at
 *  all — which is exactly the report this fixes. A command that fails still
 *  gets the link out of the door: `window.open` is inert in a shell but
 *  correct in a browser, so the fallback costs nothing and covers a shell
 *  older than the command.
 *
 *  Everywhere else (a plain browser) this is plain `window.open`, which is
 *  what those clients already did. */
export function openExternal(url: string) {
  if (IN_TAURI) {
    invoke("open_external", { url }).catch(() => {
      window.open(url, "_blank", "noreferrer");
    });
    return;
  }
  window.open(url, "_blank", "noreferrer");
}

/** One click listener, capture phase, for the whole app — the shell's, since
 *  it is the shell that has to do something special. A browser needs nothing
 *  installed: it already follows these anchors correctly. */
export function attachExternalLinks() {
  if (!IN_TAURI) return;
  document.addEventListener(
    "click",
    (e) => {
      // Modifier- and middle-clicks are the user asking for a tab, a window or
      // a download: the browser's call, never ours.
      if (e.defaultPrevented || e.button !== 0) return;
      if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
      const anchor = (e.target as Element | null)?.closest?.("a[href]");
      if (!anchor || anchor.hasAttribute("download")) return;
      const raw = anchor.getAttribute("href") ?? "";
      // "#..." is a position in this document, not a place to go.
      if (!raw || raw.startsWith("#")) return;
      let url: URL;
      try {
        url = new URL(raw, location.href);
      } catch {
        return;
      }
      // Only links that actually leave this document: the router's own
      // `NavLink`s are real anchors too and are none of this listener's
      // business. Protocol and host are compared by hand rather than through
      // `URL.origin`, which is the literal string "null" for the custom schemes
      // the shell serves from (`tauri://localhost`) — there every comparison
      // against it would claim the link is external.
      if (url.protocol === location.protocol && url.host === location.host) return;
      e.preventDefault();
      openExternal(url.href);
    },
    true,
  );
}
