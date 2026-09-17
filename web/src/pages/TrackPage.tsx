import { useEffect, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useParams, Link } from "react-router-dom";
import { Save, Play, Disc3, ListPlus, ListStart, ListMusic, ShieldCheck, ImageUp, Clapperboard, Search, FolderOpen } from "lucide-react";
import { api } from "../api";
import { fmtTech, fmtDuration, isVideoFile } from "../lib/fmt";
import { uncacheTrack } from "../lib/mediaCache";
import { LinkEditorButton, MbIcon, RymIcon } from "../components/Links";
import { SubtitledVideo } from "../components/SubtitledVideo";
import { useStore, toast } from "../store";
import { AuditBadge, GradeBadge, IssueList, EmptyState, PageLoading } from "../components/Badges";
import CoverImg from "../components/CoverImg";
import LyricsViewer from "../components/LyricsViewer";
import LyricsManagerModal from "../components/LyricsManagerModal";
import LyricsEditorModal from "../components/LyricsEditorModal";
import MoreLikeThis from "../components/MoreLikeThis";
import OverflowMenu from "../components/OverflowMenu";

export default function TrackPage() {
  const { path = "" } = useParams();
  const decoded = decodeURIComponent(path);
  const qc = useQueryClient();
  const playNow = useStore((s) => s.playNow);
  const queue = useStore((s) => s.queue);
  const queueAdd = useStore((s) => s.queueAdd);

  const { data, isLoading, error } = useQuery({
    queryKey: ["track-tags", decoded],
    queryFn: () => api.tags(decoded),
  });

  // Full grading/audit context from the album payload (issues, checks,
  // audit verdict, log grade, AccurateRip status, tech). With "mb:<id>"
  // references the album folder comes from the resolved tags payload.
  const albumDir = decoded.startsWith("mb:")
    ? (data?.path ?? "").split("/").slice(0, -1).join("/")
    : decoded.split("/").slice(0, -1).join("/");
  const { data: album, error: albumError } = useQuery({
    queryKey: ["album", albumDir],
    queryFn: () => api.album(albumDir),
    retry: false,
    enabled: !!albumDir,
  });
  const realPath = data?.path ?? decoded;
  const track = (album?.tracks ?? []).find((t) => t.path === decoded || t.path === realPath);

  const [lyrics, setLyrics] = useState("");
  const [dirty, setDirty] = useState(false);
  const [managerOpen, setManagerOpen] = useState(false);
  const [editorOpen, setEditorOpen] = useState(false);
  const [coverBusy, setCoverBusy] = useState(false);
  const [videoOpen, setVideoOpen] = useState(false);
  const coverInput = useRef<HTMLInputElement>(null);

  const tags: Record<string, string> = {};
  for (const [k, v] of Object.entries(data?.tags ?? {})) {
    if (typeof v === "string" && v !== "") tags[k] = v;
  }

  useEffect(() => {
    if (!data) return;
    setLyrics(data.lyrics ?? "");
    setDirty(false);
  }, [data]);

  if (error)
    return (
      <EmptyState
        title="Track not found"
        hint={String(error)}
        action={{ label: "Back to the library", to: "/library" }}
      />
    );
  if (isLoading || !data) return <PageLoading label="Loading track…" />;

  const fileName = realPath.split("/").pop() ?? realPath;
  const isVideo = isVideoFile(realPath);
  const tech = track?.tech ?? data.tech ?? {};
  const issues: string[] = track?.issues ?? [];
  const audit = track?.audit ?? null;
  const logGrade = track?.log_grade ?? null;
  const arStatus = track?.accuraterip_status ?? null;
  const csStatus = track?.checksum_status ?? null;

  // The album payload carries this track's grading (issues, audit, log);
  // until that query resolves AND lists the track the verdict is unknown —
  // an empty issue list there means "not graded", not "clean".
  const graded = !!track;
  const gradeText = !graded
    ? (albumError ? "unavailable" : "—")
    : issues.length
      ? `FAILED (${issues.length} check${issues.length === 1 ? "" : "s"})`
      : "PASS";
  const gradeTone = !graded ? "text-zinc-600" : issues.length ? "text-red-300" : "text-emerald-300";

  const saveLyrics = async () => {
    // Save target chosen in the lyrics editor toolbar (embedded tag / .lrc
    // sidecar / both). Default keeps the historical embed-only behavior.
    const target = localStorage.getItem("mlo.lyricsSaveTarget") ?? "embedded";
    try {
      if (target === "embedded" || target === "both") await api.lyricsEmbed(decoded, lyrics);
      if (target === "sidecar" || target === "both") await api.lyricsWrite(decoded, lyrics);
      toast(target === "sidecar" ? "Lyrics saved to .lrc sidecar" : target === "both" ? "Lyrics saved (tag + .lrc sidecar)" : "Lyrics saved");
      setDirty(false);
      qc.invalidateQueries({ queryKey: ["library"] });
      qc.invalidateQueries({ queryKey: ["track-tags", decoded] });
      qc.invalidateQueries({ queryKey: ["album", albumDir] });
    } catch (e) {
      toast(String(e));
    }
  };

  /** LyricsManager hands lyrics to the editor; the normal Save flow writes
   * them per the chosen save target. */
  const applyFoundLyrics = (lrc: string, source: string) => {
    setLyrics(lrc);
    setDirty(true);
    setManagerOpen(false);
    toast(`Lyrics loaded — ${source}. Review, then Save.`);
  };

  /** The enhanced editor saves through the API itself; refresh afterwards. */
  const refreshAfterEditor = () => {
    qc.invalidateQueries({ queryKey: ["library"] });
    qc.invalidateQueries({ queryKey: ["track-tags", decoded] });
    qc.invalidateQueries({ queryKey: ["album", albumDir] });
  };

  const queueTrack = {
    path: realPath, file: fileName, albumPath: albumDir,
    artist: tags.ALBUMARTIST ?? tags.ARTIST, album: tags.ALBUM, title: tags.TITLE || undefined,
  };
  const enqueue = (position: "next" | "end") => {
    if (!queue.length) {
      playNow([queueTrack]);
      return;
    }
    queueAdd([queueTrack], position);
    toast(position === "next" ? "Playing next" : "Added to the queue");
  };
  const addToPlaylist = async () => {
    const pls = await api.playlists();
    const manual = pls.find((p) => p.kind === "manual");
    if (!manual) {
      const created = await api.createPlaylist("Library selection", "manual");
      await api.playlistAdd(created.id, [decoded]);
    } else {
      await api.playlistAdd(manual.id, [decoded]);
    }
    toast("Added to playlist");
  };
  const openFolder = async () => {
    try {
      await api.openFolder(albumDir);
    } catch (e) {
      toast(String(e));
    }
  };

  const uploadCover = async (file: File) => {
    setCoverBusy(true);
    try {
      await api.cover(albumDir, file, fileName);
      toast(`Per-track cover saved for ${fileName}`);
      qc.invalidateQueries({ queryKey: ["album", albumDir] });
      qc.invalidateQueries({ queryKey: ["library"] });
    } catch (e) {
      toast(String(e));
    } finally {
      setCoverBusy(false);
      if (coverInput.current) coverInput.current.value = "";
    }
  };

  const linkTags: Record<string, { label: string; kind: "mb" | "rym"; url?: (v: string) => string }> = {
    MUSICBRAINZ_ALBUMID: { label: "MusicBrainz Album", kind: "mb", url: (v) => `https://musicbrainz.org/release/${v}` },
    MUSICBRAINZ_TRACKID: { label: "MusicBrainz Track", kind: "mb", url: (v) => `https://musicbrainz.org/recording/${v}` },
    MUSICBRAINZ_ARTISTID: { label: "MusicBrainz Artist", kind: "mb", url: (v) => `https://musicbrainz.org/artist/${v}` },
    MUSICBRAINZ_RELEASEGROUPID: { label: "MusicBrainz Release Group", kind: "mb", url: (v) => `https://musicbrainz.org/release-group/${v}` },
    RATEYOURMUSIC_ALBUM: { label: "RateYourMusic Album", kind: "rym" },
    RATEYOURMUSIC_TRACK: { label: "RateYourMusic Track", kind: "rym" },
    RATEYOURMUSIC_ARTIST: { label: "RateYourMusic Artist", kind: "rym" },
  };

  const mainFields = ["TITLE", "ARTIST", "ALBUM", "GENRE", "DATE", "TRACKNUMBER", "DISCNUMBER",
    "ALBUMARTIST", "ORIGINALDATE", "RELEASETYPE", "RELEASECOUNTRY", "CATALOGNUMBER"];
  const extraFields = Object.keys(tags)
    .filter((k) => !mainFields.includes(k) && k !== "MOOD" && !(k in linkTags) && !["LYRICS", "UNSYNCEDLYRICS"].includes(k))
    .sort();

  return (
    <div className="p-6 space-y-5">
      {videoOpen && isVideo && (
        <div className="fixed inset-0 z-50 bg-black/85 flex items-center justify-center p-6" onClick={() => setVideoOpen(false)}>
          <div className="w-full max-w-4xl" onClick={(e) => e.stopPropagation()}>
            <SubtitledVideo path={decoded} className="w-full max-h-[80vh] rounded-lg border border-border bg-black" />
            <div className="flex justify-between items-center mt-2 text-xs text-zinc-400">
              <span className="truncate">{fileName}</span>
              <button className="btn-ghost !py-1" onClick={() => setVideoOpen(false)}>Close</button>
            </div>
          </div>
        </div>
      )}

      <div className="flex items-center justify-between gap-4">
        <div className="min-w-0">
          <div className="text-xs text-zinc-500">
            <Link to={tags.MUSICBRAINZ_ALBUMID ? `/album/mb:${tags.MUSICBRAINZ_ALBUMID}` : `/album/${encodeURIComponent(albumDir)}`} className="hover:text-accent-soft">
              {tags.ALBUM || albumDir.split("/").pop()}
            </Link>
            {" · "}
            <span className="inline-flex items-center gap-1"><Disc3 className="h-3 w-3" /> {fileName}</span>
          </div>
          <h1 className="text-2xl font-bold tracking-tight truncate">{tags.TITLE ?? fileName}</h1>
          <div className="mt-2 flex flex-wrap items-center gap-2">
            {graded ? (
              <>
                <GradeBadge pass={!issues.length} score={issues.length ? 0 : 100} />
                <AuditBadge audit={audit} />
                <IssueList issues={issues} />
              </>
            ) : (
              <span
                className="text-xs text-zinc-600"
                title={albumError ? String(albumError) : "This track's album payload has not loaded yet"}
              >
                {albumError ? "Grading data unavailable" : "Grading —"}
              </span>
            )}
          </div>
        </div>
        <div className="flex items-center gap-2 shrink-0">
          <button className="btn-ghost" onClick={() => playNow([queueTrack])} title="Play this track">
            <Play className="h-4 w-4 fill-current" /> Play
          </button>
          <LinkEditorButton mode="track" paths={[decoded]} current={tags} />
          <button className="btn-primary" onClick={saveLyrics} disabled={!dirty} title="Save lyrics per the chosen save target">
            <Save className="h-4 w-4" /> Save lyrics
          </button>
          <OverflowMenu
            buttonTitle="All track actions"
            sections={[
              {
                items: [
                  { label: "Find lyrics", icon: Search, onClick: () => setManagerOpen(true) },
                  { label: "Add to playlist", icon: ListPlus, onClick: addToPlaylist },
                  { label: "Play next", icon: ListStart, onClick: () => enqueue("next") },
                  { label: "Add to queue", icon: ListMusic, onClick: () => enqueue("end") },
                ],
              },
              {
                title: "Track",
                items: [
                  { label: "Watch video", icon: Clapperboard, hidden: !isVideo, onClick: () => setVideoOpen(true) },
                  { label: "Open album folder", icon: FolderOpen, onClick: openFolder },
                ],
              },
            ]}
          />
        </div>
      </div>

      <div className="grid grid-cols-1 xl:grid-cols-2 gap-5">
        <div className="space-y-4">
          <div className="bg-card rounded-lg border border-border p-4 space-y-2.5">
            <div className="text-xs font-semibold uppercase tracking-wider text-zinc-500">Metadata (read-only)</div>
            <div className="grid grid-cols-2 gap-2.5">
              {mainFields.map((k) =>
                tags[k] ? (
                  <div key={k} className="min-w-0">
                    <div className="text-[10px] text-zinc-500 uppercase">{k}</div>
                    <div className="text-sm text-zinc-200 truncate" title={tags[k]}>{tags[k]}</div>
                  </div>
                ) : null
              )}
            </div>
            <div className="text-xs font-semibold uppercase tracking-wider text-zinc-500 pt-2">MusicBrainz / RateYourMusic links</div>
            {Object.entries(linkTags).map(([k, spec]) =>
              tags[k] ? (
                <div key={k} className="flex items-center gap-2">
                  <span className="text-[10px] text-zinc-500 uppercase w-40 shrink-0">{spec.label}</span>
                  <span className="text-sm text-zinc-200 truncate flex-1" title={tags[k]}>{tags[k]}</span>
                  {/* the identity-link button lives on its own metadata row:
                      MB rows carry the MusicBrainz mark, RYM rows the RYM mark */}
                  <a
                    href={spec.url ? spec.url(tags[k]) : tags[k]}
                    target="_blank"
                    rel="noreferrer"
                    title={`Open ${spec.label}`}
                    className="p-1.5 rounded-lg hover:bg-raise transition-transform hover:scale-110 inline-flex items-center shrink-0"
                  >
                    {spec.kind === "mb" ? <MbIcon className="h-4 w-4" /> : <RymIcon className="h-4 w-4" />}
                  </a>
                </div>
              ) : null
            )}
            {extraFields.length > 0 && (
              <>
                <div className="text-xs font-semibold uppercase tracking-wider text-zinc-500 pt-2">Other tags</div>
                <div className="grid grid-cols-1 gap-1.5 max-h-56 overflow-auto">
                  {extraFields.map((k) => (
                    <div key={k} className="flex gap-2 items-baseline min-w-0">
                      <span className="text-[10px] text-zinc-500 uppercase w-44 shrink-0 truncate" title={k}>{k}</span>
                      <span className="text-sm text-zinc-200 break-all min-w-0">{tags[k]}</span>
                    </div>
                  ))}
                </div>
              </>
            )}
            {/* MOOD is written by the auto-tagging script (8) — absent until
                that has run, so the empty case says what would fill it */}
            <div className="flex items-center gap-2 pt-2 border-t border-border/60 mt-1">
              <span className="text-[10px] text-zinc-500 uppercase w-44 shrink-0">MOOD</span>
              {tags.MOOD ? (
                <span className="chip bg-zinc-800/70 border border-border text-zinc-300" title="Written by the auto-tagging script">
                  {tags.MOOD}
                </span>
              ) : (
                <span
                  className="text-xs text-zinc-600"
                  title="Auto tagging (script 8) writes a MOOD tag from the audio and its metadata"
                >
                  no mood yet — run auto tagging (script 8)
                </span>
              )}
            </div>
          </div>

          <div className="bg-card rounded-lg border border-border p-4">
            <div className="text-xs font-semibold uppercase tracking-wider text-zinc-500 mb-2">Audio</div>
            <div className="grid grid-cols-2 gap-2 text-sm text-zinc-400">
              <div>Duration <span className="text-zinc-200">{fmtDuration(tech.length)}</span></div>
              <div>Bitrate <span className="text-zinc-200">{fmtTech(tech) || "—"}</span></div>
              <div>Bit depth <span className="text-zinc-200">{tech.bits_per_sample ?? "—"}</span></div>
              <div>Sample rate <span className="text-zinc-200">{tech.sample_rate ? `${(tech.sample_rate / 1000).toFixed(1).replace(/\.0$/, "")} kHz` : "—"}</span></div>
              <div>Channels <span className="text-zinc-200">{tech.channels ?? "—"}</span></div>
              {typeof tech.width === "number" && typeof tech.height === "number" && (
                <div>Video <span className="text-zinc-200">{tech.width}×{tech.height}{tech.codec ? ` · ${tech.codec}` : ""}</span></div>
              )}
            </div>
          </div>

          {isVideo && (
            <VideoTagCard
              path={realPath}
              tags={tags}
              onSaved={() => {
                qc.invalidateQueries({ queryKey: ["track-tags", decoded] });
                qc.invalidateQueries({ queryKey: ["album", albumDir] });
                qc.invalidateQueries({ queryKey: ["library"] });
              }}
            />
          )}

          <div className="bg-card rounded-lg border border-border p-4">
            <div className="flex items-center gap-1.5 text-xs font-semibold uppercase tracking-wider text-zinc-500 mb-2">
              <ShieldCheck className="h-3.5 w-3.5" /> Grading & AUDIT details
            </div>
            <div className="grid grid-cols-2 gap-2 text-sm text-zinc-400">
              <div>Grade <span className={gradeTone}>{gradeText}</span></div>
              <div>AUDIT <span className="text-zinc-200">{audit ?? "not audited"}</span></div>
              <div>Log score <span className="text-zinc-200">{logGrade != null ? `${logGrade}/100` : "—"}</span></div>
              <div>AccurateRip <span className="text-zinc-200">{arStatus ?? "—"}</span></div>
              <div>Checksum <span className="text-zinc-200">{csStatus ?? "—"}</span></div>
              {["AUDIO_MD5", "INTEGRITY", "LOG_CRC", "REPLAYGAIN_TRACK_GAIN", "DYNAMIC RANGE"].map((k) => (
                tags[k] ? (
                  <div key={k}>{k.replace(/_/g, " ")} <span className="text-zinc-200 break-all">{tags[k]}</span></div>
                ) : null
              ))}
            </div>
            {issues.length > 0 && (
              <>
                <div className="text-[10px] text-zinc-500 uppercase tracking-wider mt-3 mb-1">Failed checks</div>
                <ul className="space-y-1">
                  {issues.map((iss, i) => (
                    <li key={i} className="text-xs text-red-300/90 bg-red-950/30 border border-red-900/40 rounded px-2 py-1">{iss}</li>
                  ))}
                </ul>
              </>
            )}
          </div>
        </div>

        <div className="space-y-4">
          <div className="bg-card rounded-lg border border-border p-4 space-y-2">
            <div className="flex items-center gap-3">
              {track?.cover_file && (
                <CoverImg albumPath={albumDir} coverFile={track.cover_file} wrapperClass="h-16 w-16 rounded-lg bg-raise border border-border overflow-hidden shrink-0" />
              )}
              <div className="text-xs text-zinc-500 flex-1">
                {track?.cover_file ? `Per-track cover: ${track.cover_file}` : "No per-track cover — the album cover is used."}
              </div>
            </div>
            <input
              ref={coverInput}
              type="file"
              accept="image/*"
              className="hidden"
              onChange={(e) => e.target.files?.[0] && uploadCover(e.target.files[0])}
            />
            <button
              className="btn-ghost !py-1 text-xs w-full"
              onClick={() => coverInput.current?.click()}
              disabled={coverBusy}
              title={`Upload an image saved next to the track as "${fileName.replace(/\.[^.]+$/, "")}.jpg"`}
            >
              <ImageUp className="h-3.5 w-3.5" /> {coverBusy ? "Uploading…" : "Upload per-track cover"}
            </button>
          </div>
          <LyricsViewer
            path={decoded}
            initialLyrics={lyrics}
            onChange={(v) => { setLyrics(v); setDirty(true); }}
            onSave={saveLyrics}
            artist={tags.ARTIST}
            track={tags.TITLE}
            album={tags.ALBUM}
            duration={tech.length ? Math.round(tech.length) : undefined}
            onEnhancedEditor={() => setEditorOpen(true)}
          />
        </div>
      </div>

      <MoreLikeThis kind="track" artist={tags.ARTIST ?? tags.ALBUMARTIST ?? ""} title={tags.TITLE} album={tags.ALBUM} />

      {managerOpen && (
        <LyricsManagerModal
          path={decoded}
          artist={tags.ARTIST ?? ""}
          track={tags.TITLE ?? ""}
          album={tags.ALBUM || undefined}
          duration={tech.length ? Math.round(tech.length) : undefined}
          currentText={lyrics}
          onApplied={applyFoundLyrics}
          onSaved={refreshAfterEditor}
          onClose={() => setManagerOpen(false)}
        />
      )}

      {editorOpen && (
        <LyricsEditorModal
          path={decoded}
          artist={tags.ARTIST}
          track={tags.TITLE}
          album={tags.ALBUM || undefined}
          duration={tech.length ? Math.round(tech.length) : undefined}
          initialLyrics={lyrics}
          onClose={() => setEditorOpen(false)}
          onSaved={refreshAfterEditor}
        />
      )}
    </div>
  );
}

