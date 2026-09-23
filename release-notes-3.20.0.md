# la musica 3.20.0 — downloads you can size, a copy you can choose to play, and a search that asks more than one pressing

Everything below came from reports on the running app. The house rules are in
`docs/OPTIMIZATION-GRADING-SPEC.md` (`R171`–`R177` new here).

## A downloaded copy is the file's own codec, unless you say otherwise

"Download" caches a track on the device so it plays with the server down, and it
had no quality setting at all: the bytes were the library file's, and that was
the end of it. It is a setting now, in **Settings → Downloads & playback**
(`spec R171`):

- **`download_codec` ships as `copy`** — the cached bytes ARE the library file's,
  same codec and same bits, nothing re-encoded, so a track is **downloaded in the
  codec it is already in**. The other choices are the app's own codec list (the
  same one `library_codec` takes); they re-encode the track **for that device's
  cache only** — the library file is never touched — which is what a phone with
  no room for a FLAC library wants. `keep` is deliberately absent: `copy` IS
  "never re-encode".
- **`download_bitrate`** is that re-encode's rate, exactly as
  `library_codec_bitrate` is the library pass's: kbps for MP3/AAC/Opus, Vorbis'
  own 0-10 quality scale for Ogg, 0 = the codec's own shipped default (MP3 320,
  AAC 256, Ogg 6, Opus 128), clamped per codec and ignored by a lossless target.
- The rendition is the download's: `/api/stream?download=1` serves it, streaming
  a track always serves the library's own bytes, and the bulk transfer route —
  which frames each file's size up front and so cannot carry a re-encode —
  refuses with **409** naming the key, after which the client's queue fetches one
  track at a time as it already does for anything the batch cannot carry.

Measured on a scratch library with the bundled ffmpeg: `copy` returns the FLAC
byte-for-byte with `audio/flac`; `mp3` at 128 returns a real MP3 whose body
weighs what 128 kbps over that track weighs; `opus` at 32 is less than half of
it; the library file and its range-streamed form are untouched by all of it
(`.pi/smoke_download_quality.py`, `tools/test_download_queue.py`).

## Which copy plays is a preference — and streaming is the default

`playback_source` (**Settings → Downloads & playback**) ships as **`stream`**:
the player asks the server for the library file even when this device has
downloaded it. `downloaded` plays the cached copy instead (`spec R172`). ONE
resolver decides it for every surface — the audible element, its gapless
preload, both lyric previews and the video popout — so the setting cannot be
honoured in one place and ignored in another, and two things outrank it:

- **the server being unreachable** makes the copy the only thing that can play,
  so it is played whatever the setting says (a preference never strands the
  player). Verified with the server stopped: a downloaded track still plays from
  the cache with `playback_source: stream`.
- **a video's live transcode** is a different rendition, so a copy — which holds
  the direct stream's bytes — stands in for it only offline.

A stream that a copy exists for carries `nocache=1`, because the service worker
is cache-first on the element's own URL: without that marker "prefer streaming"
would play the very bytes it was asked to avoid.

## Cached playback works in the shell, not only in the browser

The desktop/mobile shells register no service worker, so a downloaded track there
plays through the `blob:` hand-off — and the cache key it looks the bytes up
under used to carry the **session token** (`?token=…`, because a webview cannot
send the cookie). A token is re-issued at every sign-in, so a downloaded album
read as *undownloaded* with every byte still in the cache, and a re-download
stored a second copy. The key is now token-free (`spec R173`): the token is
authorization, never identity, so a download outlives the session that made it.
The stripping is textual rather than a URL re-serialization — `URLSearchParams`
would rewrite a space as `+` where `streamUrl` wrote `%20`, and the key would
then miss the very request the service worker matches.

