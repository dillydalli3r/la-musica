import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, Check, Loader2, LogOut, Send, Unplug } from "lucide-react";
import { api } from "../api";
import { openExternal } from "../lib/externalLinks";
import {
  albumFiles,
  albumForIds,
  mbConnect,
  mbDisconnect,
  mbFinish,
  mbSubmitRatings,
  trackFiles,
  useMbAuth,
  type MbSubmitResult,
} from "../lib/mbSubmit";
import type { Library } from "../types";
import ConnectGuide from "./ConnectGuide";

/* MusicBrainz login + "Submit ratings" — the one WRITE surface of the
 * MusicBrainz browser (every read on that page is anonymous).
 *
 * The panel is handed the MusicBrainz ids the page is SHOWING (`kind` says
 * whether they name a release/release-group or a recording) and resolves them
 * against the library itself: the user's ratings live on their OWN files, so
 * there is nothing to submit unless this app already holds that release. The
 * server then reads each file's rating from the app's store and pushes one
 * batched request per album — the panel only shows what came back, row by row:
 * `sent 80 (from 80)` or the sentence explaining a skip, and MusicBrainz's own
 * refusal where it refused.
 *
 * The config keys behind the status this draws are named literally, so
 * `tools/test_setup_coverage.py` can see the "Scrobbling & submissions" group
 * is rendered outside the wizard (the server maps each one into the status
 * payload: `musicbrainz_submit_enabled` -> `submit_enabled`,
 * `musicbrainz_access_token` -> `has_access_token`,
 * `musicbrainz_token_expires_at` -> `expires_at`). */
const CONFIG_KEYS = {
  gate: "musicbrainz_submit_enabled",
  token: "musicbrainz_access_token",
  expiry: "musicbrainz_token_expires_at",
} as const;

