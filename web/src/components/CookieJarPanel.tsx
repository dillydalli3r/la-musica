import { useState, type DragEvent } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Check, Loader2, Trash2, X } from "lucide-react";
import { api, type CookieEntry, type YoutubeCookiesSaveReply } from "../api";
import ConfirmButton from "./ConfirmButton";
import { toast } from "../store";
import { fmtBytes } from "../lib/fmt";
import { useI18n } from "../lib/i18n";

/** The cookie login the app keeps: the yt-dlp jar (`youtube`). A token or an
 *  API key is not one of these — a credential that is not a cookie has no jar
 *  to import. */
export type CookieSource = "youtube";

/** A cookie's identity, exactly as the server keys its notes: domain, path and
 *  name. Two cookies can share a name on two domains, and the note has to
 *  follow the one it was written for. */
const identity = (c: { domain: string; path: string; name: string }) =>
  `${c.domain}\t${c.path}\t${c.name}`;

/** The YouTube cookie jar, in one panel: import a cookies.txt, see what was
 *  kept, write a note against any cookie, clear the jar.
 *
 *  ONE component for every place the jar is offered — Settings → Videos and
 *  the wizard's Connections step.
 *
 *  Every import arrives here as TEXT — a paste or a dropped file, never a
 *  path — and the server validates it as a Netscape cookie file BEFORE it
 *  replaces anything: a bad paste must not cost the user the cookies that were
 *  doing their job. Nothing here ever shows, posts or toasts a cookie VALUE;
 *  the server does not send one.
 *
 *  `compact` is the Sources/Keys row (a disclosure, closed until asked). */
export default function CookieJarPanel({
  source,
  compact = false,
}: {
  source: CookieSource;
  compact?: boolean;
}) {
  const qc = useQueryClient();
  const { t } = useI18n();
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [dragging, setDragging] = useState(false);
  // Comment fields are edited locally and written on blur, so a keystroke is
  // not a request.
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [saving, setSaving] = useState("");

  // The jar's own state (what the file holds) and the per-cookie view are two
  // reads: the first is the jar's summary, the second the list with each
  // cookie's note.
  const yt = useQuery({ queryKey: ["youtubeCookies"], queryFn: api.youtubeCookies, retry: false });
  const { data: list } = useQuery({
    queryKey: ["cookieList", source],
    queryFn: () => api.cookieList(source),
    retry: false,
  });
  const present = yt.data?.present ?? false;
  const lines = yt.data?.lines ?? 0;
  const maxBytes = yt.data?.max_bytes ?? 524288;
  const warnings = yt.data?.warnings ?? [];

  const save = async (body: string) => {
    if (!body.trim()) {
      toast.error("Nothing to save — paste the contents of cookies.txt first");
      return;
    }
    setBusy(true);
    try {
      const r: YoutubeCookiesSaveReply = await api.youtubeCookiesSave(body);
      setText("");
      toast.success(
        `Cookie file saved — ${r.lines} cookie(s)${r.sites.length ? ` for ${r.sites.join(", ")}` : ""}` +
          (r.filtered ? ` · ${r.filtered} cookie line(s) for other sites left out` : "")
      );
      // The server's own sentences about what the jar now holds — a jar that
      // expired means every download asks the user to sign in, and a plain
      // "saved!" would hide exactly that.
      for (const w of r.warnings) toast(w);
      qc.invalidateQueries({ queryKey: ["youtubeCookies"] });
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
      await api.youtubeCookiesDelete();
      toast("Cookie file removed — downloads run anonymously again");
      qc.invalidateQueries({ queryKey: ["youtubeCookies"] });
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

  const title = "Cookie file";
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
      {present && (
        <span className="text-[10px] text-zinc-500">
          {fmtBytes(yt.data?.bytes ?? 0)}
          {yt.data?.saved_at ? ` · saved ${new Date(yt.data.saved_at).toLocaleString()}` : ""}
        </span>
      )}
      {present && (
        <ConfirmButton
          className="btn-ghost !py-0.5 text-[11px] tap ml-auto"
          confirmLabel="Delete the cookie file?"
          onConfirm={remove}
          disabled={busy}
        >
          <Trash2 className="h-3 w-3" /> Delete
        </ConfirmButton>
      )}
    </div>
  );

  const intro = (
    <div className="text-[11px] text-zinc-600">
      In the browser you are signed in to YouTube with, export its cookies to a <span className="text-zinc-400">cookies.txt</span>{" "}
      (a "Get cookies.txt" extension writes exactly this file, in Netscape format — the only shape this box accepts), then paste its
      contents below or drop the file onto the box. yt-dlp reads the saved copy for age-gated, members-only and throttled videos — the
      jar belongs to this app, at{" "}
      <span className="text-zinc-400">{yt.data?.path ?? "<music folder>/.mlo/data/cookies.txt"}</span>.
    </div>
  );

  // What the import KEEPS is the one thing a user should be able to see without
  // reading the server's code: the hosts the jar is actually sent to, and
  // nothing else from a browser profile's export.
  const cookies = list?.cookies ?? [];
  // ONE SECTION PER IMPORT. Every row the same cookies.txt produced carries
  // the same `imported_at`, so an import reads as the event it was — the
  // header "Imported 09/10/2026, 23:35 · 27 cookies" — instead of 27 loose
  // rows that say nothing about when they arrived. The section is closed
  // until asked, so a fresh import is one line, not a wall; the per-cookie
  // notes are still there inside it. Newest import first; a jar that
  // predates the stamp (so no row carries one) keeps its older grouping.
  const groups: { at: string; rows: CookieEntry[] }[] = [];
  for (const c of cookies) {
    const at = c.imported_at || "";
    const last = groups[groups.length - 1];
    if (last && last.at === at) last.rows.push(c);
    else groups.push({ at, rows: [c] });
  }
  groups.sort((a, b) => (a.at === b.at ? 0 : a.at < b.at ? 1 : -1));
  const kept = cookies.length > 0 && (
    <div className="space-y-1.5">
      <div className="text-[10px] text-zinc-600">
        {t("cookies.keptHosts", { hosts: (list?.hosts ?? []).join(", ") })}
      </div>
      {groups.map((g) => (
        <details
          key={g.at || "undated"}
          className="rounded border border-border/60 bg-zinc-950/30 px-2 py-1"
        >
          <summary className="text-[10px] uppercase tracking-wider text-zinc-500 cursor-pointer select-none">
            {g.at
              ? t("cookies.importedAt", {
                  when: new Date(g.at).toLocaleString(),
                  count: g.rows.length,
                })
              : t("cookies.keptCount", { count: g.rows.length })}
          </summary>
          <div className="space-y-1 mt-1.5">
            {g.rows.map((c) => {
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
        </details>
      ))}
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
        placeholder={`# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t…\tLOGIN_INFO\t…\n\n(paste here, or drop cookies.txt on this box)`}
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
          Save cookie file
        </button>
        {text.trim() && (
          <button className="btn-ghost !py-1 text-xs tap" onClick={() => setText("")} disabled={busy}>
            <X className="h-3 w-3" /> Clear
          </button>
        )}
        <span className="text-[10px] text-zinc-600">
          Up to {fmtBytes(maxBytes)} — a jar is a few dozen lines, so anything bigger is a browser profile folder, not a cookies.txt.
        </span>
      </div>
    </div>
  );

  const rest = (
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