import { useState, type DragEvent, type ReactNode } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Check, Loader2, Trash2, X } from "lucide-react";
import { api, type CookieEntry, type RymCookiesSaveReply, type YoutubeCookiesSaveReply } from "../api";
import ConfirmButton from "./ConfirmButton";
import { toast } from "../store";
import { fmtBytes } from "../lib/fmt";
import { useI18n } from "../lib/i18n";

/** The cookie logins, by the id the server knows them by
 *  (`server/api_cookies.py`). There are exactly two, and both are a
 *  cookies.txt: the yt-dlp jar (`youtube`) and the RateYourMusic `Cookie`
 *  header (`rym`). A token or an API key is not one of these — a credential
 *  that is not a cookie has no jar to import. */
export type CookieSource = "youtube" | "rym";

/** A cookie's identity, exactly as the server keys its notes: domain, path and
 *  name. Two cookies can share a name on two domains, and the note has to
 *  follow the one it was written for. */
const identity = (c: { domain: string; path: string; name: string }) =>
  `${c.domain}\t${c.path}\t${c.name}`;

/** One cookie login, in one panel: import a cookies.txt, see what was kept,
 *  write a note against any cookie, clear the credential.
 *
 *  ONE component for both credentials (and for all four places they are
 *  offered — Settings → Sources, the wizard's Keys step, Settings → Videos and
 *  Settings → Discovery), because the two panels used to be a copy each and
 *  the copy is how they would drift apart. What differs between them is the
 *  credential, not the job: where it lands (a jar file the app owns, or the
 *  `rym_cookie` config value), how the file is narrowed (only the hosts that
 *  credential is sent to), and the one sentence that says so.
 *
 *  Every import arrives here as TEXT — a paste or a dropped file, never a
 *  path — and the server validates it as a Netscape cookie file BEFORE it
 *  replaces anything: a bad paste must not cost the user the cookies that were
 *  doing their job. Nothing here ever shows, posts or toasts a cookie VALUE;
 *  the server does not send one.
 *
 *  `compact` is the Sources/Keys row (a disclosure, closed until asked) and
 *  `onStored` hands the fresh config value back to a caller that keeps the
 *  credential in its own form field (the Discovery tab's `rym_cookie` box
 *  would otherwise post the old value over an import on the next Save).
 */
