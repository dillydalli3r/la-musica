# la musica 2.0.2 — an album import asks for the album, never for its tracks

An audit of what an auto-import sends to Soulseek. The owner's report
(2026-10-17): *"If I'm auto-importing an album, tracks should NOT be searched
for individually, as is the current behavior."* A CD import was POSTing 11
searches — its catalog number, its barcode, the release's MusicBrainz id and
the first four tracks' own recording ids and `artist title` (measured on
Nirvana's *Nevermind*: `GED24425`, `072064242524`, `8e06fdc4-…`, then
`Smells Like Teen Spirit` and three more beside their UUIDs). The album's batch
now carries what names the ALBUM and nothing else: the configured trait
templates (a pressing by catalog number / barcode, digital by artist album
year), the MusicBrainz alias queries when the release is filed under another
name, and the release's own id. The same *Nevermind* CD asks 3 questions.

## Soulseek

- **No per-track query exists any more.** `_mbid_queries` adds the release's own
  MusicBrainz id — not its tracks' recording ids, not their "artist title". A
  track name asks the network for singles, compilations and loose files that
  hold one song, which is not the release being imported; the id is
  album-level, so a peer folder that names ids stays reachable.
- **`soulseek_auto_mbid_tracks` and `_release_tracks` are gone** with the
  behavior. A saved config that still holds the key is inert — nothing reads
  it, it is not in the schema, and `normalize_config` does not add it back —
  and the Settings row that asked for it is gone too (the MBID switch now reads
  *Also search by the release MBID*).
- **The single parallel batch is unchanged.** The release id still goes out
  with the templates in ONE search window (one POST per query, all polled in
  one loop), and the scoring, the completeness ratio, the log gate and the
  candidate walk are untouched.
- **The manual per-track surface is unaffected**: pasting a recording MBID into
  the search box still asks for that one song (artist + title, its album, and
  the id), because that is a question about a track the user asked, not an
  album import.
- **A wish replays album-level queries.** A wish stores the queries its job
  searched with, so wishes created from here on carry the album's own set. A
  wish stored before this release keeps the queries it was stored with (a
  stored query list is a record, never re-derived).

## The contract

`docs/OPTIMIZATION-GRADING-SPEC.md` R432 is rewritten from "a release is
searched by its tracks' own names" to **"an album import asks the network for
the ALBUM, never for its tracks one at a time"**, naming the removed key, the
removed function and the manual per-track surface that stays. Pinned in
`tools/test_soulseek_candidates.py`: a release whose payload states a full
tracklist yields its trait template and the release id ONLY — no title, no
recording id, with the retired key set to 10 in the config — and the two
end-to-end job tests assert exactly that on slskd's own POST journal.

## Verification

Every Python suite in `tools/` and every Node suite runs on the frozen tree
with no failures (exit 2 = cannot run here counted as pass), together with the
web gate (`tsc -b`, `oxlint src`, `npm run build`, exit 0).

Measured on the real module with the owner's own release (a CD, catalog
`GED24425`, barcode `072064242524`, a flat 12-track `media` payload as
`integrations.resolve_release` publishes it): `release_queries` answers **3**
queries — the catalog number, the barcode and the release id — and asserts that
neither the track titles nor the recording ids appear anywhere in the list (11
before). `tools/check_versions.py v2.0.2` reports all 12 copies and the tag
agreeing.
