# la musica 2.2.2 — nothing is paid for twice

A whole-app audit of the backend and the web client. Every fix below is a
duplicate the app was paying for on a path you actually use; each one names
what it measured.

## Opening the app, and reading the library

- **A tool's version is asked once per binary.** Detection re-spawned all nine
  tool probes on every cache miss — ~110 ms each on Windows — inside the first
  library read that wanted ffmpeg or flac. A cold detection pass now asks them
  in parallel too: **708 ms → 419 ms**, every version identical. The first
  library build of a 40-album / 200-track scope went **7.1 s → 0.4 s**; a warm
  build is unchanged (0.00 s, 216 KB in 4.5 ms).
- **The album index's state signature is an index, not a scan per album.** The
  cache that keeps a restart from re-reading the library asked, for every
  album, every row of the four state stores (audit evidence is one row per
  audited file). It is now one index per store revision, and a suite pins that
  the answer equals the old per-row scan byte for byte.
- **One ratings read per library build** instead of one ratings-table scan per
  album.

## Playback and charts

- **A play start is one INSERT.** It used to re-read and re-validate the whole
  config for its retention sweep, and open a fresh database connection for one
  row: **104 ms → 3.8 ms**.
- **Charts build their library index once.** Pressing Charts re-walked every
  album and track for a list of ten rows: **170 ms → 0.4 ms**.

## Lyrics

- **A search that found nothing is remembered for five minutes.** The lyrics
  manager asks `get`, then `search`, for the same names on a miss — a miss is
  now one walk of the provider chain instead of two. A *hit* is never
  remembered: the file's own lookup always asks the live chain.

## The web client

- **Typing in the library search no longer fetches the library per keystroke.**
  The fetch was enabled on the raw text; it now settles first (the same
  debounce MusicBrainz mode already used).
- **A page re-renders for the fields it reads.** The artist page, the
  Optimization hub, the playlists page and a playlist's detail page subscribed
  to the WHOLE store, so every progress frame during a run, every volume tick
  and every toast repainted an album grid or a whole tracklist.
- **Album cards scan their tracks once and repaint only when their album
  changed.** The format chip ran `albumTech` twice per render, and the card was
  not memoized, so one selection toggle repainted every card.
- **The Library copies the rows it draws, not the library.** Sorting by Rating
  reads the same values as before; the copies are made for the visible window
  only.
- **The export wizard loads when Export is pressed** (row menus import it
  lazily). The entry bundle went **524.6 kB → 496.3 kB** (gzip **160.1 →
  152.3 kB**); the rest of the bundle is `react-dom`, the player bar and the
  shell, named in the spec (§7.88, R459–R468).

## Upgrading

Nothing to do. Settings, tags, playlists and history are read as before; the
release carries the same assets as 2.2.1 for every platform.