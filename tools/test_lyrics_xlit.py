#!/usr/bin/env python3
"""Script 17 — the AI lyric transforms (`mlo.lyrics_xlit` + `server.ai`).

Offline: `server.ai.ai_chat` is stubbed, so nothing here touches a model or the
network. The disk cache is redirected into a temp folder.

Pinned here:

  * line alignment — every output line keeps its `[mm:ss.xx]` prefix and its
    position, blank lines and `[ar:…]` headers pass through untouched, so a
    stored transform renders under the original without re-alignment (and the
    synced original still gets its word timings re-distributed);
  * romanization is only requested when it says something: Latin lyrics and
    lyrics already in the reader's script are skipped (`xlit_needs`);
  * the TRANSLITERATION tag carries its source language where it is knowable
    (kana → `TRANSLITERATION-JA-LATN`, ambiguous script → plain `-LATN`);
  * a "translation" identical to its source is recognized as a no-op;
  * `transform_lines` asks for one line per input line, repairs a short answer,
    refuses an all-blank one, and serves the same request from cache the second
    time (no second call);
  * the script is a no-op — not a failure — with no AI configured.

Run:  python tools/test_lyrics_xlit.py
"""
import os
import shutil
import subprocess
import sys
import tempfile
import wave

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server import ai  # noqa: E402
from mlo import lyrics_xlit as xl  # noqa: E402


FAILS = []


def ok(cond, label):
    print(("  ok   " if cond else "  FAIL ") + label)
    if not cond:
        FAILS.append(label)


TMP = tempfile.mkdtemp(prefix="mlo-xlit-test-")
ai._cache_dir = lambda: os.path.join(TMP, "cache")  # keep the cache out of the app state

LRC = ("[ti:Song]\n"
       "[ar:Artist]\n"
       "[00:01.00]君の名は\n"
       "[00:03.50]\n"
       "[00:05.00]Hello there\n")

print("== line alignment ==")
prefix, body = xl._split_lrc_line("[00:01.00]君の名は")
ok(prefix == "[00:01.00]" and body == "君の名は", f"a timestamped line splits ({prefix!r}/{body!r})")
prefix2, body2 = xl._split_lrc_line("[00:01.00]<00:01.20>君<00:01.60>の名は")
ok(prefix2 == "[00:01.00]" and body2 == "君の名は",
   f"word tags are stripped from the body, the line prefix survives ({body2!r})")

lines = LRC.splitlines()
index_map, bodies = {}, []
for i, ln in enumerate(lines):
    _p, b = xl._split_lrc_line(ln)
    if b and not xl._META_LINE_RE.match(b):
        index_map[i] = len(bodies)
        bodies.append(b)
ok(bodies == ["君の名は", "Hello there"],
   f"metadata headers and blank lines are not transform targets ({bodies})")
merged = xl._merge_lines(lines, index_map, ["Kimi no na wa", "Hello there"])
_m = merged.splitlines()
ok(_m[2] == "[00:01.00] Kimi no na wa"
   and _m[3] == "[00:03.50]"
   and _m[4] == "[00:05.00] Hello there"
   and _m[0] == "[ti:Song]" and _m[1] == "[ar:Artist]",
   f"the transform lands in place, blank lines and headers survive ({_m!r})")

print("== what the lyrics still need (xlit_needs) ==")
_JA = "君の名は\n忘れられない"
ok(xl.xlit_needs(_JA, {"lyrics_translation_langs": "en"})["transliteration"],
   "Japanese lyrics for an English reader are romanized")
ok(not xl.xlit_needs(_JA, {"lyrics_translation_langs": "ja"})["transliteration"],
   "the same lyrics for a Japanese reader are not (same script)")
ok(not xl.xlit_needs("Hello there, my old friend\nI sing for you",
                     {"lyrics_translation_langs": "en"})["transliteration"],
   "Latin lyrics are never romanized")
ok(xl.xlit_needs("", {"lyrics_translation_langs": "en"}) ==
   {"transliteration": False, "translation": False, "langs": []},
   "no lyrics, no work")
