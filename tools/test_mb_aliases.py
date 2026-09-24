"""MusicBrainz aliases on the pages: which alias a reader is shown.

The MB pages show an entity's name in the reader's locale in parentheses —
`locale`, the same setting the beets import translates names with — and
the ladder that picks it lives in `integrations.alias_for`. Fixtures only: no
network, no config file.

Run:  python tools/test_mb_aliases.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server import integrations as intg  # noqa: E402

passed = 0


def ok(cond, label):
    global passed
    assert cond, f"FAILED: {label}"
    passed += 1
    print(f"  ok {label}")


def alias(rows, cfg=None, name=""):
    return intg.alias_for(rows, cfg, name)


print("== the ladder ==")
# 1. the locale's own primary alias wins
rows = [
    {"name": "Rina Sawayama", "locale": "en", "primary": True, "type": None},
    {"name": "沢山リナ", "locale": "ja", "primary": True, "type": None},
]
ok(alias(rows, {"locale": "en"}, "Sawayama") == "Rina Sawayama",
   "the reader's locale wins")
ok(alias(rows, {"locale": "ja"}, "Sawayama") == "沢山リナ",
   "and it follows the setting (ja)")
ok(alias(rows, {"locale": "JA"}, "Sawayama") == "沢山リナ",
   "the locale is folded (case)")
ok(alias(rows, {"locale": "ja-JP"}, "Sawayama") == "沢山リナ",
   "a regional locale matches its language (ja-JP -> ja)")

# 2. a locale match that is not flagged primary still beats a foreign primary
rows2 = [
    {"name": "Rina Sawayama", "locale": "en", "primary": True, "type": None},
    {"name": "リナ・サワヤマ", "locale": "ja", "primary": False, "type": None},
]
ok(alias(rows2, {"locale": "ja"}, "Sawayama") == "リナ・サワヤマ",
   "any alias in the locale beats another locale's primary")

# 2b. a romanization answers a reader who asked for the bare language
rows3 = [{"name": "Rina Sawayama", "locale": "ja-Latn", "primary": True, "type": None}]
ok(alias(rows3, {"locale": "ja"}, "Sawayama") == "Rina Sawayama",
   "ja-Latn answers a ja preference (the language prefix matches)")

# 2c. MusicBrainz's own separators: `_` for a region, `-` for a script
rows_reg = [
    {"name": "Utada Hikaru", "locale": "en_PH", "primary": True, "type": "Artist name"},
    {"name": "Hikaru Utada", "locale": "en", "primary": True, "type": "Artist name"},
]
ok(alias(rows_reg, {"locale": "en_PH"}, "宇多田ヒカル") == "Utada Hikaru",
   "a regional locale matches exactly (en_PH)")
ok(alias(rows_reg, {"locale": "en"}, "宇多田ヒカル") == "Hikaru Utada",
   "…and a bare language takes the bare-language alias, not the regional one")

# 3. no locale alias at all: MusicBrainz's own primary is the fallback
rows4 = [{"name": "Rina Sawayama", "locale": "en", "primary": True, "type": None}]
ok(alias(rows4, {"locale": "de"}, "Sawayama") == "Rina Sawayama",
   "a locale with no alias falls back to the primary one")
ok(alias(rows4, {}, "Sawayama") == "Rina Sawayama",
   "no configured locale falls back to the primary one too")

# 4. nothing usable: the caller shows the name alone
ok(alias([{"name": "X", "locale": "en", "primary": False, "type": None}],
         {"locale": "de"}, "Sawayama") == "",
   "an unlabelled non-primary alias is never guessed at")
ok(alias([], {"locale": "en"}, "Sawayama") == "",
   "no aliases at all is no alias")
ok(alias(None, {"locale": "en"}, "Sawayama") == "",
   "a payload without the key is no alias")

print("== a Latin reading of a non-Latin name ==")
# The case the artist pages hit constantly: a Japanese title whose only aliases
# are a transliteration and a translation, nothing primary, no locale configured.
rows_jp = [
    {"name": "Lost Umbrella", "locale": "en", "primary": False, "type": "Release group name"},
    {"name": "ロストアンブレラ", "locale": "ja", "primary": False, "type": None},
]
ok(alias(rows_jp, {}, "ロストアンブレラ") == "Lost Umbrella",
   "a non-Latin name with no locale and no primary still gets its Latin reading")
# With `ja` configured, the only alias in that locale IS the name — and a
# reader who reads the script the name is written in gets NOTHING rather than a
# script they may not read. This is the mirror of "Radiohead (レディオヘッド)",
# and the rule is the reader's, not the name's.
ok(alias(rows_jp, {"locale": "ja"}, "ロストアンブレラ") == "",
   "a ja reader is not shown the Latin reading of a name they can already read")
ok(alias(rows_jp, {"locale": "de"}, "ロストアンブレラ") == "Lost Umbrella",
   "…and a locale with no alias of its own falls through to it")
# a transliteration beats a translation when both are offered
rows_tr = [
    {"name": "The Town's Transition", "locale": "en", "primary": False, "type": None},
    {"name": "Sen'i Hito-kukaku", "locale": "en-Latn", "primary": False, "type": None},
]
ok(alias(rows_tr, {}, "遷移一区画") == "Sen'i Hito-kukaku",
   "a *-Latn transliteration is preferred over a translation")
# a LATIN name is never given a Latin-reading fallback (nothing to romanize)
rows_lat = [{"name": "Abbey Road (Remaster)", "locale": "en", "primary": False, "type": None}]
ok(alias(rows_lat, {}, "Abbey Road") == "",
   "a Latin name with no matching alias shows nothing, not a stray variant")

print("== what is never shown ==")
# duplicate names, search hints, and junk rows
ok(alias([{"name": "Sawayama", "locale": "en", "primary": True, "type": None}],
         {"locale": "en"}, "Sawayama") == "",
   "an alias equal to the name is not shown ((X (X) never renders)")
ok(alias([{"name": "sawayama", "locale": "en", "primary": True, "type": None}],
         {"locale": "en"}, "Sawayama") == "",
   "…and case alone is not a difference")
ok(alias([{"name": "Sawayama!", "locale": "en", "primary": True, "type": "Search hint"}],
         {"locale": "en"}, "Sawayama") == "",
   "a MusicBrainz SEARCH HINT alias is never shown")
ok(alias([{"name": "Sawayama!", "locale": "en", "primary": True, "type": "search hint"},
          {"name": "Rina Sawayama", "locale": "en", "primary": False, "type": None}],
         {"locale": "en"}, "Sawayama") == "Rina Sawayama",
   "…and the next candidate in the same locale is taken instead")
ok(alias([{"name": "  ", "locale": "en", "primary": True, "type": None},
          None, "junk"],
         {"locale": "en"}, "Sawayama") == "",
   "blank and non-dict rows are ignored")

print("== the payload wires it in ==")
# The pages read `alias` off the rows the server builds; the three fetch sites
# carry `inc=aliases` so no row costs an extra request.
import inspect  # noqa: E402

src = inspect.getsource(intg)
for key, where in (('"genres+aliases"', "artist identity"),
                   # `+series-rels` rides the SAME browse request: it is what
                   # each row's `podcast` block (the Podcast series an episode
                   # is `part of`) comes from, at no extra MusicBrainz call.
                   ('{"artist": mbid, "inc": "aliases+series-rels"}',
                    "artist discography browse"),
                   ('"artist-credits+genres+aliases', "release-group lookup"),
                   # …+labels: the same request carries each edition's catalog
                   # numbers, which the fallback walk's distinct-pressing rule
                   # reads (spec R169)
                   ('"media+aliases+labels"', "release-group editions browse"),
                   ("+labels+isrcs+aliases", "release lookup")):
    ok(key in src, f"{where} asks MusicBrainz for aliases ({key})")
ok(src.count("alias_for(") >= 4, "and each of them attaches the chosen alias")

print("== an alias must be as readable as the name it annotates ==")
# A Latin name is never annotated with a foreign-script alias just because
# MusicBrainz flags that one primary: the pages used to show "Radiohead
# (レディオヘッド)" to an English reader, which translates nothing.
rows_radio = [{"name": "レディオヘッド", "locale": "ja", "primary": True, "type": None}]
ok(alias(rows_radio, {"locale": "en"}, "Radiohead") == "",
   "a name in the reader's own script takes no alias from a script they cannot read")
ok(alias(rows_radio, None, "Radiohead") == "",
   "…and the same holds with no config dict (the saved `locale` decides)")
ok(alias(rows_radio, {"locale": "ja"}, "Radiohead") == "レディオヘッド",
   "…while that alias is exactly what a ja reader wants")
# The mirror: no romanization for a reader who reads the script already.
rows_utada = [{"name": "Hikaru Utada", "locale": "en", "primary": True, "type": None}]
ok(alias(rows_utada, {"locale": "ja"}, "宇多田ヒカル") == "",
   "a Japanese name is not romanized for a ja reader")
ok(alias(rows_utada, {"locale": "en"}, "宇多田ヒカル") == "Hikaru Utada",
   "…and it still translates for an en reader")
# Another script, same rule.
rows_kino = [{"name": "Kino", "locale": "en", "primary": True, "type": None}]
ok(alias(rows_kino, {"locale": "ru"}, "Кино") == "",
   "a Cyrillic name takes no Latin alias for a ru reader")
ok(alias(rows_kino, {"locale": "en"}, "Кино") == "Kino",
   "…and does for an en reader")
# An alias in the reader's script always passes, even when it is not exactly
# the name: "Sawayama (Rina Sawayama)" is a fuller name, not a foreign one.
ok(alias([{"name": "Rina Sawayama", "locale": "en", "primary": True, "type": None}],
         {"locale": "en"}, "Sawayama") == "Rina Sawayama",
   "a readable alias is still shown beside a readable name")

print(f"mb aliases: all {passed} assertions passed")
