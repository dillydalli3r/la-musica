import { useEffect, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Loader2 } from "lucide-react";
import { api } from "../api";
import { openExternal } from "../lib/externalLinks";
import ConnectGuide from "./ConnectGuide";
import { toast } from "../store";

/** Last.fm scrobbling, on Settings → Connections.
 *
 *  What the app WRITES back to Last.fm: a now-playing notice when a track
 *  starts, and one scrobble per play under Last.fm's own rule (a track at
 *  least 30 s long, played 50 % of its length or four minutes, whichever comes
 *  first) — anything the service refuses is queued and retried, never dropped.
 *
 *  The panel shows WHO is connected, offers the ONE authorization flow
 *  (`Connect` opens Last.fm's page with the app's own key and the request token
 *  it just made), the Disconnect control and the queued count. After Connect
 *  the panel POLLS the finish route itself (`wait: true`): a token Last.fm has
 *  not seen approved yet answers `waiting` at HTTP 200, not an error, so the
 *  flow completes the moment the user approves in their browser — no second
 *  button. The manual paste-a-token + Finish pair stays as the fallback for an
 *  older server (which ignores `wait`), and a `waiting` answer is never shown
 *  as a failure on either path. The on/off switch is NOT here — it is the
 *  `lastfm_scrobble_enabled` field the Scrobbling & submissions SECTION renders
 *  above this panel (`Scrobble plays to Last.fm`), which is what the line
 *  below says: one control per setting, and this is not a second one.
 */

/** How often, and for how long, the Connect flow re-asks Last.fm whether the
 *  request has been approved yet. 2.5 s feels immediate once the user lands
 *  back here, and the two-minute ceiling stops a forgotten tab polling
 *  forever — past it the user is told to press Finish by hand. */
