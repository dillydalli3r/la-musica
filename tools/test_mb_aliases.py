"""MusicBrainz aliases on the pages: which alias a reader is shown.

The MB pages show an entity's name in the reader's locale in parentheses —
`beets_locale`, the same setting the beets import translates names with — and
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
ok(alias(rows, {"beets_locale": "en"}, "Sawayama") == "Rina Sawayama",
   "the reader's locale wins")
ok(alias(rows, {"beets_locale": "ja"}, "Sawayama") == "沢山リナ",
   "and it follows the setting (ja)")
ok(alias(rows, {"beets_locale": "JA"}, "Sawayama") == "沢山リナ",
   "the locale is folded (case)")
ok(alias(rows, {"beets_locale": "ja-JP"}, "Sawayama") == "沢山リナ",
   "a regional locale matches its language (ja-JP -> ja)")

# 2. a locale match that is not flagged primary still beats a foreign primary
rows2 = [
    {"name": "Rina Sawayama", "locale": "en", "primary": True, "type": None},
    {"name": "リナ・サワヤマ", "locale": "ja", "primary": False, "type": None},
]
ok(alias(rows2, {"beets_locale": "ja"}, "Sawayama") == "リナ・サワヤマ",
   "any alias in the locale beats another locale's primary")

# 2b. a romanization answers a reader who asked for the bare language
rows3 = [{"name": "Rina Sawayama", "locale": "ja-Latn", "primary": True, "type": None}]
ok(alias(rows3, {"beets_locale": "ja"}, "Sawayama") == "Rina Sawayama",
   "ja-Latn answers a ja preference (the language prefix matches)")

# 2c. MusicBrainz's own separators: `_` for a region, `-` for a script
rows_reg = [
    {"name": "Utada Hikaru", "locale": "en_PH", "primary": True, "type": "Artist name"},
    {"name": "Hikaru Utada", "locale": "en", "primary": True, "type": "Artist name"},
]
ok(alias(rows_reg, {"beets_locale": "en_PH"}, "宇多田ヒカル") == "Utada Hikaru",
   "a regional locale matches exactly (en_PH)")
ok(alias(rows_reg, {"beets_locale": "en"}, "宇多田ヒカル") == "Hikaru Utada",
   "…and a bare language takes the bare-language alias, not the regional one")

# 3. no locale alias at all: MusicBrainz's own primary is the fallback
rows4 = [{"name": "Rina Sawayama", "locale": "en", "primary": True, "type": None}]
ok(alias(rows4, {"beets_locale": "de"}, "Sawayama") == "Rina Sawayama",
   "a locale with no alias falls back to the primary one")
ok(alias(rows4, {}, "Sawayama") == "Rina Sawayama",
   "no configured locale falls back to the primary one too")

# 4. nothing usable: the caller shows the name alone
ok(alias([{"name": "X", "locale": "en", "primary": False, "type": None}],
         {"beets_locale": "de"}, "Sawayama") == "",
   "an unlabelled non-primary alias is never guessed at")
ok(alias([], {"beets_locale": "en"}, "Sawayama") == "",
   "no aliases at all is no alias")
ok(alias(None, {"beets_locale": "en"}, "Sawayama") == "",
   "a payload without the key is no alias")

print("== what is never shown ==")
# duplicate names, search hints, and junk rows
ok(alias([{"name": "Sawayama", "locale": "en", "primary": True, "type": None}],
         {"beets_locale": "en"}, "Sawayama") == "",
   "an alias equal to the name is not shown ((X (X) never renders)")
ok(alias([{"name": "sawayama", "locale": "en", "primary": True, "type": None}],
         {"beets_locale": "en"}, "Sawayama") == "",
   "…and case alone is not a difference")
ok(alias([{"name": "Sawayama!", "locale": "en", "primary": True, "type": "Search hint"}],
         {"beets_locale": "en"}, "Sawayama") == "",
   "a MusicBrainz SEARCH HINT alias is never shown")
ok(alias([{"name": "Sawayama!", "locale": "en", "primary": True, "type": "search hint"},
          {"name": "Rina Sawayama", "locale": "en", "primary": False, "type": None}],
         {"beets_locale": "en"}, "Sawayama") == "Rina Sawayama",
   "…and the next candidate in the same locale is taken instead")
ok(alias([{"name": "  ", "locale": "en", "primary": True, "type": None},
          None, "junk"],
         {"beets_locale": "en"}, "Sawayama") == "",
   "blank and non-dict rows are ignored")

print("== the payload wires it in ==")
# The pages read `alias` off the rows the server builds; the three fetch sites
# carry `inc=aliases` so no row costs an extra request.
import inspect  # noqa: E402

src = inspect.getsource(intg)
for key, where in (('"genres+aliases"', "artist identity"),
                   ('{"artist": mbid, "inc": "aliases"}', "artist discography browse"),
                   ('"artist-credits+genres+aliases"', "release-group lookup"),
                   ('"media+aliases"', "release-group editions browse"),
                   ("+labels+isrcs+aliases", "release lookup")):
    ok(key in src, f"{where} asks MusicBrainz for aliases ({key})")
ok(src.count("alias_for(") >= 4, "and each of them attaches the chosen alias")

print(f"mb aliases: all {passed} assertions passed")
