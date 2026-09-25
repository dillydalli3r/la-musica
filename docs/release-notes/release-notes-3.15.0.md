# la musica 3.15.0 — an album stays one album, and the queue tells the truth

This release is the result of an audit of the whole add-to-library / auto-import
path, plus the fixes it turned up. Every rule below is in
**docs/OPTIMIZATION-GRADING-SPEC.md** (§7.12–§7.15, R84–R91).

## One release, one album folder

The queue could end up with **two albums from one add**. Three causes were found
and fixed:

- **Nothing checked the album's identity at the destination.** The import built
  `<library>/<Artist - Album>` and, when that path existed, silently escaped to
  `… (2)` — a second folder for the same album. The destination is now
  identity-checked (`_import_dest`): a folder holding the SAME release (its own
  `MUSICBRAINZ_ALBUMID`/`RELEASEGROUPID`, or the ids a framework album's marker
  was created with) refuses the import with a sentence instead of duplicating
  it, while a genuinely different album sharing the name still gets `(2)` — and
  the log says so, because the silence was the bug.
- **The "already running" check was not atomic.** `start_job` read the running
  set outside its lock, so two requests arriving together (a double press, the
  wishes worker plus a manual grab) both registered a job; the folder claim only
  made the second *wait*, after which it downloaded and imported the same album
  again. The check now runs inside the same critical section that registers the
  job — the refusal is `transient` (busy pipeline, not a failed release).
- **A partial move was reported as success.** `organize` collects per-file
  failures and still returns `ok: True`; `_import` never read them, so a locked
  track's file stayed behind — and a staging folder that still holds audio *is*
  an album by the app's own definition, so it appeared as a second release
  (and, on the success path, the download dir was cleared, i.e. the unmoved
  files were deleted). Any per-file failure now fails the import with a
  sentence, leaving the download intact for a retry.

## The queue's areas say what is really happening

- **An import chain still running is `Importing`, not `Completed`.** The job
  settles when the album is *in* the library, while the chain (links, metadata,
  cover art, the configured scripts) runs on its own thread — so the row said
  Completed before anything was imported. The chain now marks its job and
  reports its current step, `job_stage` keeps the row in **In progress**, and
  `_finish` writes that stage instead of "Done".
- **Three candidates at once, from three different peers.** Pinned as the
  contract it always was: `soulseek_candidate_slots` (3) candidates of ONE
  release together, never two folders from one peer (the second keeps its place
  for a later batch), `soulseek_search_concurrency` (3) releases at once, and
  slskd's `soulseek_download_slots` (9 = 3 × 3) as the outer ceiling — narrowed
  to `slots // releases` when a config makes the product impossible.

## Notifications for the whole way in

`server/imports.finish_album` is the single call every import path makes, and it
now emits **`import_started`** and **`import_done`** (with the chain's own
one-line summary) — wizard, downloads panel, sequential queue, bulk queue,
Soulseek auto-importer and the wish/artist-watch pipeline all announce the same
way. Both are switchable in **Settings → Notifications** and ON by default, next
to the download phase's existing switches. The add itself (`album_pending`) had
no switch and no OS notification; it is in the tray for every kind, and the new
keys are in the wizard's group too.

## What an import stops inheriting from the peer

An import decides four families for itself — the lyrics the fetch found, the
release's genres, the advisory pipeline's rating and the album's own cover art —
and until now it *filled* those tags, so a download arrived carrying the peer's
values and kept every one of them: the peer's genre, its rating, its lyric (the
fetch skips a track that already has one) and the art baked into its files.

`server/imports.py::drop_arrived_values` is now the import's first tag-writing
pass, before the family steps and before the chain: it empties those four slots,
which is what lets the writers — unchanged, still fill-only, so a user's own edit
keeps surviving their `/run` and the wizard's steps — land what the import found.
A family kept in **Settings → Import pipeline → Decide by hand** is left exactly
as it arrived, and **`import_keep_synced_lyrics`** (new, off) is the lyric
family's one exception: a track whose lyric already carries timestamps keeps it
and the fetch skips that track.

## Smaller, and just as visible

- **An alias is a translation, not decoration.** MusicBrainz flags レディオヘッド
  as Radiohead's *primary* alias, and the ladder took a primary alias whatever
  its locale — so the artist page showed `Radiohead (レディオヘッド)`. An alias
  must now be at least as **readable** as the name it annotates: `宇多田ヒカル
  (Hikaru Utada)` for an English reader, `Radiohead (レディオヘッド)` for a
  Japanese one, and nothing either way when the name is already in the reader's
  script. Verified on the reported MBID: Radiohead's alias is empty, as are all
  300 of its release groups'.
- **Selecting a track silences the outgoing one at once** — before the new
  source is fetched and decoded, so a switch feels immediate instead of leaving
  the old track playing until the new one is ready (verified live with a
  deliberately delayed second stream). Skipped when a gapless swap already
  started the next track.
- **No more "12 tr" on artist pages.** The album cards state the grade verdict
  and stop there; the track count was noise on a grid of albums.

## Audited, and still open (named so nothing hides)

The audit found more than one release could fix at once. These are real, are
**not** fixed here, and are listed with their sites: the framework album is only
adopted when the import lands in a sibling folder whose name matches the
tag-derived path (and adoption can redirect the album root while leaving sidecars
in a ghost folder); a re-add reuses a **terminal** wish, so the "searching now"
promise is false and nothing searches it; a wish import
accepts an audio-less framework folder and reports success; the placeholder
cover occupies `cover.jpg`, which can leave an imported album without its own
cover; and a pending album whose wish went terminal renders a spinner that never
stops.
