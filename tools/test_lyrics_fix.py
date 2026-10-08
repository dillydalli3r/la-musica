# Quick verification of the new lyrics normalizer (user's exact sample).
# Adjusted for the merged normalizer: the v1.1.0-only helpers
# (lyrics_are_formatted, _expand_lrc_line) are gone - idempotency is
# asserted directly via format_lyrics_text(x) == x, and only lines that
# START with a timestamp get split (remote _split_merged_ts semantics).
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from mlo.lyrics import format_lyrics_text

SAMPLE = """[00:00.00][00:45.53]Stretching, filing[00:46.86]Against her skin
[00:48.16]Blessed are those
[00:49.45]Who are not kin
[00:50.67]In sin we breathe
[00:51.92]In sex we tie
[00:53.21]Duct tape her legs
[00:54.55]To the red sky
[00:55.76]Foolsome flesh allowances
[00:58.02]The pansies raided the pantry of
[01:00.78]Gabardine dreams, promiscuous
[01:03.36]Delight, deny not the flavor
[01:06.00]Custard dreams
[01:07.86]Abusing, musing
[01:10.81]Marmalade flesh
[01:13.71]Naked spread am I
[01:20.96]Am I
[01:25.98]Actors of the tragic fanthom
[01:28.58]Extend your legs for great saturn
[01:31.17]Brown table tops scream for cover
[01:33.64]At the sight of your new lover
[01:36.23]If today i die
[01:39.27]And can't deny
[01:41.82]The poison chosen
[01:44.93]For tonight
[01:50.89]Tonight
[02:25.52]Borrowed dreams
[02:26.37]Hollowed reveries
[02:27.98]Metal pillows
[02:29.25]Pewter yellows
[02:30.50]Furry roadkill
[02:31.71]House on the hill
[02:33.01]Pouring gravy
[02:34.22]On her thighs still
[02:35.41]If today i die
[02:38.46]And can't deny
[02:40.94]The poison chosen
[02:44.02]For tonight
[02:49.93]Tonight
[02:53.18]"""

out = format_lyrics_text(SAMPLE, lrc_extended_enabled=False, lrc_add_zero_timestamp=False)
print(out)
print("=" * 60)
print("first lines check:")
for ln in out.split("\n")[:4]:
    print(repr(ln))
assert out.split("\n")[0] == "[00:45.53]Stretching, filing", out.split("\n")[0]
assert out.split("\n")[1] == "[00:46.86]Against her skin"
assert "[02:53.18]" not in out, "trailing ts-only line must be dropped"
assert format_lyrics_text(out, lrc_extended_enabled=False, lrc_add_zero_timestamp=False) == out, "must be idempotent"
assert format_lyrics_text(SAMPLE, lrc_extended_enabled=False, lrc_add_zero_timestamp=False) != SAMPLE, "sample is malformed"

# additional cases
cases = {
    # repeat markers for a chorus
    "[00:20.00][01:20.00][02:20.00]Chorus line":
        "[00:20.00]Chorus line\n[01:20.00]Chorus line\n[02:20.00]Chorus line",
    # timestamp normalization (3-digit cs, 1-digit fields)
    "[0:5.500]x": "[00:05.50]x",
    # space after ts
    "[00:05.50]  hello": "[00:05.50]hello",
    # metadata lines dropped
    "[ar:Artist]\n[00:01.00]x": "[00:01.00]x",
    # plain text untouched
    "just a verse\n\nanother": "just a verse\n\nanother",
    # mid-line after text, spaces
    "[00:01.00]one two [00:02.00]three": "[00:01.00]one two\n[00:02.00]three",
    # blank collapse + trim
    "\n\n[00:01.00]x\n\n\n[00:02.00]y\n\n": "[00:01.00]x\n\n[00:02.00]y",
    # crlf
    "[00:01.00]x\r\n[00:02.00]y\r\n": "[00:01.00]x\n[00:02.00]y",
    # untimed text before a ts: remote semantics keep such a line whole
    # (only lines that START with a timestamp are split)
    "intro bit[00:10.00]timed": "intro bit[00:10.00]timed",
    # zero marker stacked is dropped, standalone zero-with-text kept
    "[00:00.00]first line": "[00:00.00]first line",
    # provider credit blocks are not lyrics: the line is replaced by a blank
    # one (trimmed here, it is the file's first line) and its stamp dies with
    # it — the first real lyric keeps its own time
    "[00:00.00] 作词 : Byrne, Eno, Talking Heads\n"
    "[00:00.00] Lyrics: Byrne, Eno, Talking Heads\n"
    "[00:12.00] Once in a lifetime": "[00:12.00]Once in a lifetime",
    # …and a real lyric AT the credit's stamp is kept, credits removed
    "[00:00.00]Lyrics by：Thom Yorke\n"
    "[00:00.00]Once in a lifetime\n"
    "[00:12.00]And the days go by":
        "[00:00.00]Once in a lifetime\n[00:12.00]And the days go by",
    # a file that held nothing but credits has no lyrics left
    "[00:00.00]作词 : X\n[00:00.00]作曲 : Y": "",
    # a lyric that merely mentions the words is a lyric
    "[00:01.00]and the lyrics by heart": "[00:01.00]and the lyrics by heart",
    "[00:01.00]Music: what a racket": "[00:01.00]Music: what a racket",
}
for src, want in cases.items():
    got = format_lyrics_text(src, lrc_extended_enabled=False, lrc_add_zero_timestamp=False)
    assert got == want, f"{src!r}: got {got!r}, want {want!r}"
    assert format_lyrics_text(got, lrc_extended_enabled=False, lrc_add_zero_timestamp=False) == got

