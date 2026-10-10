# la musica 1.2.0 — three sources, and the pictures are actually looked at

Genre and rating data now comes from exactly three providers, read live and in
one order: **RateYourMusic → Album of the Year → MusicBrainz**, with the AI as a
last resort that may only answer with published genre names. Around that, this
release fixes the things that made the data *look* wrong on screen: covers that
were picked without ever being compared, track thumbnails that kept the previous
image, cookie imports rendered as a wall of rows, and app-written text files
that were not the shape this app writes.

## Genres and ratings: three sources, per track

- **The chains ask RYM, then AOTY, then MusicBrainz — and nothing else.** The
  eight other genre handlers are deleted, not unshipped. `genre_sources` and
  `web_ratings_sources` ship the same three ids in the same order, so the two
  chains cannot disagree about who is asked first, and `normalize_config`
  migrates an older install's list (and drops any id this build no longer knows).
- **Per track, with the album behind it.** RYM answers from its song pages,
  AOTY from each track's own score row, MusicBrainz from the recording (work as
  the fallback); a source with no track answer falls back to its ALBUM answer
  and the result records which level it came from. Album of the Year publishes
  no per-track *genre*, so its genres are album-level with the artist page behind
  them — stated rather than faked.
- **The AI is a real last resort, and it may only use published names.** It is
  asked when the three sources cannot fill a track's slots, its prompt names the
  taxonomy (RateYourMusic/MusicBrainz), lists the app's own families and asks
  for the track's genres specifically; an answer that is not a published genre
  (or one a source stated) is dropped, so a plausible invention never reaches a
  file. Measured: RYM's own genre names canonicalize into the app's vocabulary
  (48 of 49 sampled), so "only RYM-supported genres" is enforced by the gate,
  not by hope.
- **Live, never from a snapshot.** The Internet Archive is out of both chains
  (`rym_archive_fallback` now scopes to the Discover charts alone). Both scraped
  sources sit behind Cloudflare, so the app needs a cleared cookie or a
  FlareSolverr-compatible solver (`cf_solver_url`); a refusal is reported in the
  source's own words and a plain client, curl_cffi, headless Chromium and a
  headed Chrome were all measured getting the interstitial — which is why the
  cookie import is the documented path.
- **A cookies.txt import for both scraped sources.** Settings → Sources has a
  panel per credential (RateYourMusic *and* Album of the Year, plus yt-dlp), the
  setup wizard's **Keys & cookies** step renders all three, and one import is now
  **one dated section** ("Imported 09/10/2026, 23:35 · 27 cookies") with the
  per-cookie notes inside it instead of 27 loose rows.
- **ITUNESADVISORY was audited live** (clean tracks → 0, explicit → 1, no false
  positives) and hardened anyway: an identity route (the track's own ISRC)
  stating *clean* now beats a `1` from a name-guess route, which is the
  Radiohead "Just" shape.

## Covers: the album's own art is measured, and compared

- **The identity rows are always probed.** The Cover Art Archive's reads are
  appended after the name search, so the old `rows[:COVER_PROBE_LIMIT]` slice
  left the release group's front cover **unmeasured** — and an unmeasured row is
  rejected while a cover minimum is set. Measured live on Plastic Beach (52
  rows): the album's own MusicBrainz art was rejected unmeasured and a store's
  re-issue sleeve was picked. Both probes now name the first 24 rows **plus
  every row carrying the album's own art**.
- **Every candidate is compared with it, pixel to pixel.** The probe that
  measures cover-likeness now also stores each image's 64-bit dHash, and a new
  **reference tier — the biggest single weight (0.18)** — scores a row by how
  many bits it shares with the album's own MusicBrainz front. The same live case
  now picks the release group's front (0.9364) over the store row (0.8076) that
  the comparison shows is a *different picture* (59 % match, while Deezer's
  re-encode of the right art matches at 97 %). With no reference among the rows
  the term is neutral, so a search that never reached MusicBrainz ranks exactly
  as it did before.
- **Auto-import and the finder's "Best Pick" are the same pick.** Both rank the
  same candidate set through `mlo/cover_choice`; the unattended search now uses
  the finder route's own timeout as well, because a timeout is part of the
  question.
- **A replaced cover is a new URL even when the SERVER wrote it.** The import's
  cover step and script 5 leave no response for the UI to learn a version from,
  so the album and track payloads now carry `cover_token` (mtime + size) and
  every thumbnail URL carries it — no more track rows drawing the previous
  cover. Offline, the warmed artwork is still used as the last resort.
- **A web rating is graded per track, by default.** `grade_check_web_rating`
  (ON) fails a track with no `WEBRATING`, stands down entirely while web ratings
  are switched off, and never applies to a music video.

## The app's own files are in the app's own shape

- **`.mlo_expected.json` is now `.mb_expected`.** The first reader renames an
  older file rather than losing the release's tracklist, and the temp-file
  sweep keeps recognising the old name.
- **The JSON sidecars were written through a text-mode handle** — on Windows
  that turned every newline into CRLF and left no final newline, which is the
  "extra character on every line" the owner saw. One writer
  (`mlo.paths.canonical_json_text`) now states the form: UTF-8, LF, no trailing
  whitespace, exactly one final newline, `ensure_ascii=False`. The manifest, the
  covers manifest and the pending/importing markers all go through it.
- **Both the manifest and the two `description.txt` files are graded on their
  BYTES** (`grade_check_sidecar_format`, ON, per album and per artist folder),
  and the scripts that fetch them repair them: Release tracklist (15) normalizes
  a manifest it would otherwise skip, and the artist/album description fetches
  normalize a stored file without a network round trip.

## More providers for artist images and descriptions

- **Wikidata (P18) and Bandcamp join keyless**, verified live: an artist's own
  photo from Wikimedia Commons (exact through the MusicBrainz `wikidata`
  relation when the folder states an MBID — Gorillaz and Hania Rani both
  answered a 1200 px Commons URL) and an album's own "about" text from its
  Bandcamp page (Hania Rani's *Esja*: 6,163 characters, no credential).
- **Last.fm and Discogs join keyed** (artist bios/photos, album wiki text,
  release notes, artist profiles) and say so in the Sources panel until their
  key/token is pasted — a keyed source is skipped with a reason, never failed.
- **Album of the Year is registered in the Discover provider registry** for what
  it publishes (genres + user score, read by the genre and rating chains) and
  names the `aoty_cookie` it needs from the Cloudflare gate.

## Fixed

- An empty library no longer draws a phantom album: a bare directory directly
  under the music root is scaffolding, not a broken album.

## Limits, said plainly

- RYM and AOTY could not be exercised end-to-end from this machine (both are
  Cloudflare-fronted and no cookie was available here): their fetch, resolve,
  solver and parse paths are covered by hermetic tests against captured markup
  and a FlareSolverr-shaped stub, and the live order/cookie plumbing is proven,
  but the real solve needs the owner's cookie or a running solver.
- AOTY publishes no per-track genre, and Last.fm/Discogs need credentials; the
  new Discover registry entry for AOTY is metadata-only on purpose — no AOTY or
  RYM *chart* feed is read yet, so no shelf is built from one.
