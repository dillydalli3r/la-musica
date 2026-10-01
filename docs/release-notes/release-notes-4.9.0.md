# la musica 4.9.0 — public ratings, whole-CD rips that split themselves, and scripts that got faster

## Soulseek is gone

The managed slskd client, its search, the transfer and download queue, sharing,
the port check, wishes, watched artists and the whole auto-import watcher are
**removed** — not hidden, deleted: eleven server modules, the Soulseek and
Watched-artists pages, the `soulseek_*` / `wishes_*` / `artist_watch_*` config
keys, the bundled slskd binary, the container's listen-port publish and the
share-reachability workflow. Forty-odd routes under `/api/soulseek`,
`/api/wishes`, `/api/watches` and `/api/queue` now answer **404**.

Importing is unaffected and now the only way in: archives, folders and uploads
still run the same eight-step wizard and the same post-import chain, and
`POST /api/library/add` still creates the framework album so a release you add
before its audio exists has somewhere to land. What went with the feature is the
thing that filled such a folder on its own. A file that already carries
`SOURCE=Soulseek` still renders that value — the tag is data, not a feature.

## Public ratings, from the sources you choose (script 24)

The app now asks the world what it thinks. **Script 24 — Web ratings** fills an
ALBUM score and a TRACK score and writes them beside your own stars:

| tag | what it holds |
|---|---|
| `WEBRATING` / `WEBRATING_SOURCE` | the track's aggregated score (0–100) and who answered |
| `ALBUMWEBRATING` / `ALBUMWEBRATING_SOURCE` | the album's, written to every track of the folder |

They sit on the same 0–100 Picard scale as your own `RATING`, which is never
touched — the star field draws **your** rating in the accent and a web value only
in its own dimmer tone, only while you have rated nothing there, with the
sources in the tooltip. Album and track are separate facts: a track whose
recording has no rating keeps none even when its album has one, and neither is
invented from the other. A source's own vote count weights the average, so a
score from 49,000 ratings outvotes sixteen.

**MusicBrainz alone ships**, and it is the only source that needs no credential:
the **release group's** rating for the album, the recording's for each track with
the **work's** as a fallback. Measured on one album: 1.2 s and both values
written (`ALBUMWEBRATING 91`, tracks `77` / `87`). RateYourMusic, Album of the
Year and Discogs are one tick away in Settings → Discovery; all three are
archive-backed (RYM's and AOTY's live pages are behind Cloudflare — AOTY refuses
every automated client we could throw at it, including headed Chromium), so the
same album costs ~23 s with them on, which is why they are opt-in rather than
shipped. Nothing is written when no source answers.

MusicBrainz genres were re-tiered at the same time: an album's genres come from
the **release group** (the album is the group, not the pressing) and a track's
from its recording with the **work's** merged behind — the work is the "release
group" a track does not have. The recording is never dropped in the work's
favour: works carry no genres at all for a mainstream album, and a
work-first reading would have lost data the app already had. Album of the Year
is available as a genre source too (`albumoftheyear` in `genre_sources`).

## A whole-CD image rip splits itself on the way in

A rip that arrives as **one `.flac` for the whole disc plus its `.cue`** — the
shape most CD rips have — used to be imported whole, so the naming script, the
per-track tags, the grader's per-track checks and AccurateRip all saw one file
where they expected a tracklist. The import now cuts it at the cue's own
`INDEX 01` points, the convention every CD splitter uses: track N runs to track
N+1's `INDEX 01`, so a pregap belongs to the track in front of it, and track 1
starts at 0 so a hidden track stays with it.

The cut is a decode/re-encode through ffmpeg, never a stream copy — a copy cuts
at a frame boundary and can shift a track by up to one frame (~93 ms at
44.1 kHz) — so **every track's PCM is bit-identical** to the image's own samples
at those offsets (pinned in `tools/test_cue_rename.py`). It is all-or-nothing per
image: a failed track removes the tracks already written and leaves the image
alone, and on success the image goes to the Trash rather than being deleted. The
`.cue` stays exactly as written.

## Edit lyrics where you read them, without losing your place