Verified in a Chromium webview against a scratch server: with the download in
the cache, `playback_source: downloaded` plays a `blob:` URL (readyState 4,
`currentTime` advancing) — and so does a stream-preferred player once the server
is stopped, because the copy is the only thing left. That `blob:` source is the
hand-off `offlineMediaUrl`/`playbackSource` performs, which is the ONLY path a
shell has (it registers no service worker, so nothing else can hand Cache
Storage's bytes to an element); the artwork warmed beside the audio is keyed the
same way.

## Every add records the walk, and the row says which pressing it is asking

The ranked-edition walk existed (`spec R150`–`R153`) — but only the artist watch
ever recorded the list, so a normal *Add to library* (the MusicBrainz pages, the
queue bar, a deferred add, an artist's prepared rows) searched exactly ONE
edition and, finding nothing usable, ended the release instead of moving on to
the next pressing (`spec R175`). `server.api_add._create_all` now hands the list
`integrations.auto_import_targets` had already resolved straight to
`pending_albums.create(candidates=…)`, which is the store's one writer of it:

- the search walks the group's editions best first, **one 60-second window each**
  (`soulseek_search_timeout_seconds`, 5–300), up to
  `soulseek_fallback_candidates` editions (3 by default, 1 = best edition only);
- two editions stating the same catalog number are **one** search (`R169`);
- a group with **fewer** eligible editions than the cap ends at the end of its own
  list — no error, no empty slot;
- a spent walk is not a give-up: the release keeps its framework album and its
  place, and moves to the queue's **Background** section, re-walked on the
  worker's own ticks until one lands or the user cancels (`R153`);
- it is still **ONE row** for the whole effort, and that row now says which
  edition it is asking with the server's own wording — a `release 2 of 3` badge
  whose tooltip names the edition being asked and the ones that already came back
  empty (`spec R176`). `wishes.candidate_state` is the single source of the
  position, so the row, the album page and the notification cannot disagree.

Covered by `tools/test_add_to_library.py` (the wish carries the ranked list, best
first, with its catalog numbers; the queue row is one row and states the
position) and by the queue page's own render check
(`tools/check_queue_view.mjs`, through `tools/test_queue_view.py`).

## The rip-log bar is 100, and it is the same number everywhere

Audited end to end (`spec R174`): `soulseek_auto_log_min_score` (the acquisition
gate), `grade_log_score_threshold` (the LOG_GRADE check) and
`audit_log_score_threshold` (the AUDIT verdict) all ship **100** — a
Logchecker-perfect log — and a CD candidate's `.log` must be scorable, score 100
and pass its own checksum before its audio is even queued. Nothing about that
changed, and the three paths that do NOT gate are the user's own hands, now
documented rather than implied: a manual entry runs the gate for the logs it
selected (picking a folder with no log is a choice, not a bypass), a CD candidate
with no log on the searched path is re-scored as Digital Media and offered
through the existing `no_logs` confirm, and a folder already on disk is graded
after the fact instead of refused.

## Fix: a lossy conversion never ran

Found while wiring the download rendition: `mlo.containers.CODECS` wrote its CBR
rate as `-b:a 320kbps`, and ffmpeg rejects that suffix outright — *"Invalid chars
'bps' at the end of expression '320kbps'"*. Every CBR target
(MP3/AAC/Opus) failed to encode, both in the library pass and in the new download
rendition. The unit is now ffmpeg's own (`320k`) in the one table that builds the
argv, and both suites that pin the command line were updated with it
(`tools/test_codec_policy.py`, `tools/test_download_queue.py`).

## Fix: an artist page's genres read as words, not as database rows

MusicBrainz publishes genre names lowercase — `alternative rock`, `art pop`,
`britpop` — and the artist page's chips drew them exactly as the database had
them. They now go through `mlo.genres.display_name`, the one capitalization the
tag writers, the grader and the Discover lists already share, so the row reads
**Alternative Rock · Ambient Pop · Art Pop · Art Rock · Britpop · Chamber Pop ·
Crossover Prog · Electronic** (`spec R177`). The release, release-group and
recording pages' chip rows take the same path (they are the same component and
the same data), the artist page's tag chips are capitalized with them so the row
is uniform, and nothing else moves: the genre cascade that feeds the writers
keeps MusicBrainz's spelling — `mlo.genres.normalize_genres` capitalizes once, at
the one place a tag is written — and identity is untouched, because every
comparison downstream folds case.

## Upgrading

Nothing to do: `download_codec`, `download_bitrate` and `playback_source` are new
keys, they ship at the behaviour the app already had (`copy`, `0`, `stream`), and
a config file that has never seen them behaves exactly as before.
