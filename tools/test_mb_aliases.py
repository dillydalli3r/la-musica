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

print("== when a name NEEDS its alias tag (spec R16a) ==")
# The tagging half of the readability rule above: a tag is written only where
# the configured locale cannot read the name. `alias_required` IS that answer
# — the writers, the importer's stamp, script 8's prescan and the grade all
# ask it — so the pages and the files agree (R87).
_req = intg.alias_required
ok(_req("Radiohead", {"locale": "en"}) is False,
   "a Latin name never needs an alias (Radiohead in an `en` library)")
ok(_req("Radiohead", {"locale": "ja"}) is False,
   "…under any locale: there is no script a Latin name's reader cannot read")
ok(_req("宇多田ヒカル", {"locale": "en"}) is True,
   "a Japanese name needs one for an `en` reader")
ok(_req("宇多田ヒカル", {"locale": "ja"}) is False,
   "…and none for a `ja` reader, who reads it already")
ok(_req("宇多田ヒカル", {}) is True,
   "no configured locale reads as the default (`en`)")
ok(_req("Кино", {"locale": "ru"}) is False and _req("Кино", {"locale": "en"}) is True,
   "the rule follows the locale's own script (Cyrillic: ru no, en yes)")
ok(_req("Björk", {"locale": "en"}) is False,
   "a Latin name with diacritics is Latin")
ok(_req("", {"locale": "en"}) is False and _req(None, {"locale": "en"}) is False,
   "no name is no alias")
ok(intg.alias_locale({}) == "en" and intg.alias_locale({"locale": "JA"}) == "ja",
   f"the tag locale falls back to the shipped default and folds case "
   f"({intg.alias_locale({})!r}/{intg.alias_locale({'locale': 'JA'})!r})")

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

print("== the alias TAGS the import writes ==")
# MusicBrainz's aliases are an IMPORT-DECIDED value (issue #59): mlo.autotag is
# the one writer, fed the rows server.integrations.release_lookup reads out of
# `inc=aliases` on the SAME release request (no extra request). The BARE key
# holds the ONE alias the reader's locale ladder chose (`alias_for`) — what the
# library shows beside the stored name — and ONLY when the name needs one
# (`alias_required`, issue #75): a name the configured locale can read is never
# annotated, and nothing is ever spelled per-locale.
import glob as _glob  # noqa: E402
import shutil as _shutil  # noqa: E402
import subprocess as _subprocess  # noqa: E402
import tempfile as _tempfile  # noqa: E402
import wave as _wave  # noqa: E402

from mlo.autotag import album_release_tags, mb_track_tags, write_mb_tags  # noqa: E402
from mlo.audio import AudioFile  # noqa: E402

CFG_EN = {"locale": "en"}

_ARTIST_ALIASES = [
    {"name": "Cubic U", "locale": "en", "primary": False, "type": "Artist name"},
    {"name": "Hikaru Utada", "locale": "en", "primary": True, "type": "Artist name"},
    {"name": "Utada Hikaru", "locale": "en_PH", "primary": True, "type": "Artist name"},
    {"name": "宇多田ヒカル", "locale": "ja", "primary": True, "type": "Artist name"},
    {"name": "ヒッキー", "locale": "ja", "primary": False, "type": "Artist name"},
    {"name": "Utada", "locale": None, "primary": None, "type": "Search hint"},
]
_TITLE_ALIASES = [
    {"name": "Hikari", "locale": "en", "primary": True, "type": "Recording name"},
    {"name": "Hikari (English Version)", "locale": "en", "primary": False,
     "type": "Recording name"},
    {"name": "光", "locale": "ja", "primary": True, "type": "Recording name"},
    {"name": "Hikari (romanized)", "locale": "en-Latn", "primary": False,
     "type": "Recording name"},
]
_RELEASE = {
    "id": "rel-1", "title": "First Love", "release_group_id": "rg-1",
    "album_artist_mbid": "art-1",
    # the release stated an English name, its GROUP a Japanese one
    "aliases": [{"name": "First Love (English)", "locale": "en", "primary": False,
                 "type": "Release name"}],
    "release_group_aliases": [{"name": "ファーストラブ", "locale": "ja",
                               "primary": True, "type": "Release group name"}],
    "artists": [{"name": "宇多田ヒカル", "mbid": "art-1",
                 "aliases": _ARTIST_ALIASES}],
    "artist_aliases": _ARTIST_ALIASES,
    "label": "EMI", "catalog_number": "TOCT-24000", "country": "JP",
    "countries": ["JP"], "status": "Official", "release_type": "Album",
    "date": "1999-03-10", "originaldate": "1999-03-10", "medium": "CD",
    "language": "jpn", "script": "Jpan", "barcode": "4988006175999",
    "asin": "", "license": "", "medium_titles": {1: ""},
    "tracks": {},
}
_SLOT = {"recording_mbid": "rec-1", "artist_mbid": "art-1", "title": "光",
         "artist_name": "宇多田ヒカル", "release_track_mbid": "rt-1",
         "isrcs": [], "aliases": _TITLE_ALIASES,
         "artist_aliases": _ARTIST_ALIASES, "credits": {}}