const POLL_MS = 2500;
const POLL_LIMIT_MS = 120000;

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
  // True while the panel is polling the finish route on the user's behalf;
  // `waitNote` is Last.fm's own sentence ("waiting for approval") shown beside
  // the spinner.
  const [polling, setPolling] = useState(false);
  const [waitNote, setWaitNote] = useState("");

  const st = status.data;
  const switchOn = cfg ? cfg.lastfm_scrobble_enabled !== false : true;
  const hasApp = !!st?.key && !!st?.secret;
  const refresh = () => {
    qc.invalidateQueries({ queryKey: ["lastfmStatus"] });
    qc.invalidateQueries({ queryKey: ["config"] });
  };

  // The poll itself. It runs while `polling` is set and a token is in hand,
  // and stops on the FIRST of: an `ok` answer, a definitive non-waiting
  // refusal (an older server, which then leaves the manual Finish below), the
  // two-minute ceiling, a Cancel, or the account already reading connected.
  useEffect(() => {
    if (!polling || !token.trim() || st?.connected) return;
    let alive = true;
    // `window.setTimeout` is the browser's own: a number, per the DOM typings
    // this build compiles against.
    let timer: number | undefined;
    const started = Date.now();
    const tick = async () => {
      if (!alive) return;
      if (Date.now() - started > POLL_LIMIT_MS) {
        alive = false;
        setPolling(false);
        setWaitNote("");
        toast.error("Last.fm did not see the approval in time — approve the app, then press Finish");
        return;
      }
      try {
        const r = await api.lastfmConnectFinish(token.trim(), true);
        if (!alive) return;
        if (r.ok) {
          alive = false;
          setPolling(false);
          setWaitNote("");
          setToken("");
          toast.success(`Connected to Last.fm as ${r.username || "your account"}`);
          refresh();
          return;
        }
        if (r.waiting) {
          // Not an error: Last.fm has not seen the approval yet. Keep waiting
          // and re-arm the poll with the server's own sentence on screen.
          setWaitNote(r.reason || "Waiting for you to approve in the browser…");
          timer = window.setTimeout(tick, POLL_MS);
          return;
        }
        // Not waiting and not ok: either a real refusal or an older server
        // that ignores `wait`. Stop and hand the user the manual Finish.
        alive = false;
        setPolling(false);
        setWaitNote("");
        toast.error(r.error || r.reason || "Last.fm has not approved that token yet — press Finish once it has");
      } catch {
        // A transport hiccup is not an answer: keep the poll alive and retry.
        if (!alive) return;
        setWaitNote("Waiting for Last.fm…");
        timer = window.setTimeout(tick, POLL_MS);
      }
    };
    void tick();
    return () => {
      alive = false;
      window.clearTimeout(timer);
    };
  }, [polling, token, st?.connected]);

  const connect = async () => {
    setBusy(true);
    try {
      const r = await api.lastfmConnect();
      if (!r.ok || !r.url) {
        toast.error(r.error || "Last.fm did not hand back an authorization URL");
        return;
      }
      // The app made the request token itself, so the panel can poll with it
      // straight away; the user only approves in their own browser.
      openExternal(r.url);
      if (r.token) {
        setToken(r.token);
        setWaitNote("Waiting for you to approve in the browser…");
        setPolling(true);
      }
      toast("Authorize the app in the browser that just opened — this panel connects itself once you do");
    } catch (e) {
      toast.error(String(e));
    } finally {
      setBusy(false);
    }
  };

  // The manual fallback: what the poll does for a current server, by hand for
  // an older one. A `waiting` answer is not a refusal, so say so rather than
  // pretending it failed.
  const finish = async () => {
    setBusy(true);
    try {
      const r = await api.lastfmConnectFinish(token.trim());
      if (!r.ok) {
        if (r.waiting) {
          setWaitNote(r.reason || "Waiting for you to approve in the browser…");
          toast("Last.fm has not seen the approval yet — approve in the browser, then press Finish again");
          return;
        }
        toast.error(r.error || r.reason || "Last.fm refused that token");
        return;
      }
      toast.success(`Connected to Last.fm as ${r.username || "your account"}`);
      setToken("");
      setPolling(false);
      setWaitNote("");
      refresh();
    } catch (e) {
      toast.error(String(e));
    } finally {
      setBusy(false);
    }
  };

  const stopPolling = () => {
    setPolling(false);
    setWaitNote("");
  };

  const disconnect = async () => {
    setBusy(true);
    setPolling(false);
    setWaitNote("");
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
        <div className="flex items-center gap-2 flex-wrap">
          <span className="chip border bg-emerald-900/50 text-emerald-300 border-emerald-800">
            Connected as {st.username || "your Last.fm account"}
          </span>
          {typeof st.queued === "number" && st.queued > 0 && (
            <span className="chip border bg-amber-950/30 border-amber-900/60 text-amber-300/90">
              {st.queued} scrobble{st.queued === 1 ? "" : "s"} queued
            </span>
          )}
          {!switchOn && (
            <span className="chip border bg-raise border-border text-zinc-500">scrobbling is switched off</span>
          )}
        </div>
      ) : (
        <div className="text-[12px] text-zinc-400">
          Not connected — nothing is announced or scrobbled until you authorize this app once.
        </div>
      )}
      {!hasApp && (
        <div className="text-[11px] text-amber-400">
          {!st?.key
            ? "No Last.fm API key saved — paste one on Settings → Connections first."
            : "No Last.fm shared secret saved — it is shown beside the API key on last.fm; scrobbling cannot be signed without it."}
        </div>
      )}
      <div className="flex items-center gap-2 flex-wrap">
        {!st?.connected && (
          <button className="btn-ghost !py-1 text-xs tap" onClick={connect} disabled={busy || polling}>
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

      {/* The poll's own line: a spinner and Last.fm's sentence, with the one
          control that ends the wait without touching the token. */}
      {polling && !st?.connected && (
        <div className="flex items-center gap-2 flex-wrap text-[11px]">
          <span className="text-sky-300 inline-flex items-center gap-1.5">
            <Loader2 className="h-3.5 w-3.5 animate-spin" />
            {waitNote || "Waiting for you to approve in the browser…"}
          </span>
          <button className="btn-ghost !py-1 text-[11px] tap" onClick={stopPolling}>
            Stop waiting
          </button>
        </div>
      )}

      {/* The manual fallback (an older server, or a token pasted by hand).
          Hidden while the poll is doing the same job. */}
      {!st?.connected && !polling && hasApp && (
        <div className="space-y-1">
          <div className="text-[11px] text-zinc-400">
            After authorizing on last.fm, paste the token it shows here — or let the poll above do it once you press Connect.
          </div>
          {waitNote && <div className="text-[11px] text-sky-300">{waitNote}</div>}
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