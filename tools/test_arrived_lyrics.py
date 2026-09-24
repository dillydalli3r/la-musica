#!/usr/bin/env python3
"""Lyrics that ARRIVED inside a downloaded/imported file, per arrival shape.

What the owner asked for: "Make sure that lyrics already in downloaded files
are only correct if synced / formatted properly already." The pipeline may
canonicalise an arrived lyric (script 1, i.e. `mlo.lyrics`'s own per-file pass)
and keep it, or it must not keep it as if it were right — and the grade has to
say which. Nothing may "look right because it arrived".

This suite drives the REAL entry point every import path ends in
(`server.imports.finish_album`) over one fixture album per arrival shape:

    1. an .lrc alone            — timed / untimed
    2. the embedded LYRICS tag  — timed / untimed
    3. both at once             — agreeing / disagreeing
    4. a line with stacked timestamps ("[a][b]text")
    5. stray metadata ("[ar:…]") and wrong timestamp precision
    6. an empty / whitespace-only lyric
    7. trailing spaces, blank lines, CRLF

and asserts, for each: what the import decided (its own report), what the file
holds afterwards, and what the app's own grade says about it. Then the same
through a chain that FETCHES (script 13), and finally the DOWNLOAD PATH: a
folder that went through `server.soulseek_auto`'s own import hand-off must end
in exactly the state a hand import of the same folder leaves.

Only two things are stubbed, and both are named where they are stubbed: the
pre-chain lookups (network sources a test cannot call) and the provider chain
itself (which is what decides whether the fetch writes synced, plain or
nothing). Everything the owner's rule is about — the drop, the settle, the
format pass, the fetch's own write and the grade — is the shipping code.
"""
import os
import shutil
import sys
import tempfile
import threading

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from mlo.audio import AudioFile                     # noqa: E402
from mlo import grader as grader_mod                # noqa: E402
from mlo import lyrics as L                         # noqa: E402
from mlo import lyrics_fetch                        # noqa: E402
from mlo import lyrics_providers as lp              # noqa: E402
from server import imports                          # noqa: E402
from server import script_runners                   # noqa: E402
from server import soulseek_auto                    # noqa: E402

TMP = tempfile.mkdtemp(prefix="mlo-arrived-")
MUSIC = os.path.join(TMP, "music")
os.makedirs(MUSIC)

# A real (if silent) MP3 frame: the tags are what this suite is about, and a
# junk byte string would be an unreadable file.
_MP3_FRAME = bytes([0xFF, 0xFB, 0x90, 0x00]) + b"\x00" * 413

TIMED = "[00:01.00]first line\n[00:05.00]second line"
TIMED_OTHER = "[00:02.00]bravo one\n[00:06.00]bravo two"
PLAIN = "first line\nsecond line"
STACKED = "[00:01.00][00:05.00]first line\n[00:09.00]second line"
STRAY = "[ar:Somebody]\n[ti:A Song]\n[00:01.00]first line\n[00:05.00]second line"
STRAY_CANON = "[00:01.00]first line\n[00:05.00]second line"
PRECISION = "[00:01.000]first line\n[00:05.000]second line"
TRAILING = "[00:01.00]first line   \n\n\n[00:05.00]second line\n"
CRLF = "[00:01.00]first line\r\n[00:05.00]second line\r\n"

# Fetched lyrics: what a provider answers, so "the fetch replaced them" is a
# fact this suite can see rather than assume.
FETCHED_SYNCED = "[00:03.00]fetched line one\n[00:07.00]fetched line two"
FETCHED_PLAIN = "fetched line one\nfetched line two"

failures = []


def check(name, cond, detail=""):
    if not cond:
        failures.append(f"{name} — {detail}")
        print(f"FAIL {name} — {detail}")


# --------------------------------------------------------------------------- #
# The fixture: one album per arrival shape
# --------------------------------------------------------------------------- #
def write_track(album, name="01 - Track.mp3", tag=None, lrc=None):
    os.makedirs(album, exist_ok=True)
    path = os.path.join(album, name)
    with open(path, "wb") as fh:
        fh.write(_MP3_FRAME * 40)
    af = AudioFile(path)
    for key, value in {"ARTIST": "Arrived Artist", "ALBUMARTIST": "Arrived Artist",
                       "ALBUM": os.path.basename(album), "TITLE": "Track",
                       "TRACKNUMBER": "1", "INSTRUMENTAL": "0",
                       "MEDIA": "Digital Media"}.items():
        assert af.set_tag(key, value), (path, key)
    if tag is not None:
        assert af.set_lyrics(tag), path
    if lrc is not None:
        with open(os.path.splitext(path)[0] + ".lrc", "w", encoding="utf-8") as fh:
            fh.write(lrc)
    return path


