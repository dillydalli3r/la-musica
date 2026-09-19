import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { Sparkles } from "lucide-react";
import { getToken, serverUrl } from "../api";
import { albumRef, trackRef } from "../lib/refs";
import CoverImg from "./CoverImg";

/** One row of GET /api/recommend (server/recommend.py). `kind` is what the row
 *  IS, not what was asked for — an artist page is served albums. */
export interface RecommendItem {
  kind: "album" | "track";
  id: string;
  path: string;
  mbid: string | null;
  title: string;
  subtitle: string;
  score: number;
  reasons: string[];
  /** Album folder the cover belongs to, plus the cover file inside it. */
  cover_path: string;
  cover: string | null;
}

/** The shelf's own fetch. Every other call goes through `api.ts`, which is
 *  owned by another writer this wave — this asks the route directly with the
 *  same credentials and token `api.ts` would have sent. */
async function fetchRecommend(kind: string, id: string, limit: number) {
  const token = getToken();
  const url = `${serverUrl()}/api/recommend?kind=${encodeURIComponent(kind)}`
    + `&id=${encodeURIComponent(id)}&limit=${limit}`;
  const r = await fetch(url, {
    credentials: "include",
    headers: token ? { Authorization: `Bearer ${token}` } : undefined,
  });
  if (!r.ok) throw new Error(`recommend failed: ${r.status}`);
  return (await r.json()) as { items: RecommendItem[] };
}

/** In-app route for one row: the MBID form when the item is tagged, the path
 *  form otherwise — the same rule every other link in the app follows. */
function refFor(item: RecommendItem): string {
  return item.kind === "track"
    ? trackRef({ path: item.path, tags: { MUSICBRAINZ_TRACKID: item.mbid ?? undefined } })
    : albumRef({ path: item.path, meta: { MUSICBRAINZ_ALBUMID: item.mbid } });
}

/** "More like this": a horizontal shelf of the library items the local scorer
 *  ranks closest to this page's entity, each captioned with its own strongest
 *  reason (hover for all of them).
 *
 *  A scroller rather than a grid — the shelf is a handful of covers on a phone
 *  and the same handful on a desktop, which is the one shape that needs no
 *  breakpoint. Renders nothing when there is nothing to suggest (an untagged
 *  or empty library, an id the library no longer holds), so a page never shows
 *  an empty box. */
export default function MoreLikeThis({ kind, id, limit = 12, title = "More like this" }: {
  kind: "artist" | "album" | "track" | "playlist";
  /** Library path or `mb:<uuid>` — the server resolves either. */
  id: string;
  limit?: number;
  title?: string;
}) {
  const { data } = useQuery({
    queryKey: ["recommend", kind, id, limit],
    queryFn: () => fetchRecommend(kind, id, limit),
    // Scored locally but not for free, and the answer only changes when the
    // library does — a page being read must not re-ask on every focus.
    staleTime: 5 * 60_000,
    retry: false,
  });
  const items = data?.items ?? [];
  if (!id || items.length === 0) return null;
  return (
    <section className="section">
      <div className="flex items-center gap-1.5 mb-2">
        <Sparkles className="h-3.5 w-3.5 text-zinc-500" />
        <h2 className="text-xs font-semibold uppercase tracking-wider text-zinc-500">{title}</h2>
      </div>
      <div className="flex gap-3 overflow-x-auto pb-1 -mx-1 px-1">
        {items.map((item) => (
          <Link
            key={item.id}
            to={refFor(item)}
            className="group w-28 shrink-0 sm:w-32"
            title={item.reasons.join(" · ")}
          >
            <CoverImg
              albumPath={item.cover_path || item.path}
              coverFile={item.cover}
              wrapperClass="aspect-square w-full rounded-xl bg-raise border border-border overflow-hidden shadow-lg ring-1 ring-black/40"
            />
            <div className="mt-1.5 px-0.5">
              <div className="text-sm font-medium truncate group-hover:text-accent-soft">{item.title}</div>
              <div className="text-[11px] text-zinc-500 truncate">{item.subtitle}</div>
              <div className="text-[10px] text-zinc-600 truncate">{item.reasons[0] ?? ""}</div>
            </div>
          </Link>
        ))}
      </div>
    </section>
  );
}