const VIDEO_TAG_FIELDS = ["TITLE", "ARTIST", "ALBUM", "GENRE", "DATE", "DISCNUMBER", "TRACKNUMBER"];

/** Tag editor for music-video files. Saving writes the tags via ffmpeg —
 * for containers that can't carry them (VOB, MPEG-PS…) the file is remuxed
 * to MKV with every stream stream-copied, so nothing is re-encoded and
 * captions / audio / video quality are untouched. */
function VideoTagCard({
  path,
  tags,
  onSaved,
}: {
  path: string;
  tags: Record<string, string>;
  onSaved: () => void;
}) {
  const [form, setForm] = useState<Record<string, string>>({});
  const [advisory, setAdvisory] = useState("0");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    setForm(Object.fromEntries(VIDEO_TAG_FIELDS.map((k) => [k, tags[k] ?? ""])));
    setAdvisory(tags.ITUNESADVISORY ?? "0");
  }, [path]);

  const set = (k: string, v: string) => setForm((m) => ({ ...m, [k]: v }));

  const save = async () => {
    setBusy(true);
    try {
      const clean: Record<string, string> = {};
      for (const [k, v] of Object.entries(form)) if (v.trim()) clean[k] = v.trim();
      if (advisory.trim()) clean.ITUNESADVISORY = advisory.trim();
      const r = await api.videoTag(path, clean);
      if (r.renamed) {
        // the file moved (remux rename) — the old stream URL's offline cache
        // entry would serve the stale file forever
        await uncacheTrack(path);
      }
      toast(r.renamed
        ? `Tags written — remuxed to MKV: ${String(r.path).split(/[\/]/).pop()}`
        : "Tags written");
      onSaved();
    } catch (e) {
      toast(String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="bg-card rounded-lg border border-border p-4 space-y-2.5">
      <div className="text-xs font-semibold uppercase tracking-wider text-zinc-500 flex items-center gap-1.5">
        <Clapperboard className="h-3.5 w-3.5" /> Tag this music video
      </div>
      <div className="grid grid-cols-2 gap-2.5">
        {VIDEO_TAG_FIELDS.map((k) => (
          <label key={k} className="text-[10px] text-zinc-500 uppercase block">
            {k.replace("TRACKNUMBER", "Track").replace("DISCNUMBER", "Disc")}
            <input
              className="input !py-1 !px-2 text-xs mt-0.5 w-full"
              value={form[k] ?? ""}
              onChange={(e) => set(k, e.target.value)}
            />
          </label>
        ))}
        <label className="text-[10px] text-zinc-500 uppercase block">
          Advisory
          <select
            className="input !py-1 !px-2 text-xs mt-0.5 w-full"
            value={advisory}
            onChange={(e) => setAdvisory(e.target.value)}
          >
            <option value="0">Clean (0)</option>
            <option value="1">Explicit (1)</option>
            <option value="2">Cleaned (2)</option>
          </select>
        </label>
      </div>
      <div className="flex items-center gap-2 flex-wrap">
        <button className="btn-primary !py-1 text-xs" disabled={busy} onClick={save}>
          <Save className="h-3.5 w-3.5" /> Save video tags
        </button>
        <span className="text-[10px] text-zinc-600">
          Video / audio / captions are stream-copied — containers that can't hold tags are remuxed to MKV losslessly.
        </span>
      </div>
    </div>
  );
}
