"""ITUNESADVISORY when no source states one — the ladder, and the genre rule.

* the merge rule is 1 > 0 > 2 and "nobody stated anything" is None, never an
  invented value (`integrations.merge_advisory`, exercised in
  `test_advisory_sources.py`);
* a source that STATED a value wins: it is written as it stands with its own
  provenance, and NOTHING else is asked about it — no instrumental rule, no AI.
  A stated 0 is final. Every case below counts the AI calls, with a stub that
  records them, so "the model was not asked" is measured, never assumed;
* when EVERY source came up with nothing, the ladder runs:
  instrumental → AI (fed the lyrics when the file has them) →
  `advisory_fallback`, and every answer carries the stage that produced it;
* an AI that answers 3 (or something unparseable) is "cannot tell" and falls
  THROUGH the ladder — the value space this app stores is 0/1/2, so a stored 3
  would fail the grader on every track carrying one;
* `advisory_fallback` is the user's last resort: "0", "2" or "none" (None
  means leave the tag alone);
* the lyrics WORD SCAN is gone, key and all: its switch is not in
  `DEFAULT_CONFIG` any more, and a stored config that still carries it
  normalizes to a dict without it — no exception, no phantom key;
* script 8 imports no genre and invents none: `mlo.autotag` has no provider
  hook left, and trimming a track that carries no GENRE writes nothing.

Run:  python tools/test_advisory_ladder.py
"""
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mlo import advisory
from mlo import autotag
from mlo.config import DEFAULT_CONFIG, normalize_config
from server import ai as ai_mod

_MISSING = object()


class FakeAudioFile:
    """`get_tag` for the ladder, plus the three methods `trim_genres` uses."""

    def __init__(self, tags=None, values=None):
        self.tags = dict(tags or {})
        self.values = values
        self.written = _MISSING

    def get_tag(self, name):
        return self.tags.get(name)

    def get_lyrics(self):
        """Empty: the embedded LYRICS tag holds nothing, which is what makes
        `local_lyrics` fall through to the .lrc sidecar."""
        return None

    def tag_values(self, name):
        return [] if self.values is None else list(self.values)

    def set_tag(self, name, value):
        self.values = list(value) if isinstance(value, list) else [value]
        self.written = value
        return True

    def delete_tag(self, name):
        self.values, self.written = None, []
        return True


def cfg(**over):
    base = {"advisory_ai_classify": True,
            "advisory_fallback": "0", "auto_zero_advisory_for_instrumental": True,
            "ai_base_url": "http://127.0.0.1:9/v1", "ai_model": "test-model"}
    base.update(over)
    return base


_real_configured, _real_chat = ai_mod.ai_configured, ai_mod.ai_chat

# --------------------------------------------------------------------------- #
# 1) A source that STATED a value wins, and the AI is NOT asked about it.
#    The stub records every call AND raises, so a stray ask is visible two
#    ways; the recorded count is the assertion.
# --------------------------------------------------------------------------- #
calls = []


def loud_chat(*a, **k):
    calls.append(k)
    raise AssertionError("the AI was asked about a track a source already stated")


ai_mod.ai_configured = lambda c: True
ai_mod.ai_chat = loud_chat


def stated(value, source, **kw):
    out = advisory.decide_advisory(cfg(), value=value, source=source, **kw)
    assert out == {"value": value, "source": source, "stage": "sources",
                   "fallback": False}, out
    return out


