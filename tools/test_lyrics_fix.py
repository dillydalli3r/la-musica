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

# Automatic writes need a CONFIDENT match; manual search keeps the loose floor.
from mlo import lyrics_providers as lp  # noqa: E402

_orig = lp._PROVIDERS["lrclib"]
# title exact, artist unknown to the provider → 0.65 + 0.35*0.4 = 0.79
lp._PROVIDERS["lrclib"] = (
    lambda artist, title, album, duration, cfg, yt:
    lp._hit("[00:01.00]Hello", "Hello", None, title, None, None))
try:
    loose = lp.fetch_lyrics({"lyrics_sources": ["lrclib"]}, "Artist", "Song")
    assert loose and loose["score"] == 0.79, loose
    assert lp.fetch_lyrics({"lyrics_sources": ["lrclib"]}, "Artist", "Song",
                           min_score=0.85) is None
finally:
    lp._PROVIDERS["lrclib"] = _orig

print("ALL LYRICS TESTS PASSED")
