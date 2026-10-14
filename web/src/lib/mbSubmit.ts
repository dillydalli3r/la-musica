import { useQuery } from "@tanstack/react-query";
import { getToken, serverUrl } from "../api";
import type { Album, Library } from "../types";

/** MusicBrainz login + rating submissions, web side.
 *
 *  Reads need no login — the whole in-app MusicBrainz browser is anonymous.
 *  ONLY writes (pushing the user's own ratings back) need the account, so this
 *  module owns exactly that half: the OAuth2 connect/finish/disconnect calls,
 *  and the submission of the files a page is showing. The server keeps the
 *  tokens (`server.mb_submit`); the browser never sees them — the PKCE
 *  verifier is the one secret that lives only here, in the panel's state,
 *  between the authorize click and the pasted code.
 *
 *  The config keys the server reports through `/api/mb/auth/status` are
 *  `musicbrainz_submit_enabled` (the gate), `musicbrainz_access_token` (the
 *  short-lived token, so `has_access_token`) and
 *  `musicbrainz_token_expires_at` (its epoch, so `expires_at`). They are named
 *  here, not only in the wizard's registry, because this panel is the surface
 *  that renders them outside Settings. */

/** `GET /api/mb/auth/status` — what the server holds. */
export interface MbAuthStatus {
  connected: boolean;
  username: string;
  client_id_set: boolean;
  /** `musicbrainz_submit_enabled` — the switch the submit route gates on. */
  submit_enabled: boolean;
  /** Whether `musicbrainz_access_token` is stored (the token renews itself). */
  has_access_token: boolean;
  /** `musicbrainz_token_expires_at`, epoch seconds (0 = never fetched). */
  expires_at: number;
  detail: string;
}

export interface MbConnect {
  ok: boolean;
  /** The authorize URL to open, empty when there is no client id saved. */
  url: string;
  /** The PKCE verifier to hand back with the pasted code. */
  verifier: string;
  detail: string;
}

export interface MbFinish {
  ok: boolean;
  checked?: string;
  detail: string;
  username?: string;
  status?: MbAuthStatus;
}

/** One file's own result: what was sent (and the rating it came from), or the
 *  sentence explaining why nothing was. */
export interface MbSubmitRow {
  path: string;
  /** "track" (a file) or "album" (the folder's release group). */
  kind: "track" | "album";
  title: string;
  entity: string;
  mbid: string;
  /** The app's rating on its own 0-100 scale, before snapping. */
  value: number;
  /** The snapped value MusicBrainz was sent, or null when skipped. */
  sent: number | null;
  status: "sent" | "skipped";
  reason: string;
}

export interface MbSubmitResult {
  ok: boolean;
  sent: number;
  skipped: number;
  /** MusicBrainz's own refusal, when it refused. */
  detail: string;
  username?: string;
  rows: MbSubmitRow[];
}

/** One call to this module's own routes, with the session the rest of the app
 *  sends (the `mlo_session` cookie, or the Bearer token for a client pointed
 *  at another server). A route's refusal is thrown as an Error carrying its
 *  `detail`, so the panel can show the service's own words. */