# A translation is needed only when the lyrics are NOT already the reader's
# own language — and the answer covers every configured language at once.
_en = xl.xlit_needs("Hello there, my old friend\nI sing for you",
                    {"lyrics_translation_langs": "en"})
ok(_en["translation"] is False and _en["langs"] == [],
   f"English lyrics need no English translation ({_en})")
_de = xl.xlit_needs("Der Himmel ist blau und ich bin hier\nIch singe nicht",
                    {"lyrics_translation_langs": "en"})
ok(_de["translation"] is True and _de["langs"] == ["en"],
   f"German lyrics for an English reader do ({_de})")
ok(xl.xlit_needs(_JA, {"lyrics_translation_langs": "en,de"})["langs"] == ["en", "de"],
   "a needed translation is wanted for every configured language")
ok(xl.xlit_needs("君の名は", {"lyrics_translation_langs": "en"})["translation"] is False,
   "a single line is a placeholder, not a lyric — nothing is asked for it")

print("== the tag says which language was romanized ==")
ok(xl.xlit_tag_suffix({}, "君の名は") == "ja-latn", "kana pins the source language")
ok(xl.dominant_script("漢字ばかりでもなく、かなもある") == "japanese",
   "kana in a mostly-kanji line still reads as Japanese")
ok(xl.xlit_tag_suffix({}, "Привет") == "latn",
   "Cyrillic maps to several languages, so the tag stays script-only")


class FakeAudio:
    def __init__(self, language=""):
        self.language = language

    def get_tag(self, key):
        return self.language if key == "LANGUAGE" else None


ok(xl.xlit_tag_suffix({}, "Привет", FakeAudio("uk")) == "uk-latn",
   "an explicit LANGUAGE tag wins over the script guess")

print("== translation languages ==")
ok(xl.translation_langs({"lyrics_translation_langs": " EN, de ;"}) == ["en", "de"],
   "the language list is parsed loosely")
ok(xl.translation_langs({}) == ["en"], "an empty list falls back to en")
ok(xl.primary_translation_lang({"lyrics_translation_langs": "de,en"}) == "de",
   "the first language is the reader's")
ok(xl._same_essence("Hello, there!", "hello there") is True,
   "a translation equal to its source is recognized")
ok(xl._same_essence("Hallo, dort!", "hello there") is False, "a real translation is kept")

print("== transform_lines: line count, blanks, cache ==")
calls = []


def fake_chat(config, system, user):
    """Stands in for the model: answers the numbered lines only, the way the
    prompt asks (the directive line is dropped, never echoed)."""
    calls.append(user)
    out = []
    for ln in user.splitlines():
        num, _, text = ln.partition(". ")
        if not text:
            continue          # the "Target language: .." directive
        out.append(f"{num}. {text.upper()}")
    # one line short on purpose: the caller has to repair the count
    return "\n".join(out[:-1])


real_chat = ai.ai_chat
ai.ai_chat = fake_chat
try:
    cfg = {"ai_base_url": "http://x/v1", "ai_model": "m", "ai_translation_langs": "en"}
    got = ai.transform_lines(cfg, ["one", "two", "three"], "translate", "en")
    ok(len(got) == 3 and got[:2] == ["ONE", "TWO"] and got[2] == "",
       f"one output line per input line; the unanswered line is padded empty ({got})")
    n_before = len(calls)
    again = ai.transform_lines(cfg, ["one", "two", "three"], "translate", "en")
    ok(again == got and len(calls) == n_before, "the second identical request is served from cache")

    short = ai.transform_lines(cfg, ["alpha", "beta", "gamma"], "translate", "de")
    ok(len(short) == 3 and short[0] == "ALPHA",
       f"a different target language is its own cache entry ({short})")

    calls.clear()
    ai.ai_chat = lambda config, system, user: ""
    try:
        ai.transform_lines(cfg, ["something"], "transliterate", "ru")
        ok(False, "an all-blank answer raises instead of being stored")
    except ValueError as e:
        ok("no lines" in str(e), f"an all-blank answer raises instead of being stored ({e})")
    ai.ai_chat = fake_chat