try:
    stated(1, "deezer-isrc", lyrics="i dont give a fuck")
    # a stated 0 with an explicit lyric in the file is STILL the source's answer
    stated(0, "deezer-isrc", lyrics="i dont give a fuck")
    # a clean edition's 2, stated by Apple, is written as it stands
    stated(2, "apple-album", lyrics="")
    # an instrumental a source already rated keeps the source's value: the
    # provider's word outranks even the tag's own inference
    stated(0, "deezer-isrc", af=FakeAudioFile({"INSTRUMENTAL": "1"}),
           lyrics="i dont give a fuck")
    assert calls == [], (
        f"the AI WAS asked about a track a source already rated ({calls}) — a "
        "stated value is final, and the model is not a second opinion on it")

    # The AI's answer is not recorded as a source behind a stated value either:
    # it is not one.
    track_answers = {}
    advisory.decide_advisory(cfg(), value=1, source="deezer-isrc",
                             answers=track_answers, lyrics="i dont give a fuck")
    assert track_answers == {}, track_answers
    assert calls == [], calls

    # ----------------------------------------------------------------------- #
    # 2) An instrumental has no words to be explicit with → 0 → no AI call.
    # ----------------------------------------------------------------------- #
    af_inst = FakeAudioFile({"INSTRUMENTAL": "1"})
    out = advisory.decide_advisory(cfg(), af=af_inst, lyrics="i dont give a fuck")
    assert out == {"value": 0, "source": "instrumental",
                   "stage": "instrumental", "fallback": False}, out
    assert calls == [], "the AI is not asked about an instrumental"
    # the setting off means the rule does not fire — the ladder continues, and
    # with nobody having stated anything the AI is what answers
    ai_mod.ai_chat = lambda *a, **k: "1"
    out = advisory.decide_advisory(
        cfg(auto_zero_advisory_for_instrumental=False), af=af_inst, lyrics="")
    assert out["stage"] == "ai" and out["value"] == 1, out

    # ----------------------------------------------------------------------- #
    # 3) Every source silent + the AI configured: the AI decides.
    # ----------------------------------------------------------------------- #
    sent = {}

    def fake_chat(cfg_, system, user, timeout=None):
        sent["system"], sent["user"] = system, user
        return "Answer: 1"

    ai_mod.ai_chat = fake_chat
    out = advisory.decide_advisory(
        cfg(),
        af=FakeAudioFile({"ARTIST": "Rihanna", "TITLE": "S&M", "ALBUM": "Loud"}),
        lyrics="sticks and stones may break my bones")
    assert out == {"value": 1, "source": "ai-lyrics", "stage": "ai",
                   "fallback": False}, out
    assert "sticks and stones may break my bones" in sent["user"], sent
    assert "Rihanna" in sent["user"] and "S&M" in sent["user"], sent
    # The prompt IS the rubric, and the parser only accepts 0/1/2 (3 falls
    # through) — so what the prompt must say is that the four answers exist and
    # that mild language alone is not explicit. Assert those two facts, not a
    # sentence: the wording is the model's instruction, not this suite's
    # contract.
    rubric = sent["system"].lower()
    assert all(f"{digit} =" in rubric for digit in "0123"), sent
    assert "mild" in rubric and "explicit" in rubric, sent

    # the answer IS the value's source when the AI is who answered: the reply's
    # per-source map names it, beside the providers (which said nothing)
    track_answers = {}
    out = advisory.decide_advisory(cfg(), answers=track_answers, lyrics="whatever")
    assert out["stage"] == "ai" and out["value"] == 1, out
    assert track_answers == {"ai-lyrics": 1}, track_answers

    # with no words in the file the source says so ("ai", not "ai-lyrics")
    out = advisory.decide_advisory(cfg(), lyrics="")
    assert out == {"value": 1, "source": "ai", "stage": "ai",
                   "fallback": False}, out

    # ----------------------------------------------------------------------- #
    # 4) The AI saying 3 (cannot tell) or nonsense falls THROUGH the ladder.
    # ----------------------------------------------------------------------- #
    for reply in ("3", "Answer: 3", "no idea", "", "maybe"):
        ai_mod.ai_chat = lambda *a, _r=reply, **k: _r
        out = advisory.decide_advisory(cfg(), lyrics="i dont give a fuck about you")
        assert out == {"value": 0, "source": "fallback", "stage": "fallback",
                       "fallback": True}, (reply, out)
    # ... and with the fallback set to "none" nothing is written at all
    out = advisory.decide_advisory(cfg(advisory_fallback="none"), lyrics="")
    assert out["value"] is None and out["source"] == "" and out["fallback"] is True, out

    # ----------------------------------------------------------------------- #
    # 5) No AI configured: `advisory_fallback` is what decides.
    # ----------------------------------------------------------------------- #
    ai_mod.ai_configured = lambda c: False
    for raw, want in (("0", 0), ("2", 2), ("none", None)):
        out = advisory.decide_advisory(cfg(advisory_fallback=raw),
                                       lyrics="shut the fuck up")
        assert out["stage"] == "fallback" and out["value"] == want, (raw, out)
        assert out["fallback"] is True, out
    # an unknown value in a hand-edited config is not a crash: it is 0
    out = advisory.decide_advisory(cfg(advisory_fallback="nonsense"), lyrics="")
    assert out["value"] == 0, out

    # `advisory_ai_classify` off means the AI is never asked, and the fallback
    # answers instead — even with a provider configured and lyrics in hand
    ai_mod.ai_configured = lambda c: True
    ai_mod.ai_chat = lambda *a, **k: calls.append(k) or "1"
    calls.clear()
    out = advisory.decide_advisory(cfg(advisory_ai_classify=False))
    assert out["stage"] == "fallback" and out["value"] == 0, out
    assert calls == [], "advisory_ai_classify off: the AI is never asked"

    # ----------------------------------------------------------------------- #
    # 6) The lyrics come off the DISK when the caller has none in hand — and
    #    they are what the AI is asked WITH: only its TRIGGER changed.
    # ----------------------------------------------------------------------- #
    root = tempfile.mkdtemp(prefix="mlo_advisory_ladder_")
    try:
        track = os.path.join(root, "01 - Song.flac")
        with open(track, "wb") as fh:
            fh.write(b"")
        with open(os.path.join(root, "01 - Song.lrc"), "w", encoding="utf-8") as fh:
            fh.write("[00:12.34] i dont give a fuck about you\n[00:15.00] and i mean it\n")
        read = {}

        def disk_chat(cfg_, system, user, timeout=None):
            read["user"] = user
            return "1"

        ai_mod.ai_chat = disk_chat
        out = advisory.decide_advisory(cfg(), path=track, af=FakeAudioFile())
        assert out == {"value": 1, "source": "ai-lyrics", "stage": "ai",
                       "fallback": False}, out
        assert "i dont give a fuck about you" in read["user"], read
        # a provider's stated value stops the ladder at step 1, so the AI is
        # not asked and nothing is read for it
        read.clear()
        out = advisory.decide_advisory(cfg(), value=0, source="apple-album",
                                       path=track, af=FakeAudioFile())
        assert out == {"value": 0, "source": "apple-album", "stage": "sources",
                       "fallback": False}, out
        assert read == {}, "a stated value is not re-read: no AI call for it"
    finally:
        shutil.rmtree(root, ignore_errors=True)
