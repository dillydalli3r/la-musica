# la musica 1.3.0 — the number is one source's own, and the chain is a graph

Five things the owner hit in one library: a web rating that did not match the
site it came from, a parent genre that appeared on some tracks and not others,
an album description fetched from a different album, Spanish lyrics that never
got translated, and an import that ran its steps one after another when most of
them had no reason to wait. Each was reproduced first.

## Web ratings: one source's own number, on a one-decimal display

- **The value is the FIRST source in the chain that answers** — the configured
  order IS the hierarchy (RateYourMusic → Album of the Year → MusicBrainz), a
  TRACK-level answer beats an album-level one whoever stated it, and the walk
  STOPS there: a source below the one that answered is never asked. The value
  used to be a vote-weighted MEAN, which put a number on the file that no site
  publishes: measured on *Clint Eastwood*, RYM 4.54 + AOTY 95 + MusicBrainz 83
  averaged to 92, so the tag read **4.6** while the site said 4.54.
- **The conversion truncates to the app's own grid, and the readout prints one
  decimal.** The grid points (0.05 on the 0-5 scale) are exactly the boundaries
  a one-decimal rounding can land on, so truncating makes the displayed number
  equal the source's own reading — RYM's 4.54 → 90 → **"4.5"** (rounding to 91
  gave 4.55 → "4.6", the double rounding the owner reported), 4.45 → 89 →
  "4.5", 4.35 → 87 → "4.4". A tolerance inside the conversion keeps a score
  that IS on the grid from truncating a step low on binary noise.
- **The shape is graded and canonicalised.** A rating on a file is a whole
  number 0-100 in its one spelling: a source's own scale pasted in raw
  ("4.54") fails as `WEBRATING_FORMAT` (`ALBUMWEBRATING_FORMAT` on the album
  twin) and names script 24 as the fixer — never "fixed" by a guess — while
  "91.0" is the same rating spelled differently, rewritten to "91" by the
  format pass with the grade agreeing. The presence check and the shape are one
  counted check per track. The owner's two albums were re-tagged live: 31 of 31
  tracks took their score from RYM alone.

## Genres: only what a source stated, and a row that says where it sits

- **A redundant parent is dropped wherever it sits.** The rule used to fire
  only when the parent came FIRST, so the same pair in the other order kept it
  — 15 of 31 tracks carried a parent beside its own child while a sibling did
  not ("the parent genres of some tracks are the normal genre of others"). One
  predicate (`_drop_derived_parents`) is now shared by the writer, the format
  pass and the grader, which reports it anywhere in the list.
- **The wizard's per-track row shows the PARENTS, not the whole path.** A path
  ends on the name it describes ("Hip Hop › Experimental Hip Hop"), so the row
  said the same thing twice and the owner asked what the arrow was for; it now
  reads **"under Hip Hop"**, and every chip carries its own place as its
  tooltip. The step's copy no longer claims the app "writes the family in the
  first slot" — the family has been a derived VIEW since 1.2.0 and nothing
  writes it.

## Descriptions: by identity first, and a name match must be strict

- An album description is fetched from the release group's own Wikipedia (or
  Wikidata) link when the MBID is known, and otherwise only from a hit whose
  title EQUALS the album title with the artist confirmed — a hit that merely
  CONTAINS the title is rejected before any article is fetched. The owner's
  self-titled *Gorillaz* (2001) held Wikipedia's article for *The Mountain*, a
  different Gorillaz album; it now holds its own, with the URL recorded in the
  provenance.

## Lyrics transforms: a wrong LANGUAGE tag no longer suppresses the translation

- Two Latin-script languages cannot be told apart by script, so the declared
  `LANGUAGE` (the release's own text representation) is now weighed against the
  lyrics' own function words and the text wins when it outvotes the declaration.
  The owner's "Latin Simone (Que Pasa Contigo)" carried `eng` over Spanish
  words and got no translation at all; script 17 now writes **TRANSLATION-EN**
  for it, and the grader's missing-translation issue for that track goes from
  present to absent. The tag itself stays filled-only.

## The import chain is a graph, and advisory waits for lyrics

- **`run_chain` runs steps in dependency order with two lanes.** Every script
  that reads or writes the album's own files is the "album" lane and runs ONE
  AT A TIME — a tag writer rebuilds a whole container from what it read, so two
  of them on one file lose one side's writes — while the artist-art and
  submission steps run BESIDE it, off the run's bar. Among ready steps the
  earliest in the configured order goes first, so a declared edge
  (`mlo.scripts.SCRIPT_DEPS`) can only delay a step, never move one forward.
- **The advisory fetch is a step of that graph, AFTER the lyrics script.** It
  used to run before the chain, which decided explicitness on half the evidence;
  the instrumental check is spliced in at the front (the lyrics script reads
  the tag it writes) and script 8 waits for both. The pinned order is
  instrumentals → lyrics → advisories → Auto Tagging.
- **A file that a step ends on swaps its own copy of the config**: the free
  lane never sees the spine's force keys, and a finished run's results come
  back in the run's own order, not completion order. Measured on a scratch copy
  of the owner's album: 5.8 s of lyric fetches with 0.2 s of artist work
  beside them, together 5.8 s — the side steps ride inside the spine.

## Fixed

- **A newly staged album starts with an EMPTY MusicBrainz release id.** The
  wizard's drop path never reset the release identity, so album B opened with
  album A's id in the field; one mechanism keyed on the album path clears it
  while the tag-based detection still fills from the new album's own tags.
- **Ratings, genres, descriptions and lyrics all follow the provenance the app
  records**: `WEBRATING_SOURCE` names the ONE source whose number the file
  carries, `GENRE` holds only names a source stated, and a description's URL is
  the article actually stored.
- The library's own genre tags were rewritten to the parents-free lists the
  rule produces (a second pass changes nothing), and the two albums' ratings
  re-fetched to the truncated values.

## Limits, said plainly

- **Two tag-writing scripts still never run at once.** They would need a
  per-file write lock; until that exists the album lane stays serial and the
  spec says why instead of pretending the graph is fully parallel.
- **A rating is only as precise as its tag.** The 0-100 integer is what the
  format carries, so a source's 4.54 is stored as 90 and shown as 4.5 — the
  same number the source's own page rounds to, and never a second rounding on
  top of the first.
- The word-level `<mm:ss.xx>` markers inside a stored translation are the app's
  own enhanced-sync pass (`lrc_enhanced_word_sync`), not text the model added.