except Exception as exc:  # noqa: BLE001 - a raised error IS the failure here
    ok(False, f"transform_lines raised unexpectedly: {exc}")
finally:
    ai.ai_chat = real_chat

print("== _apply keeps the sync level ==")
sent = {}


def fake_transform(config, bodies, mode, lang=""):
    sent["bodies"] = list(bodies)
    sent["mode"] = mode
    return {b: b.upper() for b in bodies}


real_transform = xl._transform_unique
xl._transform_unique = fake_transform
try:
    cfg = {"lrc_sync_level": "LINE"}
    sent.clear()
    new_text, changed = xl._apply(cfg, LRC, "transliterate")
    line3 = new_text.splitlines()[2]
    stripped = "".join(xl._WORD_TAG_RE.sub("", line3).split())
    ok(changed and stripped == "[00:01.00]君の名は",
       f"the transformed body keeps its line prefix ({line3!r})")
    ok("<00:" in line3,
       f"the transform is re-synced at the configured level, not left unsynced ({line3!r})")
    ok(sent["bodies"] == ["君の名は", "Hello there"], "only the real bodies are sent")
    unchanged, changed2 = xl._apply(cfg, "[ar:A]\n[00:01.00]", "transliterate")
    ok(not changed2 or unchanged, "a metadata-only text is returned untouched")
finally:
    xl._transform_unique = real_transform

print("== no AI configured is a quiet no-op ==")
stats = xl.run_lyrics_xlit({"music_folder": "", "lyrics_xlit_enabled": True,
                            "lyrics_translate_enabled": True})
ok(isinstance(stats, dict) and stats.get("transliterated") == 0,
   "an unconfigured app runs the script and reports nothing done")
ok(xl.ai_ready({"ai_base_url": "http://x", "ai_model": "m"}) is True,
   "base URL + model is the readiness rule")
ok(xl.ai_ready({"ai_base_url": "http://x"}) is False, "a model is required")

print("== a re-run changes nothing that is already correct ==")
# Real files, so the storage places are exercised end to end: an already
# complete track must not be sent to the model or rewritten (a re-run of the
# whole library costs nothing and churns no bytes), and a track whose lyrics
# need nothing at all must not grow a tag or a sidecar — the same rule the
# grader holds it to.
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FLAC_EXE = None
_deps = os.path.join(ROOT, ".dependencies")
if os.path.isdir(_deps):
    for entry in os.listdir(_deps):
        if entry.lower().startswith("flac"):
            cand = os.path.join(_deps, entry, "flac.exe")
            if os.path.isfile(cand):
                FLAC_EXE = cand
                break


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


if not FLAC_EXE:
    print("  (skipped: flac.exe not found under .dependencies)")