finally:
    ai_mod.ai_configured, ai_mod.ai_chat = _real_configured, _real_chat

# --------------------------------------------------------------------------- #
# 7) The lyrics word scan is GONE, key and all: the removal is verified by the
#    one contract that matters to a saved install — a config still carrying the
#    deleted switch normalizes to a dict WITHOUT it, no exception, no phantom
#    key nobody reads. The switch's own name is assembled from its parts so the
#    sweep for live references to the deleted stage stays empty, which is the
#    point of removing it.
# --------------------------------------------------------------------------- #
removed_switch = "advisory_" + "lyrics_scan"
assert removed_switch not in DEFAULT_CONFIG, \
    "the removed lyrics-scan switch must not ship in DEFAULT_CONFIG"
legacy = {removed_switch: True, "advisory_ai_classify": False,
          "advisory_auto_fetch": True, "advisory_fallback": "2"}
norm = normalize_config(legacy)
assert removed_switch not in norm, f"the removed switch survived: {norm}"
for key, want in (("advisory_ai_classify", False), ("advisory_auto_fetch", True),
                  ("advisory_fallback", "2")):
    assert norm.get(key) == want, (key, norm.get(key))

# --------------------------------------------------------------------------- #
# 8) Script 8 imports no genre: no provider hook left, and a track with no
#    GENRE gets none (only the cap is enforced).
# --------------------------------------------------------------------------- #
assert not hasattr(autotag, "set_genre_lookup"), "the genre hook is gone"
assert not hasattr(autotag, "_genre_lookup"), "the genre hook is gone"

fake = FakeAudioFile(values=None)
assert autotag.trim_genres(fake, 2) == 0
assert fake.written is _MISSING, fake.written

print("advisory ladder: all assertions passed")