def state(path):
    """What a track stores right now: the kind, both texts, and whether each
    source is still there."""
    af = AudioFile(path)
    embedded = af.get_lyrics() or None
    lrc_path = L._lrc_for(path)
    sidecar = None
    if os.path.isfile(lrc_path):
        with open(lrc_path, encoding="utf-8", errors="replace") as fh:
            sidecar = fh.read()
    return {"kind": L.stored_lyrics_kind(embedded, sidecar),
            "tag": embedded, "lrc": sidecar,
            "has_tag": bool(str(embedded or "").strip()),
            "has_lrc": os.path.isfile(lrc_path)}


def grade(album, cfg):
    """The app's own verdict: (kind, LYRICS issue?, the messages about it)."""
    res = grader_mod._grade_album(album, str(cfg.get("lyrics_format", "EMBEDDED")),
                                 cfg) or {}
    tracks = res.get("tracks") or []
    row = tracks[0] if tracks else {}
    issues = res.get("issues") or {}
    msgs = sorted(m for m in issues if "yric" in str(m))
    return {"kind": row.get("lyrics_kind"), "flagged": "LYRICS" in (row.get("issues") or []),
            "messages": msgs}


def snapshot_lyrics(report):
    """The lyric-relevant half of a `finish_album` result, so two paths can be
    compared on what they DID and not only on what they left behind."""
    report = report or {}
    return {"dropped": (report.get("dropped") or {}).get("lyrics"),
            "settled": {k: v for k, v in
                        ((report.get("settled") or {}).get("lyrics") or {}).items()
                        if k != "tracks"}}


def canonical(text):
    """Script 1's own canonical form for an EMBEDDED write."""
    return L._canonical_lyrics(
        L.format_lyrics_text(text, precision=2, strip_metadata=True,
                             collapse_blank_lines=True, lrc_enhanced_enabled=True,
                             lrc_enhanced_word_sync=True, lrc_extended_enabled=True,
                             lrc_add_zero_timestamp=False),
        append_final_newline=False)


# --------------------------------------------------------------------------- #
# The stubs: the pre-chain lookups (network) and the provider chain
# --------------------------------------------------------------------------- #
ANSWER = {"synced": "", "plain": ""}

_real = {n: getattr(imports, n) for n in
         ("stamp_rym_links", "fetch_advisories", "fetch_instrumentals",
          "run_metadata_step", "run_cover_step", "_stamp_release")}
_real_chain = script_runners.run_chain
_real_resolve = None
_real_providers = dict(lp._PROVIDERS)
_real_absent = None
_real_metadata_account = None


def _provider(artist, title, album, duration, cfg, yt):
    """One stub provider for the whole chain: what ANSWER says, nothing else."""
    if not (ANSWER.get("synced") or ANSWER.get("plain")):
        return None
    return lp._hit(ANSWER.get("synced") or "", ANSWER.get("plain") or "",
                   artist, title, album, duration)


def stub_chain(cfg, ids, targets=None, force=None, progress=None, wait=True,
               timeout=None, final=None):
    """The chain, with script 1 and script 13 run for real over the album.

    Script 1's own per-file pass is `mlo.lyrics._process_lyrics_for_audio` (the
    code path script 1 and the settle both use) and script 13's core is
    `mlo.lyrics_fetch.fetch_one`; every other id is a no-op, because what this
    suite is about is the lyrics. Recorded, so a case can say whether the
    fetch ran at all.
    """
    names = []
    for target in (targets or []):
        if os.path.isdir(target):
            names.extend(os.path.join(target, f) for f in sorted(os.listdir(target))
                         if f.lower().endswith((".mp3", ".flac", ".m4a", ".opus", ".ogg")))
        elif os.path.isfile(target):
            names.append(target)
    out = []
    for sid in ids:
        if sid == 1:
            for path in names:
                L._process_lyrics_for_audio(path, cfg)
            out.append({"id": 1, "label": "Format lyrics", "error": None})
        elif sid == 13:
            for path in names:
                lyrics_fetch.fetch_one(path, cfg)
            out.append({"id": 13, "label": "Fetch lyrics", "error": None})
    return out