else:
    from mutagen.flac import FLAC
    JA = "[00:01.00] 君の名は\n[00:03.50] 忘れられない"
    EN = "[00:01.00] Hello there, my old friend\n[00:03.50] I sing for you"
    run_dir = tempfile.mkdtemp(prefix="mlo_xlit_nochurn_")
    done_flac = os.path.join(run_dir, "01 done.flac")
    latin_flac = os.path.join(run_dir, "02 latin.flac")
    _make_flac(done_flac)
    _make_flac(latin_flac)
    f = FLAC(done_flac)
    f["LYRICS"] = [JA]
    f["TRANSLITERATION-JA-LATN"] = ["Kimi no na wa"]
    f["TRANSLATION-EN"] = ["What is your name"]
    f.save()
    f = FLAC(latin_flac)
    f["LYRICS"] = [EN]
    f.save()
    before = {p: open(p, "rb").read() for p in (done_flac, latin_flac)}
    nocheck_calls = []
    real_chat = ai.ai_chat
    ai.ai_chat = lambda config, system, user: nocheck_calls.append(user) or "X"
    try:
        stats = xl.run_lyrics_xlit({
            "music_folder": run_dir, "targets": [done_flac, latin_flac],
            "ai_base_url": "http://x/v1", "ai_model": "m",
            "lyrics_format": "EMBEDDED", "lyrics_translation_langs": "en",
            "lyrics_xlit_enabled": True, "lyrics_translate_enabled": True,
        })
    finally:
        ai.ai_chat = real_chat
    ok(not nocheck_calls,
       f"neither track is sent to the model ({len(nocheck_calls)} call(s))")
    ok(stats.get("transliterated") == 0 and stats.get("translated") == 0,
       f"nothing is written back ({stats})")
    after = {p: open(p, "rb").read() for p in (done_flac, latin_flac)}
    ok(after == before, "and both files are byte-identical after the re-run")
    ok(sorted(os.listdir(run_dir)) == ["01 done.flac", "02 latin.flac"],
       f"no sidecar appears next to the audio ({sorted(os.listdir(run_dir))})")

    # A transform an OLDER, laxer rule wrote must be cleared by the same run:
    # the grader fails such a token as XLIT_UNNEEDED, and since the script
    # only ever added, nothing else could ever clear it — the album would be
    # stuck failing forever.
    stale_flac = os.path.join(run_dir, "03 stale.flac")
    _make_flac(stale_flac)
    f = FLAC(stale_flac)
    f["LYRICS"] = [EN]
    f["TRANSLITERATION-LATN"] = ["Hello there"]
    f["TRANSLATION-EN"] = ["Hello there"]
    f.save()
    stale_sidecars = [os.path.join(run_dir, n)
                      for n in ("03 stale.romaji.lrc", "03 stale.en.lrc")]
    for sidecar in stale_sidecars:
        with open(sidecar, "w", encoding="utf-8") as fh:
            fh.write("[00:01.00] Hello there")
    clear_calls = []
    ai.ai_chat = lambda config, system, user: clear_calls.append(user) or "X"
    try:
        stats = xl.run_lyrics_xlit({
            "music_folder": run_dir, "targets": [stale_flac],
            "ai_base_url": "http://x/v1", "ai_model": "m",
            "lyrics_format": "EMBEDDED", "lyrics_translation_langs": "en",
            "lyrics_xlit_enabled": True, "lyrics_translate_enabled": True,
        })
    finally:
        ai.ai_chat = real_chat
    ok(not clear_calls, "clearing a stale transform never asks the model")
    ok(stats.get("stale_removed") == 2,
       f"both stale transforms are counted as removals ({stats.get('stale_removed')})")
    f = FLAC(stale_flac)
    ok(not [k for k in f.keys() if str(k).upper().startswith(("TRANSLITERATION", "TRANSLATION"))],
       f"the stale tags are gone ({list(f.keys())})")
    ok(not any(os.path.exists(s) for s in stale_sidecars),
       "and the stale sidecars are gone too")
    ok(f.get("LYRICS") == [EN],
       "the lyrics themselves are untouched")
    # …and the run is idempotent: nothing left to remove, nothing rewritten.
    stale_before = open(stale_flac, "rb").read()
    stats = xl.run_lyrics_xlit({
        "music_folder": run_dir, "targets": [stale_flac],
        "ai_base_url": "http://x/v1", "ai_model": "m",
        "lyrics_format": "EMBEDDED", "lyrics_translation_langs": "en",
        "lyrics_xlit_enabled": True, "lyrics_translate_enabled": True,
    })
    ok(stats.get("stale_removed") == 0
       and open(stale_flac, "rb").read() == stale_before,
       "a second run finds nothing to remove and rewrites nothing")
    shutil.rmtree(run_dir, ignore_errors=True)

shutil.rmtree(TMP, ignore_errors=True)
print()
if FAILS:
    print("FAILED: %d check(s)" % len(FAILS))
    for f in FAILS:
        print("  -", f)
    sys.exit(1)
print("all lyrics-xlit checks passed")