async function mbFetch<T>(path: string, body?: unknown): Promise<T> {
  const token = getToken();
  const res = await fetch(`${serverUrl()}/api${path}`, {
    method: body === undefined ? "GET" : "POST",
    credentials: "include",
    headers: {
      ...(body === undefined ? {} : { "Content-Type": "application/json" }),
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
    },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const text = await res.text();
  let payload: unknown = null;
  try {
    payload = text ? JSON.parse(text) : null;
  } catch {
    payload = null;
  }
  if (!res.ok) {
    const detail = (payload as { detail?: string } | null)?.detail;
    throw new Error(detail || `MusicBrainz request failed (HTTP ${res.status})`);
  }
  return payload as T;
}

export const mbConnect = () => mbFetch<MbConnect>("/mb/auth/connect", {});
/** Finish the login with the code MusicBrainz shows (a bare code or the whole
 *  redirect URL — the server reads either). `verifier` is only needed by an
 *  older server: the current one keeps the PKCE verifier itself and takes it
 *  from the stored login when the body carries none. */
export const mbFinish = (code: string, verifier?: string) =>
  mbFetch<MbFinish>("/mb/auth/finish", verifier ? { code, verifier } : { code });
export const mbDisconnect = () => mbFetch<MbAuthStatus>("/mb/auth/disconnect", {});
export const mbSubmitRatings = (paths: string[]) =>
  mbFetch<MbSubmitResult>("/mb/ratings/submit", { paths });

/** What the rating-upload queue is doing (GET /api/mb/ratings/queue): the
 *  folders waiting, and the newest per-album results. */
export interface MbRatingsQueueState {
  ok: boolean;
  connected: boolean;
  enabled: boolean;
  pending: number;
  running: boolean;
  recent: {
    path: string;
    name: string;
    ok: boolean;
    sent: number;
    skipped: number;
    detail: string;
    at: number;
  }[];
}

export const mbRatingsQueue = () => mbFetch<MbRatingsQueueState>("/mb/ratings/queue");
/** Queue EVERY album this library has a rating for — the "upload all my
 *  ratings" press. Answers at once with how many went into the queue; the
 *  queue route above reports the progress. */
export const mbRatingsSubmitAll = () =>
  mbFetch<{ ok: boolean; queued: number }>("/mb/ratings/submit-all", {});

/** The upload queue, polled while the panel is on screen (the worker drains
 *  in the background, so this is what makes an in-flight upload visible). */
export function useMbRatingsQueue(enabled = true) {
  return useQuery({
    queryKey: ["mbRatingsQueue"],
    queryFn: mbRatingsQueue,
    enabled,
    refetchInterval: (q) =>
      q.state.data?.pending || q.state.data?.running ? 2000 : 15000,
  });
}

/** The saved login, for the panel (and the MusicBrainz page's action). */
export function useMbAuth() {
  return useQuery({
    queryKey: ["mbAuth"],
    queryFn: () => mbFetch<MbAuthStatus>("/mb/auth/status"),
    staleTime: 15000,
  });
}

/** The library album whose own MusicBrainz tags name any of `ids`, or null.
 *
 *  The same evidence the page's "In library" badge reads (MUSICBRAINZ_ALBUMID /
 *  MUSICBRAINZ_RELEASEGROUPID), so "Submit ratings" can only ever offer files
 *  the app believes are this very release. */
export function albumForIds(lib: Library | undefined, ids: (string | null | undefined)[]) {
  const wanted = ids
    .filter(Boolean)
    .map((x) => String(x).toLowerCase());
  if (!wanted.length) return null;
  for (const artist of lib?.artists ?? []) {
    for (const al of artist.albums ?? []) {
      if (al.pending) continue;
      const meta = al.meta ?? {};
      const have = [meta.MUSICBRAINZ_ALBUMID, meta.MUSICBRAINZ_RELEASEGROUPID].map((v) =>
        String(v ?? "").toLowerCase()
      );
      if (have.some((h) => h && wanted.includes(h))) return al;
    }
  }
  return null;
}

/** Every file of an album, and the folder itself (for the release-group row). */
export function albumFiles(album: Album | null | undefined): string[] {
  return (album?.tracks ?? []).map((t) => t.path).filter(Boolean);
}

/** The local files carrying one recording MBID (a recording page), plus the
 *  album folder they share when they all share one. */
export function trackFiles(lib: Library | undefined, recordingId: string) {
  const want = String(recordingId || "").toLowerCase();
  const paths: string[] = [];
  let folder = "";
  if (!want) return { paths, albumPath: folder };
  for (const artist of lib?.artists ?? []) {
    for (const al of artist.albums ?? []) {
      if (al.pending) continue;
      for (const t of al.tracks ?? []) {
        const got = String(t.values?.MUSICBRAINZ_TRACKID ?? "").toLowerCase();
        if (got && got === want) {
          paths.push(t.path);
          if (!folder) folder = al.path;
          else if (folder !== al.path) folder = "";
        }
      }
    }
  }
  return { paths, albumPath: folder };
}