export default function CookieJarPanel({
  source,
  onStored,
  compact = false,
}: {
  source: CookieSource;
  onStored?: (value: string) => void;
  compact?: boolean;
}) {
  const qc = useQueryClient();
  const { t } = useI18n();
  const isYoutube = source === "youtube";
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [dragging, setDragging] = useState(false);
  // Comment fields are edited locally and written on blur, so a keystroke is
  // not a request.
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [saving, setSaving] = useState("");

  // The credential's own state (what the file/config holds) and the per-cookie
  // view are two reads: the first is the jar's summary, the second the list
  // with each cookie's note.
  const yt = useQuery({ queryKey: ["youtubeCookies"], queryFn: api.youtubeCookies, retry: false, enabled: isYoutube });
  const rym = useQuery({ queryKey: ["rymCookies"], queryFn: api.rymCookies, retry: false, enabled: !isYoutube });
  const { data: list } = useQuery({
    queryKey: ["cookieList", source],
    queryFn: () => api.cookieList(source),
    retry: false,
  });
  const present = (isYoutube ? yt.data?.present : rym.data?.present) ?? false;
  const lines = (isYoutube ? yt.data?.lines : rym.data?.lines) ?? 0;
  const maxBytes = (isYoutube ? yt.data?.max_bytes : rym.data?.max_bytes) ?? 524288;
  const warnings = (isYoutube ? yt.data?.warnings : rym.data?.warnings) ?? [];

  const save = async (body: string) => {
    if (!body.trim()) {
      toast.error("Nothing to save — paste the contents of cookies.txt first");
      return;
    }
    setBusy(true);
    let said: string[] = [];
    try {
      if (isYoutube) {
        const r: YoutubeCookiesSaveReply = await api.youtubeCookiesSave(body);
        setText("");
        toast.success(
          `Cookie file saved — ${r.lines} cookie(s)${r.sites.length ? ` for ${r.sites.join(", ")}` : ""}` +
            (r.filtered ? ` · ${r.filtered} cookie line(s) for other sites left out` : "")
        );
        said = r.warnings;
        qc.invalidateQueries({ queryKey: ["youtubeCookies"] });
      } else {
        const r: RymCookiesSaveReply = await api.rymCookiesSave(body);
        setText("");
        if (r.stored > 0) {
          toast.success(
            `Cookie saved — ${r.stored} rateyourmusic.com cookie${r.stored === 1 ? "" : "s"}${r.session ? ", session included" : ""}`
          );
          // The import wrote the config, so the form that owns the credential
          // has to follow it: the box on the Discovery tab (and the next "Save
          // all settings") must agree with what RYM is sent from now on. The
          // value comes from the config, never from the import's answer, which
          // is names and counts only.
          const fresh = await api.config();
          onStored?.(String(fresh.rym_cookie ?? ""));
        } else {
          toast.error("No rateyourmusic.com cookie in that file — nothing was stored");
        }
        said = r.warnings;
        qc.invalidateQueries({ queryKey: ["rymCookies"] });
        qc.invalidateQueries({ queryKey: ["config"] });
      }
      // The server's own sentences about what the credential now holds come
      // through as warnings: a stored cookie with no `session` pair means RYM
      // answers as a guest, and a jar that expired means every download asks
      // the user to sign in — a plain "saved!" would hide exactly that.
      for (const w of said) toast(w);
      qc.invalidateQueries({ queryKey: ["cookieList", source] });
    } catch (e) {
      toast.error(String(e));
    } finally {
      setBusy(false);
    }
  };

  const drop = async (e: DragEvent) => {
    e.preventDefault();
    setDragging(false);
    const file = e.dataTransfer.files?.[0];
    if (!file) return;
    try {
      await save(await file.text());
    } catch (err) {
      toast.error(String(err));
    }
  };

  const remove = async () => {
    setBusy(true);
    try {
      if (isYoutube) {
        await api.youtubeCookiesDelete();
        toast("Cookie file removed — downloads run anonymously again");
        qc.invalidateQueries({ queryKey: ["youtubeCookies"] });
      } else {
        // Clearing goes through the ordinary config route: `rym_cookie` IS the
        // credential, so "remove it" is "save it empty" — no second endpoint
        // and no second place it could live. The notes stay: they are keyed by
        // identity, and the next import of the same session finds them again.
        await api.saveConfig({ rym_cookie: "" });
        onStored?.("");
        toast("Cookie cleared — RYM is asked as a guest again");
        qc.invalidateQueries({ queryKey: ["rymCookies"] });
        qc.invalidateQueries({ queryKey: ["config"] });
      }
      qc.invalidateQueries({ queryKey: ["cookieList", source] });
    } catch (e) {
      toast.error(String(e));
    } finally {
      setBusy(false);
    }
  };

  /** Write one cookie's note — keyed by the cookie's IDENTITY, so it stays on
   *  the right cookie when the same name sits on two domains, and survives a
   *  re-import that re-orders the file. An empty note clears it. */
  const saveComment = async (cookie: CookieEntry, value: string) => {
    if (value === cookie.comment) return;
    const key = identity(cookie);
    setSaving(key);
    try {
      const fresh = await api.cookieComment(source, {
        domain: cookie.domain,
        path: cookie.path,
        name: cookie.name,
        comment: value,
      });
      qc.setQueryData(["cookieList", source], fresh);
      setDrafts((d) => {
        const next = { ...d };
        delete next[key];
        return next;
      });
    } catch (e) {
      toast.error(String(e));
      // Put the field back to what the server holds: a rejected edit must not
      // sit there looking saved.
      setDrafts((d) => ({ ...d, [key]: cookie.comment }));
    } finally {
      setSaving("");
    }
  };

  const title = isYoutube ? "Cookie file" : "Import from cookies.txt";
  const chip = present ? `${lines} cookie${lines === 1 ? "" : "s"}` : "none saved";

  const header = (
    <div className="flex items-center gap-2 flex-wrap">
      <span className="text-xs font-semibold uppercase tracking-wider text-zinc-500">{title}</span>
      <span
        className={`chip ${
          present ? "border-emerald-900/50 bg-emerald-950/30 text-emerald-300" : "border-border bg-zinc-900 text-zinc-500"
        }`}
      >
        {chip}
      </span>
      {isYoutube && present && (
        <span className="text-[10px] text-zinc-500">
          {fmtBytes(yt.data?.bytes ?? 0)}
          {yt.data?.saved_at ? ` · saved ${new Date(yt.data.saved_at).toLocaleString()}` : ""}
        </span>
      )}
      {present && (
        <ConfirmButton
          className="btn-ghost !py-0.5 text-[11px] tap ml-auto"
          confirmLabel={isYoutube ? "Delete the cookie file?" : "Delete the RateYourMusic cookie?"}
          onConfirm={remove}
          disabled={busy}
        >
          <Trash2 className="h-3 w-3" /> Delete
        </ConfirmButton>
      )}
    </div>
  );

  const intro = isYoutube ? (
    <div className="text-[11px] text-zinc-600">
      In the browser you are signed in to YouTube with, export its cookies to a <span className="text-zinc-400">cookies.txt</span>{" "}
      (a "Get cookies.txt" extension writes exactly this file, in Netscape format — the only shape this box accepts), then paste its
      contents below or drop the file onto the box. yt-dlp reads the saved copy for age-gated, members-only and throttled videos — the
      jar belongs to this app, at{" "}
      <span className="text-zinc-400">{yt.data?.path ?? "<music folder>/.mlo/data/cookies.txt"}</span>.
    </div>
  ) : (
    <div className="text-[11px] text-zinc-600">
      Signed in to rateyourmusic.com, export the browser's cookies to a <span className="text-zinc-400">cookies.txt</span> in Netscape
      format — the Firefox extension <span className="text-zinc-400">"cookies.txt"</span> by Rob W writes exactly that file, and it is
      the only shape this box accepts — then paste its contents below or drop the file onto the box. RYM's{" "}
      <span className="text-zinc-400">session</span> cookie is HttpOnly, so this export is the only way to hand it over.
    </div>
  );

  // What the import KEEPS is the one thing a user should be able to see without
  // reading the server's code: the hosts this credential is actually sent to,
  // and nothing else from a browser profile's export.
  const cookies = list?.cookies ?? [];
  const kept = cookies.length > 0 && (
    <div className="space-y-1">
      <div className="text-[10px] text-zinc-600">
        {t("cookies.keptHosts", { hosts: (list?.hosts ?? []).join(", ") })}
      </div>
      {cookies.map((c) => {
        const key = identity(c);
        const draft = drafts[key] ?? c.comment;
        return (
          <div key={key} className="flex items-center gap-2 flex-wrap">
            <span className="chip border-border bg-zinc-900 text-zinc-400">
              {c.domain}
              {c.path !== "/" ? c.path : ""}
            </span>
            <span className="text-[10px] font-mono text-zinc-300">{c.name}</span>
            <span className={`text-[10px] ${c.expired ? "text-amber-400/90" : "text-zinc-600"}`}>
              {c.expires_at || t("cookies.session")}
              {c.expired ? ` · ${t("cookies.expired")}` : ""}
            </span>
            <input
              className="input !py-0.5 text-[10px] flex-1 min-w-[140px] tap"
              value={draft}
              placeholder={t("cookies.commentPlaceholder")}
              aria-label={t("cookies.commentFor", { name: c.name })}
              spellCheck={false}
              disabled={busy || saving === key}
              onChange={(e) => setDrafts((d) => ({ ...d, [key]: e.target.value }))}
              onBlur={(e) => void saveComment(c, e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") (e.target as HTMLInputElement).blur();
              }}
            />
          </div>
        );
      })}
    </div>
  );

  const dropZone = (
    <div
      onDragOver={(e) => {
        e.preventDefault();
        setDragging(true);
      }}
      onDragLeave={() => setDragging(false)}
      onDrop={drop}
      className={`rounded-md border border-dashed p-2 ${dragging ? "border-accent bg-accent/5" : "border-border"}`}
    >
      <textarea
        className="input w-full h-24 font-mono text-[10px] tap"
        placeholder={
          isYoutube
            ? "# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t…\tLOGIN_INFO\t…\n\n(paste here, or drop cookies.txt on this box)"
            : "# Netscape HTTP Cookie File\n.rateyourmusic.com\tTRUE\t/\tTRUE\t…\tsession\t…\n\n(paste here, or drop cookies.txt on this box)"
        }
        value={text}
        onChange={(e) => setText(e.target.value)}
        spellCheck={false}
      />
      <div className="flex items-center gap-2 flex-wrap mt-1.5">
        <button
          className="btn-primary !py-1 text-xs min-h-10 md:min-h-0 tap"
          onClick={() => save(text)}
          disabled={busy || !text.trim()}
        >
          {busy ? <Loader2 className="h-3 w-3 animate-spin" /> : <Check className="h-3 w-3" />}{" "}
          {isYoutube ? "Save cookie file" : "Save cookies"}
        </button>
        {text.trim() && (
          <button className="btn-ghost !py-1 text-xs tap" onClick={() => setText("")} disabled={busy}>
            <X className="h-3 w-3" /> Clear
          </button>
        )}
        <span className="text-[10px] text-zinc-600">
          Up to {fmtBytes(maxBytes)} — {isYoutube
            ? "a jar is a few dozen lines, so anything bigger is a browser profile folder, not a cookies.txt."
            : "the export is the cookies.txt itself; a whole browser profile is bigger than that."}
        </span>
      </div>
    </div>
  );

  const rest: ReactNode = (
    <>
      {intro}
      {kept}
      {warnings.map((w) => (
        <div key={w} className="text-[10px] text-amber-400/90 border border-amber-900/40 bg-amber-950/20 rounded px-2 py-1">
          {w}
        </div>
      ))}
      {dropZone}
    </>
  );

  if (compact) {
    return (
      <details className="rounded border border-border/60 bg-bg/40 px-2 py-1.5">
        <summary className="text-[11px] text-zinc-500 cursor-pointer select-none">
          {title} — {chip}
        </summary>
        <div className="mt-1.5 space-y-2">{rest}</div>
      </details>
    );
  }
  return (
    <div className="pt-2 border-t border-border space-y-2">
      {header}
      {rest}
    </div>
  );
}
