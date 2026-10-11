#!/usr/bin/env python
"""Alias tags as SEARCH names (`mlo.audio.file_aliases` and its two ladders).

A file may carry the alias MusicBrainz states for a name in another language
(`TITLEALIAS`, `ARTISTALIAS`, `ALBUMALIAS`, bare or locale-suffixed like
`TITLEALIAS-JA`). The app already searched with MusicBrainz's aliases — but only
by MBID, so a file that HAS an alias and NO `MUSICBRAINZ_*ID` tags had no
alternative name to search with at all, which is precisely the track whose name
is published in another language (why the alias is on the file).

Everything here is offline: the MusicBrainz alias lookup is stubbed to answer
nothing, so what is proved is that the FILE's own tags reach the ladders.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mlo import audio, lyrics_fetch  # noqa: E402

PASS = 0
FAIL = 0


def ok(cond, label):
    global PASS, FAIL
    if cond:
        PASS += 1
    else:
        FAIL += 1
        print(f"FAILED: {label}")


TAGS = {
    "TITLE": "Hikari",
    "ARTIST": "Utada Hikaru",
    "ALBUM": "Deep River",
    "TITLEALIAS": "光",
    "TITLEALIAS-JA": "ひかり",
    "ARTISTALIAS": "宇多田ヒカル",
    "ALBUMALIAS-JA": "ディープ・リバー",
    # A container spelling, and a non-alias tag that must never be read.
    "----:com.apple.iTunes:TITLEALIAS-EN_PH": "Light",
    "MOOD": "Happy",
    "GENRE": "J-Pop",
}

# --------------------------------------------------------------------------- #
# Reading the values off a file's tag map
# --------------------------------------------------------------------------- #
ok(audio.alias_values(TAGS, "TITLEALIAS") == ["光", "ひかり", "Light"],
   audio.alias_values(TAGS, "TITLEALIAS"))
ok(audio.alias_values(TAGS, "ARTISTALIAS") == ["宇多田ヒカル"],
   audio.alias_values(TAGS, "ARTISTALIAS"))
ok(audio.alias_values(TAGS, "ALBUMALIAS") == ["ディープ・リバー"],
   audio.alias_values(TAGS, "ALBUMALIAS"))
ok(audio.alias_values({"TITLEALIAS": "  "}, "TITLEALIAS") == [],
   "a blank alias value is not a name to search with")
ok(audio.alias_values({"TITLEALIAS": "光", "TITLEALIAS-JA": "光"}, "TITLEALIAS") == ["光"],
   "the same name twice (two locales) is one candidate")
ok(audio.alias_values(TAGS, "RELEASETYPE") == [],
   "a family nothing carries is empty")
ok(audio.alias_values({}, "TITLEALIAS") == [], "no tags -> no aliases")

got = audio.file_aliases(TAGS)
ok(got == {"title": ["光", "ひかり", "Light"],
           "artist": ["宇多田ヒカル"],
           "album": ["ディープ・リバー"]},
   f"file_aliases is the shape the shared walk takes: {got}")
ok(audio.file_aliases({"TITLE": "x"}) == {},
   "a file with no alias tags contributes nothing")

# --------------------------------------------------------------------------- #
# They reach the lyrics ladder — with NO MusicBrainz ids anywhere
# --------------------------------------------------------------------------- #
_real_search = None
try:
    from server import integrations
    _real_search = integrations.search_aliases

    def _no_mb(*a, **k):
        return []

    integrations.search_aliases = _no_mb

    def tag(name):
        return TAGS.get(name, "")

    without = lyrics_fetch._search_aliases(tag, {}, "Utada Hikaru", "Hikari",
                                           "Deep River")
    ok(without == {}, f"no MBIDs and no tags passed -> no aliases: {without}")

    with_tags = lyrics_fetch._search_aliases(tag, {}, "Utada Hikaru", "Hikari",
                                              "Deep River", TAGS)
    ok(with_tags.get("title") == ["光", "ひかり", "Light"], with_tags)
    ok(with_tags.get("artist") == ["宇多田ヒカル"], with_tags)
    ok(with_tags.get("album") == ["ディープ・リバー"], with_tags)

    # And the advisory ladder, which merges the same tags into its own names.
    subs = integrations._advisory_alias_queries(
        "Utada Hikaru", "Hikari", "Deep River", "", TAGS, {})
    ok(subs, "the advisory ladder has substitutions from the file's tags alone")
    titles = {row[1] for row in subs}
    ok("光" in titles, f"…including the localized title: {sorted(titles)}")
finally:
    if _real_search is not None:
        integrations.search_aliases = _real_search

print(f"alias_tags: {PASS} passed, {FAIL} failed")
raise SystemExit(1 if FAIL else 0)