def _stub_absent(path, config, result):
    """`server.instrumental.lyrics_absent`: a test cannot ask the sources, and
    marking a fixture INSTRUMENTAL would hide the lyrics question this suite is
    asking. The real answer is pinned in the instrumental suite."""
    return {}


def stub_step(**payload):
    def _fn(*a, **k):
        return dict(payload)
    return _fn


def install_stubs():
    global _real_resolve, _real_absent, _real_metadata_account
    imports.stamp_rym_links = stub_step(album=None, artist=None, note="")
    imports.fetch_advisories = stub_step(updated=0, values={}, sources={}, answers={})
    imports.fetch_instrumentals = stub_step(updated=0, values={}, evidence={})
    imports.run_metadata_step = stub_step(staged=False, applied={})
    imports.run_cover_step = stub_step(fetched=False, applied={}, source=None,
                                       note="", staged=False, candidates=0)
    imports._stamp_release = lambda album_dir, release, cfg: (0, 0)
    script_runners.run_chain = stub_chain
    from server import integrations
    _real_resolve = integrations.resolve_release
    integrations.resolve_release = lambda mbid: (None, "")
    _real_absent = lyrics_fetch._mark_lyrics_absent
    lyrics_fetch._mark_lyrics_absent = _stub_absent
    _real_metadata_account = soulseek_auto._account_metadata
    soulseek_auto._account_metadata = lambda *a, **k: None
    for pid in list(lp._PROVIDERS):
        lp._PROVIDERS[pid] = _provider


def remove_stubs():
    for name, fn in _real.items():
        setattr(imports, name, fn)
    script_runners.run_chain = _real_chain
    from server import integrations
    integrations.resolve_release = _real_resolve
    lyrics_fetch._mark_lyrics_absent = _real_absent
    soulseek_auto._account_metadata = _real_metadata_account
    lp._PROVIDERS.clear()
    lp._PROVIDERS.update(_real_providers)


def cfg_for(**over):
    cfg = {
        "music_folder": MUSIC, "import_scripts": [1, 13],
        "import_auto_scripts": True, "import_review_families": [],
        "import_autonomy": "automatic", "lyrics_format": "EMBEDDED",
        "lyrics_allow_plain": False, "import_keep_synced_lyrics": False,
        "advisory_auto_fetch": False, "metadata_auto_fetch": False,
        "cover_auto_fetch": False, "rym_links_auto": False,
        "instrumental_auto_fetch": False, "genre_autofill": False,
        "grade_check_lyrics": True, "grade_check_lyrics_format": True,
        "grade_check_lyrics_spaces": True, "grade_check_lyrics_blank_lines": True,
        "grade_check_lyrics_zero": True, "grade_check_lyrics_lang_tags": True,
        "lrc_enhanced_enabled": True, "lrc_enhanced_word_sync": True,
        "lrc_sync_level": "LINE", "lrc_timestamp_precision": 2,
        "lrc_strip_metadata": True, "lrc_collapse_blank_lines": True,
        "lrc_extended_enabled": True, "lrc_add_zero_timestamp": False,
        "lrc_zero_timestamp_blank": False, "optimize_embedded_lyrics": True,
        "optimize_lrc": True, "audio_tag_writes": {},
    }
    cfg.update(over)
    return cfg


# The shapes, and what each is: the two places a lyric lives, and the kind a
# `stored_lyrics_kind` reader sees before the import touches the file. Whether
# script 1 rewrites it is NOT declared here — that is asserted against the
# file's own before/after state and the settle's `formatted` count, so this
# table cannot claim a shape is "already canonical" while the code reformats it.
SHAPES = {
    "lrc_timed":      dict(lrc=TIMED, kind="synced"),
    "lrc_untimed":    dict(lrc=PLAIN, kind="plain"),
    "tag_timed":      dict(tag=TIMED, kind="synced"),
    "tag_untimed":    dict(tag=PLAIN, kind="plain"),
    "both_agree":     dict(tag=TIMED, lrc=TIMED, kind="synced"),
    "both_disagree":  dict(tag=PLAIN, lrc=TIMED_OTHER, kind="synced"),
    "stacked":        dict(tag=STACKED, kind="synced"),
    "stray_metadata": dict(tag=STRAY, kind="synced"),
    "precision":      dict(tag=PRECISION, kind="synced"),
    "trailing_crlf":  dict(tag=TRAILING, kind="synced"),
    "empty":          dict(tag="", lrc="\n   \n", kind=None),
}