# ---------------------------------------------------------------------------
# The owner's real track: an ID3 USLT content descriptor ("[id:$00000000]"),
# the descriptive LRC headers, and a leading credit line repeating the
# track's own identity. Script 1 drops all of them; the grader's format check
# shares this predicate (the grading section at the end).
# ---------------------------------------------------------------------------
from mlo.lyrics import has_lyrics_text  # noqa: E402

TITLE = "Evil Death Roll"
ARTIST = "King Gizzard & The Lizard Wizard"
DIRTY = (
    "[id:$00000000]\n"
    "[ti:Evil Death Roll]\n"
    "[ar:King Gizzard & The Lizard Wizard]\n"
    "[al:Nonagon Infinity]\n"
    "[au:King Gizzard]\n"
    "[by:Stu Mackenzie]\n"
    "[re:Live]\n"
    "[ve:1]\n"
    "[length:03:24]\n"
    "Evil Death Roll - King Gizzard & The Lizard Wizard\n"
    "[00:12.00]Nonagon\n"
    "[00:15.00]Infinity"
)
CLEAN = "[00:12.00]Nonagon\n[00:15.00]Infinity"
IDENT = dict(track_title=TITLE, track_artist=ARTIST,
             lrc_extended_enabled=False, lrc_add_zero_timestamp=False)

got = format_lyrics_text(DIRTY, **IDENT)
assert got == CLEAN, f"dirty block: got {got!r}"
assert format_lyrics_text(got, **IDENT) == got, "cleaned block is idempotent"
assert DIRTY != CLEAN, "the fixture really is dirty"

# Every header family goes, in any case and with an empty value; the leading
# identity line goes with them.
for junk in ("[ID:$00000000]", "[id:]", "[Id:  abc 123 ]", "[la:eng]",
             "[ti:]", "[al:]", "[au:]"):
    got = format_lyrics_text(f"{junk}\n[00:01.00]words", **IDENT)
    assert got == "[00:01.00]words", f"header {junk!r}: got {got!r}"

# Both credit orders, dash variants and case/spacing are the same credit.
for credit in (f"{TITLE} - {ARTIST}", f"{ARTIST} - {TITLE}",
               f"{TITLE}\u2013{ARTIST}", f"  {TITLE.lower()} - {ARTIST.upper()} "):
    got = format_lyrics_text(f"{credit}\n[00:01.00]words", **IDENT)
    assert got == "[00:01.00]words", f"identity {credit!r}: got {got!r}"

# An unrelated prose line is NOT eaten...
got = format_lyrics_text("The sky was the colour of television\n[00:01.00]words", **IDENT)
assert got == "The sky was the colour of television\n[00:01.00]words", got
# ... nor is a matching line once the words have already started...
got = format_lyrics_text(f"[00:01.00]words\n{TITLE} - {ARTIST}", **IDENT)
assert got == f"[00:01.00]words\n{TITLE} - {ARTIST}", got
# ... nor is the shape guessed when the file states no tags.
got = format_lyrics_text(f"{TITLE} - {ARTIST}\n[00:01.00]words",
                         lrc_extended_enabled=False, lrc_add_zero_timestamp=False)
assert got == f"{TITLE} - {ARTIST}\n[00:01.00]words", got