_ALBUM_VALUES = dict(album_release_tags(_RELEASE, disc=1, config=CFG_EN))
# The album's own title is "First Love" — Latin, which an `en` reader reads —
# so it needs NO alias (issue #75): "First Love (English)" annotates a name the
# reader already has. The artist IS Japanese, so exactly one ARTISTALIAS lands.
ok("ALBUMALIAS" not in _ALBUM_VALUES,
   f"a Latin album title gets no ALBUMALIAS in an `en` library "
   f"({_ALBUM_VALUES.get('ALBUMALIAS')!r})")
ok(_ALBUM_VALUES.get("ARTISTALIAS") == "Hikaru Utada",
   f"ARTISTALIAS is the ladder's pick for the non-Latin artist "
   f"({_ALBUM_VALUES.get('ARTISTALIAS')!r})")
ok(not [t for t in _ALBUM_VALUES if t.startswith("ARTISTALIAS-")],
   "…and nothing is spelled per-locale (no ARTISTALIAS-EN / -EN_PH / -JA) "
   f"({sorted(_ALBUM_VALUES)})")
ok(not any(str(v) == "Utada" for v in _ALBUM_VALUES.values()),
   "a `search hint` alias never reaches a tag")

# The SAME release under a JAPANESE title: now the album name is one an `en`
# reader cannot read, so it gets its alias — the ladder's pick, ONE value, and
# still no locale-suffixed tag of any kind.
_JA_RELEASE = dict(_RELEASE, title="ファーストラブ")
_JA_VALUES = dict(album_release_tags(_JA_RELEASE, disc=1, config=CFG_EN))
ok(_JA_VALUES.get("ALBUMALIAS") == "First Love (English)",
   f"a non-Latin album title gets its alias ({_JA_VALUES.get('ALBUMALIAS')!r})")
ok(not [t for t in _JA_VALUES if t.startswith("ALBUMALIAS-")],
   f"…one value, no per-locale fan-out ({sorted(_JA_VALUES)})")
# A release that states NO alias of its own falls back to its GROUP's, which is
# where a translated album name usually lives — the merging is unchanged, only
# what is written from it is.
_RG_ONLY = dict(_RELEASE, title="ファーストラブ",
                release_group_aliases=[{"name": "First Love", "locale": "en",
                                        "primary": True,
                                        "type": "Release group name"}])
_RG_ONLY.pop("aliases")
_RG_VALUES = dict(album_release_tags(_RG_ONLY, disc=1, config=CFG_EN))
ok(_RG_VALUES.get("ALBUMALIAS") == "First Love",
   f"the release GROUP's aliases are still the ALBUMALIAS source "
   f"({_RG_VALUES.get('ALBUMALIAS')!r})")

_TRACK_VALUES = dict(mb_track_tags(_RELEASE, _SLOT, disc=1,
                                   album_artist_mbid="art-1", config=CFG_EN))
ok(_TRACK_VALUES.get("TITLEALIAS") == "Hikari",
   f"the track's own alias is the ladder's pick ({_TRACK_VALUES.get('TITLEALIAS')!r})")
ok(not [t for t in _TRACK_VALUES
        if t.startswith(("TITLEALIAS-", "ARTISTALIAS-", "ALBUMALIAS-"))],
   f"…with no locale-suffixed tag beside it ({sorted(_TRACK_VALUES)})")
# A ja reader reads both names already — a Japanese title and a Japanese
# artist — and the Latin album title never needs one: no alias tag at all.
_JA_TRACK = dict(mb_track_tags(_RELEASE, _SLOT, disc=1,
                               album_artist_mbid="art-1", config={"locale": "ja"}))
ok(not [t for t in _JA_TRACK if "ALIAS" in t],
   "a ja reader is charged no alias for names they already read "
   f"({sorted(t for t in _JA_TRACK if 'ALIAS' in t)})")
# A release that states no aliases at all writes nothing — no empty tags.
_BARE = dict(_RELEASE, aliases=[], release_group_aliases=[], artist_aliases=[],
             artists=[{"name": "宇多田ヒカル", "mbid": "art-1"}])
ok(not [t for t, _v in album_release_tags(_BARE, disc=1, config=CFG_EN)
        if "ALIAS" in t],
   "a release with no aliases produces no alias tag")


def _flac_dir():
    """A vendored flac.exe, or None (the round trip is then skipped)."""
    for cand in _glob.glob(os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            ".dependencies", "*", "flac.exe")):
        if os.path.isfile(cand):
            return cand
    return _shutil.which("flac")


