#!/usr/bin/env python3
"""The genre vocabulary and the two-slot list policy — offline, no network.

What this pins:

  * every curated genre -> family entry and every alias target is a REAL
    MusicBrainz genre (the bundled `mlo._genre_names.MB_GENRES`), so the
    tables cannot rot into invented names;
  * `canonical` folds case, whitespace and hyphen/space variants, resolves the
    alias table, and returns None for anything else;
  * `parent_of` answers with a FAMILY, never with a specific genre, and with
    None rather than a wrong guess;
  * `normalize_genres` is the list policy: the family FIRST, the specific
    genres behind it, the family derived rather than repeated, duplicates
    collapsed, the cap enforced, and an unrecognised name kept verbatim
    (lossless);
  * `server.integrations.genre_category` buckets a name by that FAMILY and only
    by its spelling when the vocabulary has no family for it, so the page's
    fixed buckets cannot disagree with the family the app derived (the owner's
    `New Wave` chip sat under OTHER while its family said rock);
  * `split_stored` reads a repeated field, a " / "-joined value and a
    "; "-joined legacy value as the same list;
  * `issues` reports exactly the structural problems the grader acts on.

Run:  python tools/test_genre_vocab.py   (exit 0 = pass, 1 = failure)
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mlo import genres as G                      # noqa: E402
from mlo import genre_vocab as V                 # noqa: E402
from mlo._genre_names import MB_GENRES           # noqa: E402
# The fixed filter buckets GET /api/genres/facets groups by: server.integrations
# is imported for genre_category() alone, because that table is the second genre
# taxonomy the app carries and the place a family has to agree with a bucket.
# The import is offline (its own module-level imports are httpx and mlo modules;
# nothing is requested) and costs under a second.
from server import integrations as I             # noqa: E402

fails = []


def check(ok, label, extra=""):
    if not ok:
        fails.append(f"{label}{(': ' + str(extra)) if extra else ''}")


# --------------------------------------------------------------------------- #
# 1. The tables only name real MusicBrainz genres
# --------------------------------------------------------------------------- #
check(len(MB_GENRES) > 2000, "the bundled vocabulary is the real list",
      len(MB_GENRES))
bad_fams = [f for f in V.FAMILIES if f not in MB_GENRES]
check(not bad_fams, "every family is a MusicBrainz genre", bad_fams)
bad_map = [k for k in V._FAMILY_OF if k not in MB_GENRES]
check(not bad_map, "every mapped genre is a MusicBrainz genre", bad_map)
bad_value = sorted({v for v in V._FAMILY_OF.values() if v not in V.FAMILIES})
check(not bad_value, "every mapped family is in FAMILIES", bad_value)
bad_alias = {k: v for k, v in V.ALIASES.items() if v not in MB_GENRES}
check(not bad_alias, "every alias target is a MusicBrainz genre", bad_alias)

# --------------------------------------------------------------------------- #
# 2. canonical()
# --------------------------------------------------------------------------- #
check(V.canonical("Shoegaze") == "shoegaze", "case is folded to MusicBrainz's")
check(V.canonical("  drum   and  bass ") == "drum and bass", "whitespace collapses")
check(V.canonical("Hip-Hop") == "hip hop", "the space/hyphen swap resolves")
check(V.canonical("rnb") == "r&b", "the alias table resolves")
check(V.canonical("OST") is None, "a spelling with no MusicBrainz genre is None")
check(V.canonical("") is None and V.canonical(None) is None, "empty is None")

# --------------------------------------------------------------------------- #
# 3. parent_of()
# --------------------------------------------------------------------------- #
FAMILIES = set(V.FAMILIES)
for name in ("shoegaze", "post-punk", "trip hop", "jazz rap", "folk metal",
             "soul jazz", "lo-fi hip hop", "afrobeat", "drone metal",
             "uk garage", "gospel choir", "celtic", "flamenco"):
    fam = V.parent_of(name)
    check(fam is None or fam in FAMILIES, f"parent_of({name}) is a family", fam)
check(V.parent_of("shoegaze") == "rock", "shoegaze is rock")
check(V.parent_of("post-punk") == "punk", "post-punk is punk, not rock")
check(V.parent_of("folk metal") == "metal", "the more specific family wins")
# The "wave" names are NOT one family. The keyword rule carries "wave" as an
# ELECTRONIC word, which is right for the genre called "wave" and for the
# synth-driven ones — and wrong for new wave (a pop/rock movement named for the
# thing it was new AGAINST) and no wave (an avant-garde scene, not electronic at
# all). The owner's report was a new wave track whose genre read
# "Electronic; New Wave": the family was the first genre in the tag AND the
# wrong one.
check(V.parent_of("new wave") == "rock", "new wave is rock, not electronic")
check(V.parent_of("no wave") == "experimental", "no wave is experimental, not electronic")
check(V.parent_of("new romantic") == "pop", "new romantic is pop")
check(V.parent_of("dark wave") == "electronic", "the synth-driven waves stay electronic")
check(V.parent_of("wave") == "electronic", "the genre called wave is electronic")
check(V.parent_of("rock") == "rock", "a family is its own parent")
check(V.parent_of("qawwali") is None, "no family beats a wrong family")

# --------------------------------------------------------------------------- #
# 4. genre_category() — the fixed buckets GET /api/genres/facets groups by
# --------------------------------------------------------------------------- #
# The buckets are server/integrations.py's own coarse filter table, not the
# vocabulary — two taxonomies, deliberately. A name is bucketed by its FAMILY
# first (rock, hip hop, …) and the table's existing keyword rules then read that
# family, which is the whole mapping; the name as spelled is only read when the
# vocabulary has no family for it or that family matches no bucket. That is what
# keeps the page from filing a genre by how its name happens to look: the
# owner's card OTHER held the chip `New Wave` (8 tracks) while METAL and ROCK
# bucketed right, because "new wave" reads as the keyword "wave" and only the
# spelling was consulted.
check(I.genre_category("new wave") == "Rock",
      "the owner's report: new wave is bucketed by its family",
      I.genre_category("new wave"))
check(I.genre_category("New Wave") == "Rock", "case is folded before the lookup")
check(I.genre_category("shoegaze") == "Rock", "shoegaze is rock")
check(I.genre_category("hip hop") == "Hip-Hop", "a family bucket")
check(I.genre_category("gospel") == "Soul & Funk", "the buckets fold families in")
check(I.genre_category("folk metal") == "Metal", "the keyword order still holds")
check(I.genre_category("dark wave") == "Electronic",
      "the synth-driven waves keep their family's bucket",
      I.genre_category("dark wave"))
check(I.genre_category("no wave") == "Other",
      "no wave's family (experimental) has no bucket here, and neither has its "
      "spelling — Other is the honest answer, not a new bucket")
check(I.genre_category("trip hop") == "Electronic",
      "the vocabulary files trip hop under electronic, so the bucket follows it",
      I.genre_category("trip hop"))
check(I.genre_category("polka") == "Other",
      "a name the vocabulary cannot place still matches by keyword, and polka "
      "matches nothing")
check(I.genre_category("qawwali") == "Other", "no family, no keyword, Other")
check(I.genre_category("") == "Other" and I.genre_category(None) == "Other",
      "an empty name is Other, never None")
# The composition of the two tables, spelled out: a family the nine buckets have
# no head for keeps Other instead of a bucket invented for it (the vocabulary is
# finer than the buckets — it publishes blues, latin and reggae as families and
# the page shows them under Other, which is a documented choice, not a slip).
BUCKET_OF_FAMILY = {
    "metal": "Metal", "punk": "Rock", "hip hop": "Hip-Hop", "rock": "Rock",
    "pop": "Pop", "jazz": "Jazz", "blues": "Other", "soul": "Soul & Funk",
    "funk": "Soul & Funk", "r&b": "Soul & Funk", "reggae": "Other",
    "country": "Folk", "folk": "Folk", "classical": "Classical",
    "latin": "Other", "gospel": "Soul & Funk", "industrial": "Other",
    "experimental": "Other", "ambient": "Electronic", "new age": "Other",
    "easy listening": "Other", "dance": "Other", "electronic": "Electronic",
    "instrumental": "Other", "spoken word": "Other", "comedy": "Other",
    "christmas music": "Other", "children's music": "Other",
}
check(set(BUCKET_OF_FAMILY) == set(V.FAMILIES),
      "the table covers exactly the families the vocabulary publishes",
      sorted(set(V.FAMILIES) ^ set(BUCKET_OF_FAMILY)))
bad_bucket = {f: (b, I.genre_category(f)) for f, b in BUCKET_OF_FAMILY.items()
              if I.genre_category(f) != b}
check(not bad_bucket, "every family buckets where the keyword rules put it",
      bad_bucket)

# --------------------------------------------------------------------------- #
# 5. normalize_genres()
# --------------------------------------------------------------------------- #
check(G.normalize_genres(["Shoegaze"]) == ["Rock", "Shoegaze"],
      "the family is derived and goes first", G.normalize_genres(["Shoegaze"]))
check(G.normalize_genres(["Shoegaze", "Dream Pop"]) == ["Rock", "Shoegaze"],
      "the family takes slot 1 from a second specific genre")
check(G.normalize_genres(["Shoegaze", "Dream Pop"], count=3)
      == ["Rock", "Shoegaze", "Dream Pop"], "count 3 fits two specifics")
check(G.normalize_genres(["rock", "Shoegaze"]) == ["Rock", "Shoegaze"],
      "a family given first is not duplicated by the derived one")
check(G.normalize_genres(["Hip-Hop", "Trap"], count=3) == ["Hip Hop", "Trap"],
      "a family given first keeps its slot, most specific behind it")
check(G.normalize_genres(["Shoegaze", "shoegaze", "SHOEGAZE"]) == ["Rock", "Shoegaze"],
      "duplicates collapse case-insensitively")
check(G.normalize_genres(["Shoegaze"], count=1) == ["Shoegaze"],
      "count 1 has no room for the family")
check(G.normalize_genres(["Shoegaze", "Nonsense"]) == ["Rock", "Shoegaze"],
      "a recognised genre wins the slot a junk name would have taken",
      G.normalize_genres(["Shoegaze", "Nonsense"]))
check(G.normalize_genres(["Nonsense"], count=2) == ["Nonsense"],
      "an unrecognised name is stored verbatim, not casefolded",
      G.normalize_genres(["Nonsense"]))
check(G.normalize_genres([]) == [] and G.normalize_genres(None) == [],
      "nothing in, nothing out")
check(G.normalize_genres(["Rock; Alternative Rock / Shoegaze"])
      == ["Rock", "Alternative Rock"],
      "one stored value holding a list is read as names",
      G.normalize_genres(["Rock; Alternative Rock / Shoegaze"]))

# --------------------------------------------------------------------------- #
# 6. split_stored() and format_genres()
# --------------------------------------------------------------------------- #
check(G.split_stored("Rock / Shoegaze") == ["Rock", "Shoegaze"], "the ' / ' form")
check(G.split_stored("Rock; Shoegaze") == ["Rock", "Shoegaze"], "the '; ' form")
check(G.split_stored("Shoegaze") == ["Shoegaze"], "the single form")
check(G.split_stored("") == [] and G.split_stored(None) == [], "the empty form")
check(G.format_genres(["Shoegaze", "dream pop"]) == "Rock / Shoegaze / Dream Pop",
      "rendering puts the family first", G.format_genres(["Shoegaze", "dream pop"]))

# --------------------------------------------------------------------------- #
# 7. issues()
# --------------------------------------------------------------------------- #
check(G.issues(["rock", "shoegaze"]) == [], "family then specific is in shape")
check(G.issues(["rock", "shoegaze", "dream pop"], count=2)
      == ["too many (3 > 2)"], "over the cap")
check(G.issues(["shoegaze", "rock"]) == ["family genre must be the first one"],
      "family last is out of shape")
check("duplicate" in G.issues(["shoegaze", "shoegaze", "rock"]), "a duplicate")
check(any("not a known genre" in p for p in G.issues(["nonsense", "rock"])),
      "an unrecognised name is reported")
check(G.issues([]) == ["missing"], "an empty list is missing")
check(G.DEFAULT_GENRE_COUNT == 2 and G.GENRE_COUNT_MAX == 3,
      "two slots by default, three at most")

if fails:
    print("genre vocab: FAILED")
    for line in fails:
        print("  - " + line)
    sys.exit(1)
print("genre vocab: all assertions passed")
sys.exit(0)