export default function MbLoginPanel({
  ids,
  kind,
  className = "",
}: {
  /** The MusicBrainz ids the page is showing (release + group, or recording). */
  ids: (string | null | undefined)[];
  /** What those ids name: a library album's release, or a single file. */
  kind: "album" | "track";
  className?: string;
}) {
  const qc = useQueryClient();
  const auth = useMbAuth();
  const [verifier, setVerifier] = useState("");
  const [code, setCode] = useState("");
  const [result, setResult] = useState<MbSubmitResult | null>(null);
  const [error, setError] = useState("");

  // The library is the same query key every page already uses, so a visit
  // that started there costs no extra request.
  const { data: lib } = useQuery<Library>({
    queryKey: ["library"],
    queryFn: () => api.library(),
    staleTime: 30000,
  });

  const album = kind === "album" ? albumForIds(lib, ids) : null;
  const recording = kind === "track" ? trackFiles(lib, String(ids[0] ?? "")) : null;
  const paths = kind === "album" ? albumFiles(album) : recording?.paths ?? [];
  const connected = !!auth.data?.connected;

  const connect = useMutation({
    mutationFn: mbConnect,
    onSuccess: (got) => {
      if (got.ok && got.url) {
        setError("");
        setVerifier(got.verifier);
        openExternal(got.url);
      } else {
        setError(got.detail || "MusicBrainz could not be reached");
      }
    },
    onError: (e) => setError((e as Error).message),
  });

  const finish = useMutation({
    mutationFn: () => mbFinish(code.trim(), verifier),
    onSuccess: (got) => {
      if (got.ok) {
        setCode("");
        setVerifier("");
        setError("");
        qc.invalidateQueries({ queryKey: ["mbAuth"] });
      } else {
        setError(got.detail || "MusicBrainz refused the code");
      }
    },
    onError: (e) => setError((e as Error).message),
  });

  const disconnect = useMutation({
    mutationFn: mbDisconnect,
    onSuccess: () => {
      setResult(null);
      setError("");
      qc.invalidateQueries({ queryKey: ["mbAuth"] });
    },
    onError: (e) => setError((e as Error).message),
  });

  const submit = useMutation({
    mutationFn: () => mbSubmitRatings(paths),
    onSuccess: (got) => {
      setResult(got);
      setError("");
    },
    onError: (e) => {
      setResult(null);
      setError((e as Error).message);
    },
  });

  const busy = connect.isPending || finish.isPending || disconnect.isPending || submit.isPending;
  const expiresAt = auth.data?.expires_at
    ? new Date(auth.data.expires_at * 1000).toLocaleString()
    : "";

  return (
    <div className={`panel text-xs ${className}`}>
      {/* The OAuth2 flow's own how-to, above the Connect button that runs it —
          the same component and words the setup wizard's Connections step
          shows, because this panel IS that step's MusicBrainz half. */}
      <ConnectGuide service="musicbrainz" className="mb-1.5" />
      <div className="flex items-center gap-2 flex-wrap">
        <span className="font-medium text-zinc-300">MusicBrainz</span>
        {auth.isLoading ? (
          <span className="text-zinc-500 inline-flex items-center gap-1.5">
            <Loader2 className="h-3.5 w-3.5 animate-spin" /> checking…
          </span>
        ) : connected ? (
          <>
            <span
              className="inline-flex items-center gap-1.5 text-emerald-300"
              title={
                `${CONFIG_KEYS.token} is stored and renews itself; ` +
                `${CONFIG_KEYS.expiry} = ${expiresAt || "not fetched yet"}`
              }
            >
              <Check className="h-3.5 w-3.5" /> connected as {auth.data?.username || "your account"}
            </span>
            <div className="ml-auto flex gap-1.5">
              <button
                className="btn-primary !py-1 text-xs"
                disabled={busy || !paths.length}
                title={
                  paths.length
                    ? `Submit your ratings for ${paths.length} file${paths.length === 1 ? "" : "s"} of this ${kind === "album" ? "album" : "track"} to MusicBrainz`
                    : kind === "album"
                      ? "This release is not in your library yet, so there are no ratings of yours to submit"
                      : "No file of your library is this recording, so there are no ratings of yours to submit"
                }
                onClick={() => submit.mutate()}
              >
                {submit.isPending ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Send className="h-3.5 w-3.5" />}
                {paths.length ? `Submit ratings (${paths.length})` : "Submit ratings"}
              </button>
              <button
                className="btn-ghost !py-1 text-xs"
                disabled={busy}
                onClick={() => disconnect.mutate()}
                title="Forget the saved MusicBrainz login (the app's own client id/secret stay)"
              >
                <LogOut className="h-3.5 w-3.5" /> Disconnect
              </button>
            </div>
          </>
        ) : (
          <>
            <span className="text-zinc-500">reads need no login — only submitting your ratings does</span>
            <div className="ml-auto flex gap-1.5">
              <button
                className="btn-primary !py-1 text-xs"
                disabled={busy}
                onClick={() => connect.mutate()}
                title={
                  auth.data?.client_id_set
                    ? "Open MusicBrainz to authorize this app, then paste back the code it shows"
                    : "Save the MusicBrainz client id and secret in Settings → Scrobbling & submissions first"
                }
              >
                {connect.isPending ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Unplug className="h-3.5 w-3.5" />}
                Connect
              </button>
            </div>
          </>
        )}
      </div>

      {auth.data && !auth.data.submit_enabled && (
        <div className="mt-1.5 text-amber-300 inline-flex items-center gap-1.5">
          <AlertTriangle className="h-3.5 w-3.5" />
          {CONFIG_KEYS.gate} is off — submissions are refused until it is turned on.
        </div>
      )}

      {!!verifier && (
        <div className="mt-2 flex items-center gap-2 flex-wrap">
          <span className="text-zinc-400">
            Authorize in the page that just opened, then paste the code MusicBrainz shows:
          </span>
          <input
            className="input !py-1 text-xs w-56"
            value={code}
            placeholder="authorization code"
            spellCheck={false}
            onChange={(e) => setCode(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && code.trim() && !busy) finish.mutate();
            }}
          />
          <button
            className="btn-primary !py-1 text-xs"
            disabled={busy || !code.trim()}
            onClick={() => finish.mutate()}
          >
            {finish.isPending ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : null}
            Finish
          </button>
        </div>
      )}

      {error ? (
        <div className="mt-1.5 text-red-300 inline-flex items-start gap-1.5">
          <AlertTriangle className="h-3.5 w-3.5 mt-0.5 shrink-0" />
          <span>{error}</span>
        </div>
      ) : null}

      {result && (
        <div className="mt-2 rounded-lg border border-border overflow-hidden">
          <div className="px-2 py-1 text-[11px] text-zinc-400 border-b border-border bg-raise">
            {result.sent} sent · {result.skipped} skipped
            {result.detail ? ` · ${result.detail}` : ""}
          </div>
          <div className="max-h-64 overflow-auto">
            {result.rows.map((row, i) => (
              <div
                key={`${row.path}-${i}`}
                className="flex items-center gap-2 px-2 py-1 border-t border-border/60 first:border-t-0"
              >
                <span
                  className={row.status === "sent" ? "text-emerald-300" : "text-zinc-500"}
                  title={row.entity ? `${row.entity} ${row.mbid}` : undefined}
                >
                  {row.status === "sent" ? "sent" : "skipped"}
                </span>
                <span className="flex-1 min-w-0 truncate text-zinc-300">{row.title}</span>
                <span className="text-zinc-500 tabular-nums shrink-0">
                  {row.sent !== null
                    ? `${row.sent}${row.value !== row.sent ? ` (from ${row.value})` : ""}`
                    : row.reason}
                </span>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}