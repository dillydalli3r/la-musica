import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api";
import { openExternal } from "../lib/externalLinks";
import ConnectGuide from "./ConnectGuide";
import { toast } from "../store";

/** Last.fm scrobbling, on Settings → Scrobbling & submissions.
 *
 *  What the app WRITES back to Last.fm: a now-playing notice when a track
 *  starts, and one scrobble per play under Last.fm's own rule (a track at
 *  least 30 s long, played 50 % of its length or four minutes, whichever comes
 *  first) — anything the service refuses is queued and retried, never dropped.
 *
 *  The panel shows WHO is connected, offers the ONE authorization flow
 *  (`Connect` opens Last.fm's page with the app's own key; the token it hands
 *  back is pasted and exchanged for a session key), the Disconnect control and
 *  the queued count. The on/off switch is NOT here — it is the
 *  `lastfm_scrobble_enabled` field the Scrobbling & submissions tab renders
 *  above this panel (`Scrobble plays to Last.fm`), which is what the line
 *  below says: one control per setting, and this is not a second one.
 */
export default function ScrobblePanel() {
  const qc = useQueryClient();
  // The status is the connection half (who, how many queued) and the config
  // query is the switch half: the same `lastfm_scrobble_enabled` key the group
  // above renders, read here only to say which way it is set.
  const status = useQuery({
    queryKey: ["lastfmStatus"],
    queryFn: api.lastfmStatus,
    retry: false,
    refetchInterval: 30000,
  });
  const { data: cfg } = useQuery({
    queryKey: ["config"],
    queryFn: api.config,
    staleTime: 5 * 60 * 1000,
  });
  const [token, setToken] = useState("");
  const [busy, setBusy] = useState(false);
  const [awaiting, setAwaiting] = useState(false);

  const st = status.data;
  const switchOn = cfg ? cfg.lastfm_scrobble_enabled !== false : true;
  const hasApp = !!st?.key && !!st?.secret;
  const refresh = () => {
    qc.invalidateQueries({ queryKey: ["lastfmStatus"] });
    qc.invalidateQueries({ queryKey: ["config"] });
  };

  const connect = async () => {
    setBusy(true);
    try {
      const r = await api.lastfmConnect();
      if (!r.ok || !r.url) {
        toast.error(r.error || "Last.fm did not hand back an authorization URL");
        return;
      }
      // The user authorizes in their own browser; the token below is what
      // Last.fm's page shows once they have.
      openExternal(r.url);
      setAwaiting(true);
      toast("Authorize the app in the browser that just opened, then paste the token below");
    } catch (e) {
      toast.error(String(e));
    } finally {
      setBusy(false);
    }
  };

  const finish = async () => {
    setBusy(true);
    try {
      const r = await api.lastfmConnectFinish(token.trim());
      if (!r.ok) {
        toast.error(r.error || "Last.fm refused that token");
        return;
      }
      toast.success(`Connected to Last.fm as ${r.username || "your account"}`);
      setToken("");
      setAwaiting(false);
      refresh();
    } catch (e) {
      toast.error(String(e));
    } finally {
      setBusy(false);
    }
  };

  const disconnect = async () => {
    setBusy(true);
    try {
      await api.lastfmDisconnect();
      toast("Disconnected — the session key was forgotten");
      refresh();
    } catch (e) {
      toast.error(String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="pt-2 border-t border-border space-y-2">
      <div className="text-xs font-semibold uppercase tracking-wider text-zinc-500">Last.fm account</div>
      {/* The flow's own how-to, above the button that runs it — the same
          component and the same words the setup wizard's Connections step
          shows, because this panel IS that step's Last.fm half. */}
      <ConnectGuide service="lastfm" />
      {st?.connected ? (
        <div className="text-[12px] text-zinc-300">
          Connected as <span className="text-accent-soft font-medium">{st.username || "your Last.fm account"}</span>
          {typeof st.queued === "number" && st.queued > 0 && (
            <span className="text-amber-400"> · {st.queued} scrobble{st.queued === 1 ? "" : "s"} queued</span>
          )}
          {!switchOn && <span className="text-zinc-500"> · scrobbling is switched off</span>}
        </div>
      ) : (
        <div className="text-[12px] text-zinc-400">
          Not connected — nothing is announced or scrobbled until you authorize this app once.
        </div>
      )}
      {!hasApp && (
        <div className="text-[11px] text-amber-400">
          {!st?.key
            ? "No Last.fm API key saved — paste one on Settings → Sources first."
            : "No Last.fm shared secret saved — it is shown beside the API key on last.fm; scrobbling cannot be signed without it."}
        </div>
      )}
      <div className="flex items-center gap-2 flex-wrap">
        {!st?.connected && (
          <button className="btn-ghost !py-1 text-xs tap" onClick={connect} disabled={busy}>
            {busy ? "Working…" : "Connect to Last.fm"}
          </button>
        )}
        {st?.connected && (
          <button className="btn-ghost !py-1 text-xs tap" onClick={disconnect} disabled={busy}>
            Disconnect
          </button>
        )}
        <span className="text-[10px] text-zinc-600">
          The switch above ({switchOn ? "on" : "off"}) is `lastfm_scrobble_enabled`
          {switchOn
            ? " — plays are announced and scrobbled."
            : " — nothing is sent, and nothing is queued, while it is off."}
        </span>
      </div>
      {(awaiting || (!st?.connected && hasApp && token)) && (
        <div className="space-y-1">
          <div className="text-[11px] text-zinc-400">
            After authorizing on last.fm, paste the token it shows here:
          </div>
          <div className="flex items-center gap-2">
            <input
              className="input !py-1 text-xs w-full tap"
              placeholder="authorization token"
              value={token}
              onChange={(e) => setToken(e.target.value)}
            />
            <button
              className="btn-primary !py-1 text-xs tap"
              onClick={finish}
              disabled={busy || !token.trim()}
            >
              Finish
            </button>
          </div>
        </div>
      )}
      {status.isError && (
        <div className="text-[11px] text-red-300">
          Status unavailable: {String(status.error)}
        </div>
      )}
    </div>
  );
}