# `[offset:…]` is an INSTRUCTION a player applies, not a header: it survives
# formatting (and a file that holds only one still has no lyrics).
got = format_lyrics_text("[offset:-250]\n[00:01.00]words",
                         lrc_extended_enabled=False, lrc_add_zero_timestamp=False)
assert got == "[offset:-250]\n[00:01.00]words", got
assert format_lyrics_text(got, lrc_extended_enabled=False,
                          lrc_add_zero_timestamp=False) == got
assert not has_lyrics_text("[offset:-250]"), "an offset alone is not lyrics"
assert not has_lyrics_text("[id:$00000000]"), "a frame descriptor is not lyrics"

# Automatic writes need a CONFIDENT match; manual search keeps the loose floor.
from mlo import lyrics_providers as lp  # noqa: E402

_orig = lp._PROVIDERS["lrclib"]
# title exact, artist unknown to the provider → 0.65 + 0.35*0.4 = 0.79
lp._PROVIDERS["lrclib"] = (
    lambda artist, title, album, duration, cfg:
    lp._hit("[00:01.00]Hello", "Hello", None, title, None, None))
try:
    loose = lp.fetch_lyrics({"lyrics_sources": ["lrclib"]}, "Artist", "Song")
    assert loose and loose["score"] == 0.79, loose
    assert lp.fetch_lyrics({"lyrics_sources": ["lrclib"]}, "Artist", "Song",
                           min_score=0.85) is None
finally:
    lp._PROVIDERS["lrclib"] = _orig

# ---------------------------------------------------------------------------
# The grade agrees with the strip: a stored lyric that still carries the
# `[id:…]`, the descriptive headers or the identity credit line FAILS the
# canonical-format check — the check and the strip share one predicate — and
# script 1's own pass clears exactly what the check flags.
# ---------------------------------------------------------------------------
import shutil, subprocess, tempfile, wave  # noqa: E402
from mlo.audio import AudioFile  # noqa: E402
from mlo.config import DEFAULT_CONFIG  # noqa: E402
from mlo.grader import _grade_album  # noqa: E402
from mlo.lyrics import _process_lyrics_for_audio  # noqa: E402

FLAC_EXE = None
_deps = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     ".dependencies")
if os.path.isdir(_deps):
    for entry in os.listdir(_deps):
        if entry.lower().startswith("flac"):
            cand = os.path.join(_deps, entry, "flac.exe")
            if os.path.isfile(cand):
                FLAC_EXE = cand
                break
assert FLAC_EXE, "flac.exe not found under .dependencies"


def _make_flac(path):
    wav = path + ".wav"
    with wave.open(wav, "w") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(44100)
        w.writeframes(b"\x00\x00\x00\x00" * 4410)
    subprocess.run([FLAC_EXE, "-s", "-f", "-8", "-o", path, wav],
                   check=True, capture_output=True)
    os.remove(wav)


# Everything off except the canonical-format check this section exercises: a
# grade of one fixture must not be a verdict on the dozen other things a bare
# album misses (the same isolation test_tag_hygiene.py uses).
ISO = {k: False for k in DEFAULT_CONFIG if str(k).startswith("grade_check_")}
ISO.update({
    "music_folder": "",
    "grade_include_music": True,
    "grade_check_lyrics_format": True,
    "grade_check_lyrics_spaces": True,
    "grade_check_lyrics_blank_lines": True,
})

tmp = tempfile.mkdtemp(prefix="mlo_lyr_fix_")
try:
    track = os.path.join(tmp, "Album", "01 - Evil Death Roll.flac")
    os.makedirs(os.path.dirname(track), exist_ok=True)
    _make_flac(track)
    af = AudioFile(track)
    af.set_tag("TITLE", TITLE)
    af.set_tag("ARTIST", ARTIST)
    af.set_tag("INSTRUMENTAL", "0")
    af.set_lyrics(DIRTY)
    album = os.path.dirname(track)

    dirty = _grade_album(album, "EMBEDDED", ISO)
    assert any(i.startswith("Lyrics not optimally formatted") for i in dirty["issues"]), \
        f"the dirty lyric fails the format check ({dirty['issues']})"
    assert "LYRICS" in dirty["tracks"][0]["issues"], dirty["tracks"][0]["issues"]

    # Script 1's own pass clears what the check flags (one predicate).
    status, _, _, note = _process_lyrics_for_audio(
        track, dict(DEFAULT_CONFIG, music_folder=""))
    after = AudioFile(track).get_lyrics()
    assert after == CLEAN, \
        f"script 1 cleared the stray lines (got {after!r}, {status}/{note})"

    clean = _grade_album(album, "EMBEDDED", ISO)
    assert not any(i.startswith("Lyrics not optimally formatted") for i in clean["issues"]), \
        f"the cleaned lyric passes ({clean['issues']})"
    assert clean["pass_count"] - dirty["pass_count"] == 1 \
        and clean["total_checks"] == dirty["total_checks"], \
        f"the stray lines cost exactly one check " \
        f"({dirty['pass_count']}/{dirty['total_checks']} -> " \
        f"{clean['pass_count']}/{clean['total_checks']})"