_FLAC = _flac_dir()
if not _FLAC:
    print("  skipped: no flac.exe (the on-disk round trip needs one)")
else:
    _TMP = _tempfile.mkdtemp(prefix="mlo-alias-tags-")
    _artist_dir = os.path.join(_TMP, "宇多田ヒカル")
    _album_dir = os.path.join(_artist_dir, "First Love")
    os.makedirs(_album_dir)
    _wav = os.path.join(_TMP, "tone.wav")
    with _wave.open(_wav, "w") as _w:
        _w.setnchannels(1); _w.setsampwidth(2); _w.setframerate(44100)
        _w.writeframes(b"\x00\x00" * 44100)
    _path = os.path.join(_album_dir, "01 - 光.flac")
    _subprocess.run([_FLAC, "-f", "-s", "-o", _path, _wav], check=True,
                    capture_output=True)
    _af = AudioFile(_path)
    for _k, _v in (("TITLE", "光"), ("ARTIST", "宇多田ヒカル"),
                   ("ALBUMARTIST", "宇多田ヒカル"), ("ALBUM", "ファーストラブ"),
                   ("TRACKNUMBER", "1")):
        _af.set_tag(_k, _v)
    # the REAL write path, from the stubbed release payload (whose title is the
    # Japanese one this file carries, so ALBUMALIAS has something to annotate)
    _af.defer_save(True)
    _written, _refused = write_mb_tags(
        _af, mb_track_tags(_JA_RELEASE, _SLOT, disc=1, album_artist_mbid="art-1",
                           config=CFG_EN), CFG_EN)
    _af.defer_save(False)
    ok(_written >= 3 and not _refused,
       f"the alias tags land through the real writer ({_written}, {_refused})")
    _back = AudioFile(_path)
    ok(_back.get_tag("TITLEALIAS") == "Hikari",
       f"TITLEALIAS reads back ({_back.get_tag('TITLEALIAS')!r})")
    ok(_back.get_tag("ARTISTALIAS") == "Hikaru Utada",
       f"ARTISTALIAS reads back ({_back.get_tag('ARTISTALIAS')!r})")
    ok(_back.get_tag("ALBUMALIAS") == "First Love (English)",
       f"ALBUMALIAS reads back ({_back.get_tag('ALBUMALIAS')!r})")
    ok(not [t for t in _back.all_tags()
            if str(t).upper().rsplit(":", 1)[-1].startswith(
                ("TITLEALIAS-", "ARTISTALIAS-", "ALBUMALIAS-"))],
       f"…and NOTHING is spelled per-locale ({sorted(_back.all_tags())})")

    # The IMPORT's own stamp (`server.imports._stamp_release_identity` →
    # `mlo.autotag.fill_release_identity`) is the SAME writer: an import that
    # holds the release stamps the album-level pair at once, with no script 8
    # run and no manual pass. It writes ONLY the album-level half — the
    # per-track alias belongs to the tagging stage.
    from mlo.autotag import fill_release_identity  # noqa: E402
    _path2 = os.path.join(_album_dir, "02 - 光2.flac")
    _subprocess.run([_FLAC, "-f", "-s", "-o", _path2, _wav], check=True,
                    capture_output=True)
    _af2 = AudioFile(_path2)
    for _k, _v in (("TITLE", "光"), ("ARTIST", "宇多田ヒカル"),
                   ("ALBUMARTIST", "宇多田ヒカル"), ("ALBUM", "ファーストラブ"),
                   ("TRACKNUMBER", "2")):
        _af2.set_tag(_k, _v)
    _res2 = fill_release_identity([_path2], _JA_RELEASE, CFG_EN)
    _back2 = AudioFile(_path2)
    ok(_res2["written"] == 1 and _res2["failed"] == 0,
       f"the importer's stamp writes the album-level tags ({_res2})")
    ok(_back2.get_tag("ALBUMALIAS") == "First Love (English)"
       and _back2.get_tag("ARTISTALIAS") == "Hikaru Utada",
       f"…the alias the album and its artist need ({_back2.get_tag('ALBUMALIAS')!r} / "
       f"{_back2.get_tag('ARTISTALIAS')!r})")
    ok(_back2.get_tag("TITLEALIAS") is None and not [
        t for t in _back2.all_tags()
        if str(t).upper().rsplit(":", 1)[-1].startswith("TITLEALIAS-")],
       "…and nothing of the per-track half, which is the tagging stage's")

print("== the library reads them: display and search ==")
from server import api_query as _api_query  # noqa: E402
from server import library as _lib  # noqa: E402

ok({"TITLEALIAS", "ARTISTALIAS", "ALBUMALIAS"} <= set(_lib.TRACK_TAGS),
   "the library payload reads every alias tag")