The docked lyrics sidebar and the fullscreen player both carry an editor now,
using the same editor the track page already mounts — so the words you are
reading and the words you are editing cannot drift. Save writes through the same
tag/`.lrc` path and the same save-target setting.

While an editor is open, **playback is held on that track**: if the song ends
mid-edit the player pauses instead of advancing (the gapless handover included),
because a track change would swap the file out from under you. Next/Previous
still work if you ask for them.

## The scripts got faster, and none of them changed what they produce

Every one of the 24 scripts was audited for wasted work. What was measured:

- **Audit library** — the AudioAuditor spectral pass ran one CLI batch at a time
  on the runner thread. Batches are independent processes, so they run in lanes
  now: a 130-file album's CLI work went **3.1 s serial → 2.0 s wall**, and the
  whole run **16.3 s → 4.7 s**, with an identical AUDIT histogram and identical
  stats.
- **Format lyrics** — every changed track was parsed by mutagen **twice** (once
  to read, once to re-read after the write) and the same lyric text was
  re-normalised up to five times per track. A 40-track album: **80 container
  parses → 40**, **200 formatter calls → 100**, median **4.02 s → 3.26 s**, with
  every output file's sha256 identical across 13 configurations.
- **Fetch lyrics** — the same double parse (40 tracks: 2.00 opens/track →
  1.00).
- **Grade** — the naming script was evaluated twice per track, tag keys were
  re-normalised per tag per check (25,125 regex calls per 120-file pass), alias
  families were re-classified per tag (18,720 calls per 480-track pass), each
  `.lrc` sidecar was read up to three times and each rip log decoded twice. All
  of it is computed once now — **every verdict byte-identical**, including the
  counters and the sentence in each issue.
- **Optimize FLACs** — the tool folder was re-detected once per converted file
  (37 lookups for a 36-file run → 1).
- **Process images** — a cover resize helper decoded the whole image for files
  whose own skip probe had already said it could do nothing (a fresh 84-image
  run: 84 calls → 48, i.e. 36 full decodes removed).
- **Key & BPM** — the tag writes ran on the runner thread while every decoder
  lane sat idle; they happen on the file's own lane now, and the bundled-librosa
  probe is answered once per process instead of once per track. The whole-track
  decode was **deliberately kept** — capping it would change the BPM/KEY written
  for a mixed-tempo track, which is a regression, not an optimization.
- **AccurateRip** — the MEDIA=CD filter opened one container per file, serially,
  album after album; it runs in lanes now.

**Writing MusicBrainz metadata during an import runs its files in lanes.** Each
file costs a full container rewrite (the app copies it, then mutagen rewrites
the copy — two passes over every byte), and a 28-track write ran 28 of those end
to end. `POST /api/mb/assign` now writes across up to 8 lanes; measured on 6
tracks, **99 ms → 42 ms**, with the reply byte-identical (error order included).

## Fixes

- **RateYourMusic refuses with a reason you can act on.** A 403 was never
  checked for Cloudflare's interstitial — only 200s were — so every blocked
  request was reported as "refused without a Cloudflare challenge" and the 403
  branch never used the detector at all. Caught live: a 27-pair signed-in cookie
  and a 403 whose body was `<title>Just a moment...</title>`. Cloudflare binds
  its `cf_clearance` cookie to the **exact User-Agent and network** that earned
  it, so there is a new `rym_user_agent` setting, and a stored cookie with no
  `cf_clearance` now says so by name instead of leaving you guessing.
- **Grade findings reach the notification tray from any page**, whether or not
  you ever open Home or the Library, and the strip says **how much** is wrong —
  the failing check count for the album or track, with the names one hover away
  — instead of printing twenty check names and burying the album in them.
- **A run scoped to one album is named by the album**, not by its folder. The
  progress row used to read `[Album; Compilation] 197…` — a naming-script path
  truncated to the point of naming nothing.
- **The advisory step's auto-import no longer says "Re-rating".** It writes what
  the sources state for every track, and most of an album being imported carries
  nothing yet — the label reads "Importing advisory for 28 track(s)…", and the
  per-track outcome rows fold away on a big album with a Show/Hide press.