finally:
    shutil.rmtree(tmp, ignore_errors=True)

# ---------------------------------------------------------------------------
# The same agreement for a track the file itself calls instrumental: a
# leftover LYRICS tag and a stale empty .lrc are cleared by script 1 (the
# file's own INSTRUMENTAL=1 wins over the tag), and the lyrics checks do not
# report them while the script would clear them.
# ---------------------------------------------------------------------------
LEFTOVER = "untimed leftover words"


def _inst_fixture(root, inst):
    os.makedirs(os.path.join(root, "Album"), exist_ok=True)
    track = os.path.join(root, "Album", "01 - Track.flac")
    _make_flac(track)
    af = AudioFile(track)
    af.set_tag("TITLE", "Track")
    af.set_tag("ARTIST", "Artist")
    af.set_tag("INSTRUMENTAL", inst)
    af.set_lyrics(LEFTOVER)
    lrc = os.path.splitext(track)[0] + ".lrc"
    with open(lrc, "w", encoding="utf-8"):
        pass  # the 0-byte sidecar an aborted run leaves behind
    return os.path.dirname(track), track, lrc


def _lyrics_tag_keys(path):
    return {str(k).upper().rsplit(":", 1)[-1]
            for k in (AudioFile(path).all_tags() or {})}


# INSTRUMENTAL=1: the leftover is a contradiction the script clears, so the
# grade must not charge the track for it either before or after the script.
tmp_i = tempfile.mkdtemp(prefix="mlo_lyr_inst_")
try:
    album, track, lrc = _inst_fixture(tmp_i, "1")
    assert "LYRICS" in _lyrics_tag_keys(track), "the fixture carries the leftover tag"
    assert os.path.exists(lrc), "the fixture carries the stale sidecar"

    before = _grade_album(album, "EMBEDDED", ISO)
    assert before["total_checks"] == 0 and before["pass_count"] == 0, \
        f"an instrumental is charged nothing for leftover lyrics " \
        f"({before['pass_count']}/{before['total_checks']}, {before['issues']})"
    assert not before["tracks"][0]["issues"], before["tracks"][0]["issues"]

    status, _, _, note = _process_lyrics_for_audio(
        track, dict(DEFAULT_CONFIG, music_folder=""))
    assert "LYRICS" not in _lyrics_tag_keys(track), \
        f"script 1 removed the leftover LYRICS tag ({status}/{note})"
    assert not os.path.exists(lrc), "script 1 removed the stale .lrc"
    assert str(AudioFile(track).get_tag("INSTRUMENTAL")).strip() == "1", \
        "the file's own INSTRUMENTAL=1 still wins"

    after = _grade_album(album, "EMBEDDED", ISO)
    assert after["total_checks"] == 0 and after["pass_count"] == 0, \
        f"still nothing to report ({after['issues']})"
finally:
    shutil.rmtree(tmp_i, ignore_errors=True)

# INSTRUMENTAL=0: the very same leftover is a lyrics problem, script 1 leaves
# it alone, and the format check keeps failing it — now saying WHY: the leftover
# is untimed text, which no formatter can add timing to (the message used to
# name script 1, a script this very case proves leaves the file alone).
tmp_z = tempfile.mkdtemp(prefix="mlo_lyr_noninst_")
try:
    album, track, lrc = _inst_fixture(tmp_z, "0")
    res = _grade_album(album, "EMBEDDED", ISO)
    assert any(i.startswith("Lyrics cannot be repaired by a script")
               and "no timestamps" in i for i in res["issues"]), \
        f"a non-instrumental track with the untimed leftover fails, and the message says why ({res['issues']})"

    _process_lyrics_for_audio(track, dict(DEFAULT_CONFIG, music_folder=""))
    assert str(AudioFile(track).get_lyrics() or "").strip(), \
        "script 1 must NOT clear lyrics on a non-instrumental track"
finally:
    shutil.rmtree(tmp_z, ignore_errors=True)

print("ALL LYRICS TESTS PASSED")