def album_for(name, folder=None):
    spec = SHAPES[name]
    album = folder or os.path.join(MUSIC, name)
    path = write_track(album, tag=spec.get("tag"), lrc=spec.get("lrc"))
    return album, path


install_stubs()
try:
    # ----------------------------------------------------------------------- #
    # 1) THE KEEP REGIME — the arrived lyric is what the import decides about.
    #    `import_keep_synced_lyrics` keeps what arrived (the switch the four
    #    families have), the chain still holds script 13, and the provider
    #    answers NOTHING: so the end state is the arrival's own fate and
    #    nothing else. This is the arrival table.
    # ----------------------------------------------------------------------- #
    ANSWER["synced"] = ANSWER["plain"] = ""
    KEEP_CFG = cfg_for(import_keep_synced_lyrics=True)
    print("== the arrival table (keep what arrived, no provider answer) ==")
    for name, spec in SHAPES.items():
        album, path = album_for(name)
        before = state(path)
        check(f"{name}: the fixture is the shape it claims to be",
              before["kind"] == spec["kind"], repr(before))
        res = imports.finish_album(album, KEEP_CFG)
        settled = res["settled"]["lyrics"]
        cleared = res["dropped"]["lyrics"]
        after = state(path)
        verdict = grade(album, KEEP_CFG)

        if spec["kind"] is None:
            # Nothing of this family to decide: the stub sidecar goes (a
            # writer cleared it), the track reports no lyrics, and the grade
            # says so rather than crediting an empty "lyric".
            check(f"{name}: the stub was cleared", not after["has_tag"] and not after["has_lrc"],
                  repr(after))
            check(f"{name}: counted as empty, not as kept lyrics",
                  settled["empty"] == 1 and settled["kept"] == 1, repr(settled))
            check(f"{name}: the grade reports missing lyrics",
                  verdict["flagged"] and verdict["kind"] is None, repr(verdict))
            continue

        if spec["kind"] == "plain":
            # An untimed arrival this install does not accept. It goes in the
            # import's OWN first pass — the family decision, which clears what
            # arrived so script 13 can write this import's own answer — and the
            # import's line says so. Nothing is fetched here (the provider
            # answered nothing), so the grade reports MISSING lyrics, never
            # "not optimally formatted" (which names a script that cannot
            # help).
            check(f"{name}: the untimed arrival was cleared",
                  cleared == 1 and after["kind"] is None, f"{cleared} {after!r}")
            check(f"{name}: the settle had nothing left to remove",
                  settled["dropped"] == 0 and settled["message"] == "", repr(settled))
            check(f"{name}: the grade reports missing lyrics, not misformatting",
                  verdict["flagged"] and "Missing lyrics" in " ".join(verdict["messages"]),
                  repr(verdict))
            check(f"{name}: the import itself says so",
                  "arrived lyrics cleared" in imports.chain_summary(res),
                  imports.chain_summary(res))
            continue

        # Synced: KEPT. Script 1's own pass runs over it — the verdict is
        # either "it was already the canonical form" or "the formatter
        # repaired it", and the grade follows.
        if name == "stacked":
            # Repairable by nothing: a line carrying two timestamps cannot be
            # split, so pass two asks the app's OWN grade and removes what the
            # grade rejects. The removal is counted apart (unformatted) and the
            # user is told with the sentence for that case.
            check(f"{name}: removed, and counted as unformatted",
                  settled["dropped"] == 1 and settled["unformatted"] == 1
                  and not after["has_tag"], f"{settled} {after}")
            check(f"{name}: the settle words it as the unformatted case",
                  "not in the form this install's grading check asks for"
                  in settled["message"], repr(settled))
            check(f"{name}: the import says so",
                  "not in the form this install's grading check asks for"
                  in imports.chain_summary(res), imports.chain_summary(res))
            check(f"{name}: the grade reports missing lyrics",
                  verdict["flagged"] and verdict["kind"] is None, repr(verdict))
            continue

        check(f"{name}: the synced arrival was kept", after["has_tag"] or after["has_lrc"],
              repr(after))
        check(f"{name}: what the file holds is the canonical form",
              canonical(after["tag"] or after["lrc"]) == (after["tag"] or after["lrc"]),
              repr(after))
        check(f"{name}: the grade passes it", not verdict["flagged"], repr(verdict))
        # `formatted` is script 1's own pass, counted only when it really
        # rewrote the file — so it must agree with what the file shows: an
        # already-canonical tag is untouched (0), a repaired one or a sidecar
        # folded into the tag is a change (1).
        changed = before != after
        check(f"{name}: the canonicalisation count matches what changed on disk",
              settled["formatted"] == (1 if changed else 0),
              f"{settled} before={before!r} after={after!r}")
        if changed:
            check(f"{name}: and the import reports it",
                  "Lyrics script's own pass" in imports.chain_summary(res),
                  imports.chain_summary(res))
        else:
            check(f"{name}: nothing was rewritten, so nothing is claimed",
                  "Lyrics script's own pass" not in imports.chain_summary(res),
                  imports.chain_summary(res))

    # The two shapes whose canonical form is a specific string, so "repairable"
    # cannot hide a formatter that rewrote the words too.
    check("stray metadata: only the metadata lines went",
          state(os.path.join(MUSIC, "stray_metadata", "01 - Track.mp3"))["tag"] == STRAY_CANON,
          repr(state(os.path.join(MUSIC, "stray_metadata", "01 - Track.mp3"))["tag"]))
    _dis = state(os.path.join(MUSIC, "both_disagree", "01 - Track.mp3"))
    check("a disagreeing sidecar: ONE canonical text survives, not two",
          _dis["kind"] == "synced" and _dis["tag"] == canonical(TIMED_OTHER)
          and not _dis["has_lrc"], repr(_dis))
    print("arrival table: done")

    # ----------------------------------------------------------------------- #
    # 2) THE FETCH REGIME — the shipped default. The arrived lyrics are cleared
    #    so the import can fetch its own (script 13 is in the chain), and what
    #    the track ends holding is the FETCH's answer, canonical and graded.
    #    Nothing survives merely because it arrived.
    # ----------------------------------------------------------------------- #
    ANSWER["synced"], ANSWER["plain"] = FETCHED_SYNCED, ""
    print("== the fetch regime (default config, provider answers synced) ==")
    for name in SHAPES:
        album, path = album_for(name, folder=os.path.join(MUSIC, f"fetch-{name}"))
        res = imports.finish_album(album, cfg_for())
        after = state(path)
        verdict = grade(album, cfg_for())
        if name == "empty":
            # Nothing arrived but a stub (a tag with no words and a sidecar
            # holding whitespace): the stub file goes, and NO "arrived lyrics
            # cleared" claim is made about a file that had none — the count
            # stays 0 while the sidecar really is gone.
            check(f"{name}: the stub sidecar went, uncounted",
                  res["dropped"]["lyrics"] == 0 and not after["has_lrc"],
                  repr(res["dropped"]))
            check(f"{name}: the import claims nothing about arrived lyrics",
                  "arrived lyrics cleared" not in imports.chain_summary(res),
                  imports.chain_summary(res))
            check(f"{name}: the track now holds the fetched lyric",
                  after["kind"] == "synced" and after["tag"] == canonical(FETCHED_SYNCED),
                  repr(after))
            check(f"{name}: the grade passes the fetched lyric", not verdict["flagged"],
                  repr(verdict))
            continue
        check(f"{name}: the arrived lyric was cleared for the fetch",
              res["dropped"]["lyrics"] == 1, repr(res["dropped"]))
        check(f"{name}: the import says the arrived lyrics went",
              "arrived lyrics cleared" in imports.chain_summary(res),
              imports.chain_summary(res))
        check(f"{name}: the track now holds the FETCHED lyric, not the arrival",
              after["kind"] == "synced" and after["tag"] == canonical(FETCHED_SYNCED),
              repr(after))
        check(f"{name}: the grade passes the fetched lyric", not verdict["flagged"],
              repr(verdict))
    print("fetch regime: done")

    # ----------------------------------------------------------------------- #
    # 3) THE PLAIN FALLBACK — the other half of the same rule. When nothing
    #    synced exists and a provider has plain text, the fetch WRITES it: the
    #    track ends with an honest plain lyric, never with nothing (which is
    #    how a plain-only track used to be reported and even marked
    #    INSTRUMENTAL). It is graded exactly by the install's own setting.
    # ----------------------------------------------------------------------- #
    ANSWER["synced"], ANSWER["plain"] = "", FETCHED_PLAIN
    print("== the plain fallback (provider answers plain only) ==")
    for allow, want_flagged in ((False, True), (True, False)):
        album, path = album_for("tag_untimed",
                                folder=os.path.join(MUSIC, f"plain-allow-{int(allow)}"))
        cfg = cfg_for(lyrics_allow_plain=allow)
        res = imports.finish_album(album, cfg)
        after = state(path)
        verdict = grade(album, cfg)
        check(f"plain fallback (allow_plain={allow}): the fetched plain lyric is THERE",
              after["kind"] == "plain" and after["tag"] == canonical(FETCHED_PLAIN),
              repr(after))
        check(f"plain fallback (allow_plain={allow}): kept, never removed as an arrival",
              res["settled"]["lyrics"]["dropped"] in (0,) and after["has_tag"],
              repr(res["settled"]["lyrics"]))
        check(f"plain fallback (allow_plain={allow}): the grade follows the setting",
              verdict["flagged"] is want_flagged, repr(verdict))
        if want_flagged:
            check("plain fallback: the failing message names the plain state",
                  any("not optimally formatted" in m for m in verdict["messages"]),
                  repr(verdict))

    # ----------------------------------------------------------------------- #
    # 4) A KEPT FAMILY — `import_review_families` hands the lyrics to the user.
    #    Then nothing is cleared and nothing is fetched, the arrival is exactly
    #    as it was, and the grade is the honest verdict on it.
    # ----------------------------------------------------------------------- #
    ANSWER["synced"] = ANSWER["plain"] = ""
    print("== a kept lyrics family ==")
    KEPT = cfg_for(import_review_families=["lyrics"])
    for name in ("tag_untimed", "stacked", "stray_metadata"):
        album, path = album_for(name, folder=os.path.join(MUSIC, f"kept-{name}"))
        before = state(path)
        res = imports.finish_album(album, KEPT)
        after = state(path)
        verdict = grade(album, KEPT)
        check(f"kept family ({name}): nothing was cleared",
              res["dropped"]["lyrics"] == 0, repr(res["dropped"]))
        check(f"kept family ({name}): the settle reports the skip (no fetch, nothing touched)",
              res["settled"]["lyrics"]["state"] == "no-fetch"
              and res["settled"]["lyrics"]["dropped"] == 0, repr(res["settled"]))
        if name == "stray_metadata":
            # The chain still runs script 1, and script 1's job is exactly
            # this: the metadata the peer wrote is stripped, the file is the
            # canonical form, and the grade accepts it.
            check(f"kept family ({name}): script 1 canonicalised it",
                  after["tag"] == canonical(STRAY) and after["tag"] != before["tag"],
                  f"{before!r} -> {after!r}")
            check(f"kept family ({name}): the grade passes it",
                  not verdict["flagged"], repr(verdict))
        else:
            check(f"kept family ({name}): the arrival is exactly what it was",
                  after == before, f"{before} -> {after}")
            check(f"kept family ({name}): the grade FAILS it — kept, not blessed",
                  verdict["flagged"], repr(verdict))
    print("kept family: done")

    # ----------------------------------------------------------------------- #
    # 5) THE DOWNLOAD PATH — the auto-import's own hand-off must leave the same
    #    album as a hand import of the same bytes. `soulseek_auto
    #    ._start_import_chain` runs `finish_album` on a thread of its own; the
    #    thread is made synchronous here so the end state can be compared, and
    #    nothing else about its closure is stubbed.
    # ----------------------------------------------------------------------- #
    ANSWER["synced"] = ANSWER["plain"] = ""
    print("== the download path == (manual import vs the auto-import hand-off)")
    snapshots = {}
    reports = {}
    real_finish = imports.finish_album
    for kind in ("manual", "download"):
        album = os.path.join(MUSIC, f"cmp-{kind}")
        write_track(album, name="01 - Track.mp3", tag=STACKED)          # unformattable
        write_track(album, name="02 - Track.mp3", tag=TIMED)            # canonical synced
        write_track(album, name="03 - Track.mp3", lrc=PLAIN)            # untimed sidecar
        cfg = cfg_for(import_keep_synced_lyrics=True)
        if kind == "manual":
            res = real_finish(album, cfg)
            check("download path: the manual import ran", bool(res),
                  "finish_album returned nothing")
        else:
            # The chain's own thread, run where it is started — WITHOUT
            # patching `threading.Thread` itself: that module object is global,
            # so replacing its `Thread` would also replace the one
            # `ThreadPoolExecutor` builds its workers from (which is how an
            # import fans its per-file passes out), and the executor would run
            # its worker loop inline forever. Only the name `soulseek_auto`
            # looks up is swapped.
            real_threading = soulseek_auto.threading

            class _Inline:
                def __init__(self, target=None, name=None, daemon=None, args=(), kwargs=None):
                    self._t = target
                    self._a = args
                    self._k = kwargs or {}

                def start(self):
                    self._t(*self._a, **self._k)

            class _Shim:
                Thread = _Inline

                def __getattr__(self, item):
                    return getattr(real_threading, item)

            soulseek_auto.threading = _Shim()
            # What the auto-import's chain actually CALLED, recorded through the
            # one entry point it uses: the same album must go through the same
            # drop/settle/format work, not merely end up in the same shape.
            captured = {}

            def _rec(*a, **k):
                out = real_finish(*a, **k)
                captured.update(out or {})
                return out

            imports.finish_album = _rec
            try:
                soulseek_auto._start_import_chain(album, cfg, release=None,
                                                  download_dir="")
            finally:
                imports.finish_album = real_finish
                soulseek_auto.threading = real_threading
            res = captured
        snapshots[kind] = {
            name: state(os.path.join(album, name))
            for name in ("01 - Track.mp3", "02 - Track.mp3", "03 - Track.mp3")}
        snapshots[kind]["grade"] = grade(album, cfg)
        reports[kind] = res or {}

    check("download path: the SAME states as a hand import",
          snapshots["manual"] == snapshots["download"],
          f"manual={snapshots['manual']}\ndownload={snapshots['download']}")
    # …and by the same work: the family decision and the settle report the same
    # numbers on both paths, so an auto-import that skipped either step would
    # have to say so here.
    for kind in ("manual", "download"):
        report = reports[kind]
        check(f"download path ({kind}): the family decision and the settle ran",
              report.get("dropped", {}).get("lyrics") == 1
              and report.get("settled", {}).get("lyrics", {}).get("checked") == 3,
              repr(report.get("dropped")) + repr(report.get("settled")))
    check("download path: the same counts on both paths",
          snapshot_lyrics(reports["manual"]) == snapshot_lyrics(reports["download"]),
          f"{snapshot_lyrics(reports['manual'])} != {snapshot_lyrics(reports['download'])}")
    for name, got in snapshots["download"].items():
        if name == "grade":
            continue
        print(f"  {name}: kind={got['kind']!r} tag={got['tag']!r} lrc={got['lrc']!r}")
    print(f"  grade: {snapshots['download']['grade']}")
    check("download path: the unformattable arrival went on both paths",
          snapshots["manual"]["01 - Track.mp3"]["kind"] is None
          and snapshots["download"]["01 - Track.mp3"]["kind"] is None,
          repr(snapshots))
    check("download path: the canonical synced arrival survived",
          snapshots["download"]["02 - Track.mp3"]["kind"] == "synced", repr(snapshots))
    check("download path: the untimed sidecar went",
          snapshots["download"]["03 - Track.mp3"]["kind"] is None, repr(snapshots))
finally:
    remove_stubs()
    shutil.rmtree(TMP, ignore_errors=True)

if failures:
    print(f"\narrived lyrics: {len(failures)} check(s) FAILED")
    raise SystemExit(1)
print("arrived lyrics: the arrival table (kept / canonicalised / removed), the "
      "fetch regime, the plain fallback, a kept family and the download path "
      "— all assertions passed")
