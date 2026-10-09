# la musica 4.9.1 — web ratings default to RateYourMusic

One behaviour change on top of 4.9.0. `web_ratings_sources` now ships all four
sources with **RateYourMusic first**:

```
["rateyourmusic", "musicbrainz", "albumoftheyear", "discogs"]
```

4.9.0 shipped MusicBrainz alone, on the reasoning that it is the only source
with no credential and no archive leg. RYM leads now because it is the widest
public verdict the app can read — one score from tens of thousands of ratings —
and its rating rides the **same page fetch its genres already make**, so an
install that was already asking RYM for genres pays nothing extra for the score.

**MusicBrainz stays on, and that is deliberate.** It is the only one of the four
that answers for a TRACK at all: RYM, Album of the Year and Discogs are
album-level, so an all-archive list writes `ALBUMWEBRATING` on every track and
no `WEBRATING` on any of them. If you only want album scores, untick it in
**Settings → Discovery**.

**What it costs.** RYM, Album of the Year and Discogs are all archive-backed
when their live pages refuse, and RYM's live page needs a `cf_clearance` cookie
that matches the configured `rym_user_agent`. Measured on one album: **23 s**
with the archive-backed three enabled against **1.2 s** for MusicBrainz alone.
The page is cached for 30 days (misses included), so that is a *first-run* cost
per album, not a per-run one — a Run All over a library pays it once per album
and is fast afterwards.

An existing install keeps whatever it saved: the key is new in 4.9.0/4.9.1, so
nothing has one yet and everyone gets the new default. Changing the list in
Settings → Discovery is the only thing that writes it.
