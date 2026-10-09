/** Playback black box: what the element, the OS and the shell each did, in order.
 *
 *  ## Why this module exists
 *
 *  The owner's iOS reports — "audio stops playing when I tab out" and "the
 *  lock-screen controls do nothing" — are unobservable from a dev box: a
 *  desktop browser does not background-kill a webview's decoder, an Android
 *  phone does not park the audio session the way iOS does, and the machine that
 *  would show the console is not the machine with the bug. Every round of
 *  guessing so far (audio session, WebAudio context, mediaSession handlers,
 *  gapless handover) could only be argued from the code. This is the black box
 *  that ends the arguing: the events below are the ones that DISCRIMINATE
 *  between the candidate causes, and the Settings panel
 *  (`settings.playback_diag`) reads them back on the device that misbehaved, so
 *  the owner can photograph or copy the report instead of describing it.
 *
 *  ## What it records, and what it must never become
 *
 *  Only state CHANGES and OS/app DECISIONS: the element's own play/pause/ended/
 *  error/stalled/waiting, track loads, seeks (and who asked), visibility and
 *  page-show/hide, every mediaSession action that reaches the page, the two iOS
 *  bridge pushes, and the shared AudioContext's state. The single most valuable
 *  row is a `pause` carrying `asked: false` while `vis: hidden` — the element
 *  stopping on its own, with the app's back turned. There is deliberately NO
 *  per-second and NO per-frame note: a 40-slot ring buffer of everything is a
 *  buffer of nothing, so the timeupdate/meter paths must stay silent.
 *
 *  ## It must never be able to break playback
 *
 *  Exactly like `lib/iosAudio.ts` and `lib/iosFavs.ts`: `note()` is called from
 *  inside the player's event handlers and from the WebAudio graph's own
 *  listener, where a thrown exception would become a broken pause handler or a
 *  broken analyser. Every entry point is therefore total — an internal failure
 *  drops the event, never the call site — and the module carries no React, no
 *  DOM and no imports, so it also loads in the repo's Node check harnesses. */

/** One recorded fact. `detail` is flat on purpose: it is rendered as `k=v`
 *  pairs and copied as tab-separated text. */
export type DiagEvent = {
  /** Epoch ms — the panel renders it as HH:MM:SS. */
  t: number;
  /** What happened; the discriminating word the panel groups by. */
  kind: string;
  detail?: Record<string, string | number | boolean | null>;
};

/** How many events the ring keeps. Sized for one phone session's worth of
 *  real transitions (a few per track) rather than for a log. */
const CAP = 40;

let events: DiagEvent[] = [];
/** Bumped on every change; `useSyncExternalStore` in the panel reads it. */
let version = 0;
const listeners = new Set<() => void>();

function emit() {
  version++;
  for (const l of [...listeners]) {
    try { l(); } catch { /* a bad subscriber must not cost the event */ }
  }
}

/** Record one event. Never throws: if anything about the call is unusable the
 *  event is dropped and the caller's playback path continues untouched. */
export function note(kind: string, detail?: Record<string, string | number | boolean | null>) {
  try {
    const ev: DiagEvent = { t: Date.now(), kind: String(kind) };
    if (detail) ev.detail = { ...detail };
    events.push(ev);
    if (events.length > CAP) events.splice(0, events.length - CAP);
    emit();
  } catch {
    /* a diagnostic that can throw is worse than no diagnostic */
  }
}

/** The recorded events, oldest first, as a copy — the panel must not be able
 *  to hand `note`'s own array (or an event's detail) to anything that writes. */
export function pbDiagEvents(): DiagEvent[] {
  try {
    return events.map((e) => (e.detail ? { t: e.t, kind: e.kind, detail: { ...e.detail } } : { ...e }));
  } catch {
    return [];
  }
}

/** Drop everything recorded so far. */
export function pbDiagClear() {
  try {
    events = [];
    emit();
  } catch {
    /* see note() */
  }
}

/** The current revision, for `useSyncExternalStore(subscribe, pbDiagVersion)`. */
export function pbDiagVersion(): number {
  return version;
}

/** Subscribe to changes; the returned function unsubscribes. */
export function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => { listeners.delete(listener); };
}

/** The shorthand this report uses for a track path: the BASENAME only.
 *
 *  The report is meant to be photographed, pasted into a chat and shown to
 *  whoever is reading the bug, and a track path carries the owner's own folder
 *  layout (album name, and on a shared box the user's home). The basename is
 *  what identifies the track in a queue anyway — the report is never a
 *  filesystem index. */
export function shortPath(path: string | null | undefined): string | null {
  if (!path) return null;
  const parts = String(path).split(/[\\/]/);
  return parts[parts.length - 1] || null;
}

/** One tick per second, and a note ONLY when a tick arrives late.
 *
 *  Why a gap is the signal that decides between the candidate causes: the
 *  owner's two symptoms are "audio stops" AND "the lock-screen controls do
 *  nothing", and the likely actors are (a) WebKit throttling or freezing this
 *  web content process in the background, (b) the platform pausing the media
 *  element while the process is alive, (c) the app's own process being
 *  suspended while the webview's survives. A `setInterval` is the cheapest
 *  probe of (a): a frozen or suspended page delivers no ticks, so the first
 *  tick after the freeze carries the whole frozen window in its gap, and its
 *  `playing`/`hidden` pair says what the app believed at that moment — a page
 *  frozen for 20 s while it thought a track was playing is (a) or (c), while a
 *  small gap next to the element's own `pause` row (see PlayerBar's handlers)
 *  is (b).
 *
 *  It ticks while the page is HIDDEN — that is the window being measured, so a
 *  heartbeat that only ran while visible would be blind to the only case it
 *  exists for. It records nothing at all while ticks are on time: a report of
 *  forty "still alive" rows is a report of nothing, and the ring would have no
 *  room left for the events that matter.
 *
 *  Started at module load, and guarded exactly like `lib/analyser.ts`'s
 *  gesture listeners: the repo's Node check harnesses import this module
 *  through Vite's module runner, where a `document` stub exists (so a timer
 *  would keep the harness's event loop alive forever) but is not a browser.
 *  `addEventListener` being a FUNCTION is the same test the analyser uses, and
 *  it means "a real page". */
const HEARTBEAT_MS = 1000;
/** A tick this late means the page did not run for a while: three missed
 *  ticks, well clear of ordinary timer jitter and of a busy main thread. */
const HEARTBEAT_GAP_MS = 3000;

/** Read the player's state for the gap row, without dragging the store (and
 *  through it the api module's `window` access) into this module's import
 *  graph: a diagnostic must load in a Node check harness, and the store is only
 *  asked at the moment a gap is reported — which is to say never there. */
async function heartbeatGap(seconds: number) {
  let playing: boolean | null = null;
  try {
    const { useStore } = await import("../store");
    playing = useStore.getState().playing != null;
  } catch {
    /* No store to ask (a check harness, a hot reload mid-import, a store that
     * itself failed): the gap is still the row worth having. */
  }
  note("heartbeat-gap", { seconds, playing, hidden: document.visibilityState === "hidden" });
}

if (typeof document !== "undefined" && typeof document.addEventListener === "function") {
  try {
    let last = Date.now();
    setInterval(() => {
      const now = Date.now();
      const gap = now - last;
      last = now;
      if (gap >= HEARTBEAT_GAP_MS) void heartbeatGap(Math.round(gap / 1000));
    }, HEARTBEAT_MS);
  } catch {
    /* no timer API: every other row of the report still works */
  }
}