ok({"ARTISTALIAS", "ALBUMALIAS"} <= set(_lib.ALBUM_LEVEL_TAGS),
   "…the album-level ones too")
# The disambiguation family is read the same way: the recording's per track,
# the release group's and the credited artist's as album-level values.
ok({"TITLEDISAMBIGUATION", "ALBUMDISAMBIGUATION", "ARTISTDISAMBIGUATION"}
   <= set(_lib.TRACK_TAGS),
   "the library payload reads every disambiguation tag")
ok({"ALBUMDISAMBIGUATION", "ARTISTDISAMBIGUATION"} <= set(_lib.ALBUM_LEVEL_TAGS),
   "…including the two album-level ones")
_fields = {f["field"]: f for g in _api_query.catalogue()["groups"]
           for f in g["fields"]}
ok(_fields.get("tags.TITLEALIAS", {}).get("in_payload") is True,
   "the query catalogue offers tags.TITLEALIAS over the payload that carries it")
ok(_api_query._known("tags.TITLEALIAS") and _api_query._known("tags.ALBUMALIAS"),
   "…so a query may name it (and the bare name)")

if _FLAC:
    _res = _lib.build_album(_album_dir, {"music_folder": _TMP,
                                         "lyrics_format": "EMBEDDED",
                                         "worker_limit": 2})
    _tr = _res["tracks"][0]
    ok(_tr.get("alias") == "Hikari",
       f"the track row carries its alias beside tags.TITLE ({_tr.get('alias')!r})")
    ok(_tr["tags"].get("TITLE") == "光",
       "…while the original title stays the tag it is (the details)")
    ok(_res.get("alias") == "First Love (English)",
       f"the album row carries ALBUMALIAS beside meta.ALBUM ({_res.get('alias')!r})")
    ok(_res["meta"].get("ALBUM") == "ファーストラブ",
       "…and the original album name stays visible")
    # The library's search blob IS every value of tags (the client's haystack,
    # LibraryPage's `Object.values(t.tags)`), so the alias reaches the search.
    _hay = " ".join(str(v) for v in _tr["tags"].values()).lower()
    ok("hikari" in _hay,
       "the library's search blob holds the alias, so a track is findable by it")
    # …and the QUERY ENGINE answers by the alias too (mlo.query: the same
    # engine the browser's condition builder, the saved smart playlists and
    # `POST /api/library/query` run, over the payload TRACK_TAGS feeds).
    from mlo import query as _query  # noqa: E402
    _payload = {"artists": [{"albums": [{"path": _album_dir, "tracks": [_tr]}]}]}
    _hit = _query.run(
        _payload,
        {"conditions": [{"field": "tags.TITLEALIAS", "op": "contains",
                         "value": "hikari"}], "limit": 10},
        bool_fields=_api_query.bool_fields())["items"]
    ok([i["path"] for i in _hit] == [_tr["path"]],
       f"a library query finds the track by tags.TITLEALIAS ({_hit})")
    ok(_lib._artist_display_name(_artist_dir, [_res]) == "宇多田ヒカル (Hikaru Utada)",
       "the artist's display name shows the alias with the original "
       f"({_lib._artist_display_name(_artist_dir, [_res])!r})")

    # MusicBrainz's disambiguation comment for the ARTIST — the same fact the
    # album payload surfaces as `artist_disambiguation` — reaches the artist
    # payload and the library's artist rows, and is None (never "") when no
    # file states one. Written through the real tag writer, read back through
    # the real builders (no network: both read the albums they already hold).
    from server import main as _main  # noqa: E402
    _cfg = {"music_folder": _TMP, "lyrics_format": "EMBEDDED",
            "worker_limit": 2}
    _marked = AudioFile(_path)
    _marked.set_tag("ARTISTDISAMBIGUATION", "UK rock band")
    _artist_payload = _main._artist_payload(_artist_dir, _cfg)
    ok(_artist_payload.get("disambiguation") == "UK rock band",
       f"the artist payload carries ARTISTDISAMBIGUATION "
       f"({_artist_payload.get('disambiguation')!r})")
    _art_row = next((a for a in _lib.build_library(_cfg)["artists"]
                     if a["path"].replace("\\", "/").endswith("宇多田ヒカル")),
                    {})
    ok(_art_row.get("disambiguation") == "UK rock band",
       f"…and so does the library's artist row "
       f"({_art_row.get('disambiguation')!r})")
    for _f in (_path, _path2):
        AudioFile(_f).delete_tag("ARTISTDISAMBIGUATION")
    _bare_artist = _main._artist_payload(_artist_dir, _cfg)
    ok(_bare_artist.get("disambiguation") is None,
       "an artist whose files state none surfaces None, not \"\" "
       f"({_bare_artist.get('disambiguation')!r})")
    _shutil.rmtree(_TMP, ignore_errors=True)

print(f"mb aliases: all {passed} assertions passed")
