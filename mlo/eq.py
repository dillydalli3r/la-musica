"""Equalizer APO / Peace EQ profiles, as an ffmpeg filter chain.

The Export page offers an equalizer so a device with a known weak spot (thin
earbuds, a car stereo) gets the correction baked into the exported files —
useful on players that have no EQ of their own. The profiles people already
have are Equalizer APO configuration files: AutoEQ's headphone corrections and
Peace's saved presets are the same text format, and every one of them is
either copied into a forum post, exported from Peace, or pasted from AutoEQ's
GitHub. So this module reads THAT format instead of inventing one:

    Preamp: -6.0 dB
    Filter 1: ON PK Fc 105 Hz Gain 4.5 dB Q 0.70
    Filter 2: OFF LS Fc 60 Hz Gain 2.0 dB Q 0.70
    GraphicEQ: 20 -3.0; 50 -2.0; 100 0.0; 5000 -1.5

Field order is free, the spellings F/Fc/freq are all accepted, ``dB``/``Hz``
suffixes are optional, and keywords are case-insensitive — real files written
by Peace and by hand use all of those. Peace saves its profiles with a UTF-8
BOM on some systems and as UTF-16 on others, so the bytes on disk are decoded
per encoding rather than assumed.

Two kinds of line are treated differently, because they cost different things:

* A line this module has no equivalent for (``Include:``, ``Convolution:``,
  ``Device:``, a banner) is IGNORED and REPORTED in ``unsupported``: the curve
  is what the user wrote, minus a feature the app cannot reproduce, and the
  import result says which line that was.
* A line that carries a BAND this parser cannot read — an unknown filter type,
  a frequency that is not a number, a ``GraphicEQ`` pair that is not two
  numbers — is an ERROR naming its line (``errors``). It is never dropped: a
  profile that silently loses one of its bands is a different curve, which for
  audio is worse than a refusal, so ``import_profile`` refuses the whole file,
  the export refuses to apply such a profile from disk, and the PLAYER installs
  nothing either — one sentence (``apply_refusal``) says so on all three
  surfaces, because a profile that cannot be exported must not be auditioned
  through as a shorter curve.

Anything this module cannot render is reported rather than dropped, because a
profile that silently loses its Convolution/Include half is not the curve the
user asked for.

Two APO constructs have no direct ffmpeg equivalent and are mapped:

* ``GraphicEQ`` (a band list, which is what AutoEQ publishes for headphones)
  becomes one peaking filter per band with Q 1.41 — the conversion AutoEQ
  itself emits for the same data, so a curve imported here sounds like the
  curve AutoEQ's "ParametricEQ" file produces. ``firequalizer`` could take the
  bands verbatim, but its ``gain_entry`` interpolates between the points we
  hand it, while Equalizer APO interpolates the same way between the bands it
  was given; making the two agree would mean re-deriving APO's own
  interpolation, and a curve that is subtly wrong at every band edge is worse
  than one that is right where AutoEQ says it is.
* ``LSC``/``HSC`` (shelf with a custom slope) render as the same shelf as
  ``LS``/``HS``: ffmpeg's shelf takes a Q, and APO's own shelf Q is what its
  slope parameter is expressed as in every profile seen in the wild.
"""

import codecs
import math
import os
import re
import time
import urllib.request
from urllib.parse import quote, unquote

from mlo.paths import app_data_dir, safe_segment, slug_name

# Imported profiles live beside the rest of the app state so they follow the
# library: <music>/.mlo/data/eq/<slug>.txt.
EQ_DIRNAME = "eq"

# A profile is a few dozen lines of text. The cap is what keeps a pasted
# binary/log file from filling the data dir, and it is generous enough that no
# real AutoEQ or Peace file comes near it.
MAX_PROFILE_BYTES = 64 * 1024

# The filter types Equalizer APO defines, keyed by their lower-case spelling.
_TYPES = {t.lower(): t for t in
          ("PK", "LS", "HS", "LP", "HP", "BP", "NO", "LSC", "HSC")}

# APO's OTHER spellings for the same filters, mapped to the type this module
# renders. The right-hand side is the type the spelling NAMES — APO's own
# configuration reference lists each alias in the row of the filter it is, and a
# low-pass stays a low-pass:
#
#   PEQ            peaking filter (its "Parametric EQ" row, the same row as PK) → PK
#   Modal          peaking filter (the same row; its T60 target is reported)    → PK
#   LPQ            low-pass with a Q (the LP row)                               → LP
#   HPQ            high-pass with a Q (the HP row)                              → HP
#   LS 6dB, LS 12dB   low shelf, 6 / 12 dB per octave                           → LS
#   HS 6dB, HS 12dB   high shelf, 6 / 12 dB per octave                          → HS
#   LSC 10.8 dB    low shelf with a custom slope in dB/octave                   → LSC
#   HSC 6 dB       high shelf with a custom slope in dB/octave                  → HSC
#
# The shelves keep their own Fc and Gain and are rendered as the shelf this
# module has; the slope in dB/octave itself is not carried over (ffmpeg's shelf
# takes a Q, and APO's own shelf Q is what its slope is expressed as in the
# profiles seen in the wild), and the result's notes say so.
_ALIASES = {"peq": "PK", "modal": "PK", "lpq": "LP", "hpq": "HP"}

# The shelf spellings APO writes as TWO words — "Filter 1: ON LS 6dB Fc …". The
# second word is the shelf's own slope; the type is APO's LS/HS shelf.
_SHELF_SPELLINGS = {"6db": 6.0, "12db": 12.0}

# APO types this module UNDERSTANDS and deliberately does not render, with the
# sentence the import reports. Neither can become one of the nine filters both
# renderers have, and guessing at one would invent a curve:
#
#   AP    all-pass: phase only, its magnitude is flat — a chain without it has
#         exactly the curve the file asked for, so nothing is lost by leaving
#         it out and nothing is honest in faking it with a notch.
#   IIR   the file's own b0…bm / a0…am coefficients, which a fixed filter bank
#         has no equivalent for.
_UNRENDERED = {
    "ap": ("an all-pass filter changes phase only — its magnitude response is "
           "flat, so leaving it out leaves the curve as the file wrote it"),
    "iir": ("an IIR filter is the file's own coefficients (b0…bm / a0…am), "
            "which this app has no filter for"),
}

# APO's parameter names, including the short forms files in the wild use.
_KEYS = {"fc": "fc", "f": "fc", "freq": "fc", "frequency": "fc",
         "gain": "gain", "g": "gain", "q": "q", "bw": "bw",
         "bandwidth": "bw"}

# Unit tokens that follow a value ("Fc 105 Hz Gain 6 dB"). They carry nothing
# the renderer needs — ffmpeg's frequencies are Hz and its gains are dB — so
# they are stepped over rather than treated as an unknown field.
_UNITS = ("hz", "db")

# A missing Q is the one field the formats leave optional. These are the
# widths the filter is normally drawn with: AutoEQ emits 1.41 for the peaking
# filters it derives from a GraphicEQ, a shelf and a Butterworth 2-pole
# low/high-pass are 0.7, and a band-pass/notch is 1.
_DEFAULT_Q = {"PK": 1.41, "LS": 0.7, "LSC": 0.7, "HS": 0.7, "HSC": 0.7,
              "LP": 0.707, "HP": 0.707, "BP": 1.0, "NO": 1.0}

# Q AutoEQ writes for a GraphicEQ band list converted to peaking filters.
_GRAPHICEQ_Q = 1.41

# The bounds a RENDERED band is clamped to — the same numbers the in-app player
# clamps with (web/src/lib/eqNodes.ts: EQ_FC_MIN/EQ_FC_MAX, EQ_GAIN_LIMIT,
# EQ_PREAMP_LIMIT and the Q range its `configure` uses). The player clamps
# because its node and the editor's own boxes are bounded; the ffmpeg chain
# applies the SAME limits because otherwise one profile would sound like two
# different curves — the app's own auditioning bounds are the promise the export
# has to keep. The file's own values stay in the parsed band (that is the
# file's provenance) and the result's notes say when one was outside them.
FC_MIN_HZ, FC_MAX_HZ = 20.0, 20000.0
GAIN_LIMIT_DB = 20.0
Q_MIN, Q_MAX = 0.1, 30.0
PREAMP_LIMIT_DB = 24.0

_NUMBER_RE = re.compile(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)")
# The preamp is matched by its NAME, not by "a name followed by a number": a
# "Preamp: high" line must come back as an error naming the line, never fall
# through to the ignored bucket, because the preamp is what keeps a boosted
# curve from clipping.
_PREAMP_RE = re.compile(r"^\s*preamp\s*:\s*(.*)$", re.IGNORECASE)
_FILTER_RE = re.compile(r"^\s*filter\s*\d*\s*:(.*)$", re.IGNORECASE)
_GRAPHIC_RE = re.compile(r"^\s*graphiceq\s*:(.*)$", re.IGNORECASE)

# APO's conditional execution: ``If:``/``ElseIf:``/``Else:``/``EndIf:``. The
# condition is an expression over APO's OWN variables (sampleRate,
# inputChannelCount, deviceName, user variables — its reference's own list),
# which this app does not model at all; see parse_apo for what is done instead
# of evaluating one.
_COND_RE = re.compile(r"^\s*(if|elseif|else|endif)\s*:\s*(.*)$", re.IGNORECASE)

# The APO token that means "this slot holds no filter" ("Filter 3: ON None").
# Peace writes one for every unused band, so it is skipped like a comment.
_NONE_TOKEN = "none"

# The ffmpeg filter each APO type renders to, and the shape of its arguments:
#
#   peak/shelf/band filters take a gain, a low/high pass has none.
_FFMPEG_FILTER = {"PK": "equalizer", "LS": "bass", "LSC": "bass",
                  "HS": "treble", "HSC": "treble", "LP": "lowpass",
                  "HP": "highpass", "BP": "bandpass", "NO": "bandreject"}

# The types whose whole effect is the gain: a low/high pass or a notch is a
# shape, not a level, so its (unused) 0 dB reading must not skip it.
_GAIN_TYPES = ("PK", "LS", "LSC", "HS", "HSC")


def _number(text):
    """The first number in *text* ("105 Hz", "-6.0", "0.7"), or None."""
    m = _NUMBER_RE.match(str(text or "").strip())
    return float(m.group(0)) if m else None


def _num(value):
    """A value as ffmpeg wants it: -6, 0.7, 1.41 — never "-6.0 dB"."""
    return f"{float(value):g}"


def _clamp(value, lo, hi):
    """*value* inside ``[lo, hi]`` — the bounds BOTH renderers use."""
    return min(hi, max(lo, float(value)))


def _out_of_bounds(band):
    """Whether *band* carries a value the renderers clamp (see the bounds)."""
    if not FC_MIN_HZ <= float(band["fc"]) <= FC_MAX_HZ:
        return True
    if not Q_MIN <= float(band["q"]) <= Q_MAX:
        return True
    return (band["type"] in _GAIN_TYPES
            and not -GAIN_LIMIT_DB <= float(band.get("gain") or 0.0) <= GAIN_LIMIT_DB)


def _band_to_q(bandwidth_octaves):
    """APO's ``BW`` (bandwidth in octaves) as the Q ffmpeg's filters take, or
    None when the file's own value cannot be one (a width of 0 or less)."""
    bw = float(bandwidth_octaves)
    if bw <= 0:
        return None
    return math.sqrt(2 ** bw) / (2 ** bw - 1)


# The filter types, spelled out for the sentence that refuses an unknown one.
_TYPE_LIST = ", ".join(("PK", "LS", "HS", "LP", "HP", "BP", "NO", "LSC", "HSC"))


def _parse_filter(body):
    """One ``Filter N: …`` body as ``(filter, error)``.

    ``filter`` is the band dict, the string ``"none"`` for APO's own empty slot
    (``Filter 1: ON None``) or for an OFF band of a type this app cannot hold,
    or the SENTENCE naming a filter APO defines and this app has no equivalent
    for (``AP``, ``IIR`` — reported, never applied). None with *error* says what
    could not be read. The tokens are scanned in the order the file wrote them,
    so any field order works and ``ON``/``OFF`` may sit wherever the writer put
    it. A token this parser does not know is an ERROR, not a loss to report:
    guessing at it (or dropping the band) would apply a curve the user did not
    write.
    """
    tokens = body.replace(",", " ").split()
    enabled, ftype, values = True, None, {}
    slope, t60 = None, None
    i = 0
    while i < len(tokens):
        token = tokens[i].lower()
        if token in ("on", "off"):
            enabled = token == "on"
            i += 1
            continue
        if token == _NONE_TOKEN:
            # The slot holds no filter at all — Peace writes one per unused
            # band. Enabled or not, there is no curve here to render.
            return "none", None
        if ftype is None and token in _UNRENDERED:
            # Understood, and deliberately not rendered (see _UNRENDERED). An
            # OFF one is skipped silently: it contributed nothing either way.
            if not enabled:
                return "none", None
            return _UNRENDERED[token], None
        if ftype is None and token in _ALIASES:
            ftype = _ALIASES[token]
            i += 1
            continue
        if token in _TYPES and ftype is None:
            if (token in ("ls", "hs") and i + 1 < len(tokens)
                    and tokens[i + 1].lower() in _SHELF_SPELLINGS):
                # "ON LS 6dB Fc …": APO's reference lists the type and its own
                # slope as those two words. The slope is kept for the report.
                slope = _SHELF_SPELLINGS[tokens[i + 1].lower()]
                ftype = _TYPES[token]
                i += 2
                continue
            ftype = _TYPES[token]
            i += 1
            continue
        if token in _UNITS:
            i += 1
            continue
        if token == "t60":
            # APO's Modal filter carries a decay on top of the band's shape
            # ("T60 target 100 ms", and files in the wild also write it without
            # the word). The peaking part is rendered; the decay is reported
            # instead of dropped in silence.
            at = i + 1
            if at < len(tokens) and tokens[at].lower() == "target":
                at += 1
            if at >= len(tokens):
                return None, "T60 needs a value (T60 target 100 ms)"
            value = _number(tokens[at])
            if value is None:
                return None, f"T60 needs a number, got {tokens[at]!r}"
            t60 = value
            i = at + 1
            if i < len(tokens) and tokens[i].lower() in ("ms", "s"):
                i += 1
            continue
        if token in _KEYS:
            name = _KEYS[token]
            at = i + 1
            # APO writes a bandwidth as "BW Oct 0.5": the unit word sits between
            # the key and its value.
            if name == "bw" and at < len(tokens) and tokens[at].lower() == "oct":
                at += 1
            if at >= len(tokens):
                return None, f"{token.capitalize()} has no value"
            value = _number(tokens[at])
            if value is None:
                return None, (f"{token.capitalize()} needs a number, got "
                              f"{tokens[at]!r}")
            values[name] = value
            i = at + 1
            continue
        if (ftype in ("LSC", "HSC") and slope is None
                and _NUMBER_RE.fullmatch(tokens[i])):
            # A custom shelf's own slope, written right after its type:
            # "ON LSC 10.8 dB Fc 300 Hz Gain 5.0 dB".
            slope = float(tokens[i])
            i += 1
            if i < len(tokens) and tokens[i].lower() == "db":
                i += 1
            continue
        if ftype is None and token.isalpha():
            # A word where the filter type goes: say what a type may be rather
            # than only that this one was unknown.
            return None, (f"unknown filter type {tokens[i]!r} (the types are "
                          f"{_TYPE_LIST})")
        return None, f"unknown word {tokens[i]!r}"
    if ftype is None:
        return None, f"no filter type (the types are {_TYPE_LIST})"
    if "fc" not in values:
        return None, "no frequency (Fc)"
    q = values.get("q")
    bw = values.get("bw")
    converted = None
    if q is None and bw is not None:
        q = _band_to_q(bw)
        if q is None:
            return None, f"BW {_num(bw)} is not a usable bandwidth"
        converted = bw
    if q is None or q <= 0:
        q = _DEFAULT_Q[ftype]
    band = {"type": ftype, "fc": values["fc"], "gain": values.get("gain", 0.0),
            "q": q, "on": enabled}
    if converted is not None:
        # The file's own bandwidth, kept as provenance: the import result
        # states the BW→Q conversion instead of looking exact.
        band["bw"] = converted
    if slope is not None:
        # The shelf's own slope in dB/octave, kept for the same reason: it is
        # the one number of the band the rendered shelf cannot carry.
        band["slope"] = slope
    if t60 is not None:
        band["t60"] = t60
    return band, None


def _parse_graphic(body):
    """A ``GraphicEQ:`` band list as ``(pairs, error)``.

    The bands are READ as ``frequency gain`` pairs, never assumed: Peace writes
    31 of them and its own frequency ladder, AutoEQ writes 10 and a different
    one, and neither is a fixed table this app could hard-code. A group that is
    not exactly one pair is an error naming the group — the alternative is a
    curve with a band silently missing from the middle of it.
    """
    pairs = []
    for group in str(body or "").split(";"):
        # A trailing ";" (Peace writes one) leaves an empty group, and the
        # Hz/dB suffixes a hand-written file may carry mean nothing here.
        tokens = [t for t in group.replace(",", " ").split()
                  if t.lower() not in _UNITS]
        if not tokens:
            continue
        if len(tokens) != 2:
            return None, (f"expected one 'frequency gain' pair, got "
                          f"{' '.join(tokens)!r}")
        fc, gain = _number(tokens[0]), _number(tokens[1])
        if fc is None or gain is None:
            return None, (f"expected one 'frequency gain' pair, got "
                          f"{' '.join(tokens)!r}")
        pairs.append((fc, gain))
    if not pairs:
        return None, "no frequency/gain pairs"
    return pairs, None


# Peace's other export shape: ONE `FilterCurve:` line whose whole body is
# `name="value"` pairs — `f0`…`fN` the frequencies, `v0`…`vN` the value at each
# point (paired BY INDEX: there is no separate gain list), then the curve's
# meta attributes. A real Peace export starts at 10 Hz and ends at 18.9 kHz on
# no clean ladder, so the points are read, never assumed.
_CURVE_RE = re.compile(r"^\s*filtercurve\s*:(.*)$", re.IGNORECASE)
_ATTR_RE = re.compile(r'([A-Za-z_][A-Za-z0-9_]*)\s*=\s*"([^"]*)"')
_POINT_RE = re.compile(r"^([fv])(\d+)$")

# The curve's own attributes, lower-cased. Everything else an APO curve file
# carries is reported rather than applied.
_CURVE_META = ("filterlength", "interpolatelin", "interpolationmethod", "preamp")

_MISSING_QUOTE = 'expected name="value"'


def _parse_curve(body):
    """One ``FilterCurve:`` body as ``(points, meta, unknown, error)``.

    ``points`` is ``[(frequency_hz, gain_db), …]`` sorted by frequency, ``meta``
    the curve's own attributes (``filterlength``, ``interpolatelin``,
    ``interpolationmethod``, ``preamp``), ``unknown`` the attribute pairs this
    module has no meaning for. Every failure — a value that is not a number, a
    ``vN`` with no ``fN`` or the reverse, a token that is not a pair at all — is
    an error naming the attribute: a curve is audio, and a silently different
    one is worse than a refusal.
    """
    text = str(body or "")
    freqs, gains = {}, {}
    meta, unknown = {}, []
    pos = 0
    for m in _ATTR_RE.finditer(text):
        stray = text[pos:m.start()].strip()
        if stray:
            return None, meta, unknown, f"{_MISSING_QUOTE}, got {stray!r}"
        pos = m.end()
        name, value = m.group(1), m.group(2)
        key = name.lower()
        point = _POINT_RE.match(key)
        if point:
            try:
                number = float(value)
            except ValueError:
                return None, meta, unknown, f'{name}="{value}" is not a number'
            (freqs if point.group(1) == "f" else gains)[int(point.group(2))] = number
        elif key in _CURVE_META:
            meta[key] = value
        else:
            unknown.append(f'{name}="{value}"')
    stray = text[pos:].strip()
    if stray:
        return None, meta, unknown, f"{_MISSING_QUOTE}, got {stray!r}"
    if not freqs:
        return None, meta, unknown, 'no points (f0/v0 …)'
    orphans = sorted(set(gains) - set(freqs))
    if orphans:
        return None, meta, unknown, (f"v{orphans[0]} has no f{orphans[0]} to pair "
                                     "it with")
    unpaired = sorted(set(freqs) - set(gains))
    if unpaired:
        return None, meta, unknown, (f"f{unpaired[0]} has no v{unpaired[0]} to pair "
                                     "it with")
    points = [(freqs[i], gains[i]) for i in sorted(freqs)]
    return points, meta, unknown, None


def _curve_band_q(freqs, i):
    """The Q for point *i* of a curve ladder, from its own neighbours.

    A ``FilterCurve`` is a sampled curve, not a band list: the spacing between
    its points is what says how wide each band has to be for the chain to trace
    it (a 50-point 10 Hz–19 kHz Peace curve is a ~1/4-octave ladder, where the
    octave-wide Q 1.41 the ``GraphicEQ`` conversion uses would smear every point
    into its neighbours). The ends use the one spacing they have.
    """
    lo = freqs[max(0, i - 1)]
    hi = freqs[min(len(freqs) - 1, i + 1)]
    if hi <= lo:
        return _GRAPHICEQ_Q         # a one-point (or degenerate) curve
    q = _band_to_q(math.log2(hi / lo) / 2.0)
    return q if q else _GRAPHICEQ_Q


def parse_apo(text, name=""):
    """An Equalizer APO / Peace profile as a structured profile.

    Returns ``{"name", "preamp_db", "filters": [{type, fc, gain, q, on}],
    "unsupported": [str], "errors": [str], "notes": [str], "empty": bool}``.
    Filters keep the FILE's order — the chain that comes out has to sound like
    the chain the user wrote — and an OFF filter stays in the list (with ``on``
    False) so the UI can show what was skipped. ``Filter N: ON None`` is APO's
    own empty slot: not a band, not a loss, skipped entirely.

    ``unsupported`` names every line that was IGNORED without changing the
    curve: ``Include:`` (a profile that pulls in another file cannot be
    reproduced from one text block), other APO constructs, a Peace banner, a
    band this module understands and deliberately does not render (``AP``,
    ``IIR``), and every ``If:`` that opens a conditional block. The bands
    INSIDE such a block are not applied either — APO evaluates the condition
    against its own variables and this app does not model them, so anything
    that MIGHT apply is left out and the count of skipped lines is in the
    notes; a band that cannot be evaluated must not be applied as though it
    were unconditional. ``errors`` names every line that carries a band this
    module could not read, with its line number, and the caller must REFUSE
    those rather than import the remains — a curve missing the band that failed
    to parse is a different
    curve. ``empty`` is True when the file held no filter and no preamp at all,
    which is a profile that exports the audio unchanged, never a flat curve
    standing in for one. ``Channel: all`` is not a loss: it means "every
    channel", which is what the rendered chain does.
    """
    profile = {"name": str(name or ""), "preamp_db": 0.0, "filters": [],
               "unsupported": [], "errors": [], "notes": [], "empty": False}
    graphic_bands = 0
    slope_shelves = 0
    bw_filters = 0
    t60_filters = 0
    curve_points = 0
    curve_qs = []
    curve_meta = {}
    conditional_depth = 0
    conditional_lines = 0
    # A BOM can arrive from a FILE and from a paste through the API: Windows
    # tools write one, and an un-stripped "\ufeffPreamp:" is a line this parser
    # would otherwise have to call unknown — losing the preamp without saying
    # why. Every encoding is handled here, on the text.
    body = str(text or "").lstrip("\ufeff")
    for lineno, raw in enumerate(body.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith(";"):
            continue  # comments and blank lines carry nothing to render
        m = _COND_RE.match(line)
        if m:
            word = m.group(1).lower()
            if word == "endif":
                if conditional_depth:
                    conditional_depth -= 1
                else:
                    profile["unsupported"].append(
                        f"line {lineno}: {line} — an EndIf: with no If: to end")
            elif word == "if":
                # APO evaluates this against its OWN variables (sample rate,
                # channel count, device name, user variables) and this app does
                # not model them — so the block is REPORTED and the bands inside
                # it are NOT applied. Applying them unconditionally would change
                # the sound of a profile that never asked for them, and an
                # expression evaluator is not what this app is.
                conditional_depth += 1
                profile["unsupported"].append(
                    f"line {lineno}: {line} — a conditional block: this app does "
                    "not evaluate APO's expressions, so the bands inside it are "
                    "not applied")
            elif conditional_depth:
                # ElseIf:/Else: are another branch of the block already named.
                pass
            else:
                profile["unsupported"].append(
                    f"line {lineno}: {line} — an Else:/ElseIf: with no If: block")
            continue
        if conditional_depth and (_PREAMP_RE.match(line) or _FILTER_RE.match(line)
                                  or _GRAPHIC_RE.match(line) or _CURVE_RE.match(line)):
            conditional_lines += 1
            continue
        if re.match(r"^channel\s*:\s*all\s*$", line, re.IGNORECASE):
            continue
        m = _PREAMP_RE.match(line)
        if m:
            value = _number(m.group(1))
            if value is None:
                profile["errors"].append(
                    f"line {lineno}: {line} — Preamp needs a number, got "
                    f"{m.group(1).strip()!r}")
            else:
                profile["preamp_db"] = value
            continue
        m = _FILTER_RE.match(line)
        if m:
            parsed, why = _parse_filter(m.group(1))
            if why:
                profile["errors"].append(f"line {lineno}: {line} — {why}")
            elif isinstance(parsed, str):
                # "none" is APO's own empty slot — nothing to render, nothing
                # lost. Any OTHER string is the sentence naming a filter that
                # was understood and deliberately NOT rendered (AP, IIR): the
                # line is named and reported, never guessed at.
                if parsed != "none":
                    profile["unsupported"].append(
                        f"line {lineno}: {line} — {parsed}")
            else:
                if parsed["type"] in ("LSC", "HSC") or "slope" in parsed:
                    slope_shelves += 1
                if "bw" in parsed:
                    bw_filters += 1
                if "t60" in parsed:
                    t60_filters += 1
                profile["filters"].append(parsed)
            continue
        m = _GRAPHIC_RE.match(line)
        if m:
            pairs, why = _parse_graphic(m.group(1))
            if pairs is None:
                profile["errors"].append(f"line {lineno}: {line} — {why}")
            else:
                for fc, gain in pairs:
                    profile["filters"].append(
                        {"type": "PK", "fc": fc, "gain": gain,
                         "q": _GRAPHICEQ_Q, "on": True})
                    graphic_bands += 1
            continue
        m = _CURVE_RE.match(line)
        if m:
            points, meta, unknown, why = _parse_curve(m.group(1))
            if points is None:
                # The line is one long attribute list: the sentence names the
                # attribute that failed instead of echoing a kilobyte of it.
                profile["errors"].append(f"line {lineno}: FilterCurve — {why}")
            else:
                curve_meta.update(meta)
                profile["unsupported"].extend(unknown)
                freqs = [fc for fc, _ in points]
                for i, (fc, gain) in enumerate(points):
                    profile["filters"].append(
                        {"type": "PK", "fc": fc, "gain": gain,
                         "q": _curve_band_q(freqs, i), "on": True})
                curve_points += len(points)
                curve_qs.extend(_curve_band_q(freqs, i) for i in range(len(points)))
                if meta.get("preamp") is not None:
                    value = _number(meta["preamp"])
                    if value is None:
                        profile["errors"].append(
                            f'line {lineno}: FilterCurve — Preamp="{meta["preamp"]}" '
                            "is not a number")
                    else:
                        profile["preamp_db"] = value
            continue
        profile["unsupported"].append(line)
    # How many of the file's own numbers the renderers have to clamp (see the
    # bounds above): the bands that are actually rendered first, then the
    # preamp. An OFF band is rendered by neither path, so it is not counted.
    clamped = sum(1 for band in profile["filters"]
                  if band.get("on", True) and _out_of_bounds(band))
    if not -PREAMP_LIMIT_DB <= profile["preamp_db"] <= PREAMP_LIMIT_DB:
        clamped += 1
    if graphic_bands:
        profile["notes"].append(
            "GraphicEQ bands are rendered as peaking filters with Q 1.41 "
            "(AutoEQ's own conversion); %d band(s)" % graphic_bands)
    if slope_shelves:
        # APO's LSC/HSC take a slope, and "LS 6dB"/"LS 12dB" name one; ffmpeg's
        # shelf takes a Q. The slope and the Q are two spellings of the same
        # shaped curve (APO's own shelf Q is what its slope produces), but the
        # file's own value is not carried through, so the result says so instead
        # of looking exact.
        profile["notes"].append(
            "LSC/HSC shelves, and the fixed 'LS 6dB'/'LS 12dB' spellings, are "
            "rendered as ffmpeg's LS/HS shelf with the profile's own shelf Q; "
            "APO's slope parameter itself is not carried over; %d filter(s)"
            % slope_shelves)
    if bw_filters:
        profile["notes"].append(
            "BW (bandwidth in octaves) was converted to the Q ffmpeg's filters "
            "take, with APO's own conversion; %d filter(s)" % bw_filters)
    if t60_filters:
        profile["notes"].append(
            "Modal filters are rendered as peaking filters — APO's own "
            "reference lists Modal in its peaking-filter row — and the filter's "
            "T60 target (the decay, not the band's shape) is not carried over; "
            "%d filter(s)" % t60_filters)
    if conditional_lines:
        profile["notes"].append(
            "%d filter line(s) inside If:/ElseIf: blocks were NOT applied: APO "
            "evaluates those blocks against its own variables (sample rate, "
            "channel count, device name, user variables), which this app does "
            "not model, and a band it cannot evaluate must not be applied as "
            "though it were unconditional" % conditional_lines)
    if clamped:
        profile["notes"].append(
            "%d value(s) of this profile are outside the bounds this app "
            "renders within (Fc 20 Hz–20 kHz, gain ±20 dB, Q 0.1–30, preamp "
            "±24 dB — the bounds the in-app player applies) and are clamped to "
            "them when the curve is played or exported; the file's own values "
            "are kept" % clamped)
    if curve_points:
        qs = sorted(curve_qs)
        profile["notes"].append(
            "FilterCurve points are rendered as peaking filters, one per point, "
            "with each band's Q taken from the ladder's own spacing (median "
            "Q %.2f for this curve); the curve is exact AT the file's points "
            "and an approximation between them; %d point(s)"
            % (qs[len(qs) // 2], curve_points))
        lin = str(curve_meta.get("interpolatelin", ""))
        method = str(curve_meta.get("interpolationmethod", ""))
        if lin == "1":
            joins = "straight lines (InterpolateLin=1)"
        elif method:
            joins = ("a %s curve (InterpolateLin=%s, InterpolationMethod=%s)"
                     % (method, lin or "0", method))
        else:
            joins = "a smooth curve (InterpolateLin=%s)" % (lin or "0")
        profile["notes"].append(
            "the file asks APO to join its points with %s, which a chain of "
            "ffmpeg biquads cannot reproduce — those joins are approximated, "
            "not flattened to straight lines or dropped" % joins)
        length = str(curve_meta.get("filterlength", ""))
        if length:
            profile["notes"].append(
                "APO applies this curve by convolution (%s taps, which is a "
                "linear-phase FIR); this app renders minimum-phase peaking "
                "bands instead, so the magnitude is close and the phase is not "
                "the file's" % length)
    if profile["unsupported"]:
        profile["notes"].append(
            "%d line(s) could not be rendered and are listed in 'unsupported'"
            % len(profile["unsupported"]))
    if profile["errors"]:
        profile["notes"].append(
            "%d line(s) carry a band that cannot be read and are listed in "
            "'errors' — applying this profile would change the curve, so an "
            "import of it is refused" % len(profile["errors"]))
    if not profile["filters"] and not profile["preamp_db"]:
        # Explicitly empty, and said out loud: "no filters" and "a flat curve"
        # are different answers, and a user who imported the wrong file needs
        # the first one.
        profile["empty"] = True
        profile["notes"].append(
            "this profile holds no filters — the exported audio is left "
            "unchanged")
    return profile


def _filter_af(filt):
    """One filter as an ffmpeg ``-af`` entry.

    Every number is clamped to the bounds BOTH renderers use (see FC_MIN_HZ):
    the band's own Fc/Gain/Q are what the file wrote, and what the player
    actually renders is what these bounds allow — the export has to be that
    same curve, not a wider or louder one.
    """
    ftype = filt["type"]
    name = _FFMPEG_FILTER[ftype]
    fc = _num(_clamp(filt["fc"], FC_MIN_HZ, FC_MAX_HZ))
    q = _num(_clamp(filt["q"], Q_MIN, Q_MAX))
    gain = _num(_clamp(filt["gain"], -GAIN_LIMIT_DB, GAIN_LIMIT_DB))
    if ftype in ("LP", "HP", "BP", "NO"):
        return f"{name}=f={fc}:t=q:w={q}"
    if name == "equalizer":
        return f"{name}=f={fc}:t=q:w={q}:g={gain}"
    # bass/treble: the gain leads, the corner frequency and the shelf width
    # follow — the same three numbers, the order those filters document.
    return f"{name}=g={gain}:f={fc}:t=q:w={q}"


def apply_refusal(profile):
    """The sentence a profile that parsed WITH ERRORS is refused with, or "".

    Refusing is the conservative half of the choice this module makes for a
    profile that lost a band: the bands that DID parse are not the curve the
    file wrote, and while a warning would leave the app usable, it would also
    let a run bake that different curve into files the user keeps — and let the
    player audition a curve no export of theirs will ever produce. All three
    surfaces use THIS sentence — the import (``import_profile``), the export
    (server/exporter.py) and the player (web/src/lib/eqNodes.ts
    `eqApplyRefusal`, whose words are the editor's banner's) — so a user cannot
    be told three different things about one profile.
    """
    errors = (profile or {}).get("errors") or []
    return f"this profile cannot be applied: {errors[0]}" if errors else ""


def chain(profile):
    """The ffmpeg ``-af`` filters for *profile*, preamp first, file order
    kept and OFF filters skipped. Empty when the profile is a no-op.

    A gain filter set to 0 dB is skipped too: it is transparent, and rendering
    it would multiply every sample of every exported file for nothing.
    ``GraphicEQ`` band lists are full of them.

    A profile that parsed with ERRORS renders nothing at all — it raises, so no
    caller can build a chain out of the bands that happened to parse (see
    ``apply_refusal``).
    """
    if not profile:
        return []
    refusal = apply_refusal(profile)
    if refusal:
        raise ValueError(refusal)
    parts = []
    preamp = _clamp(profile.get("preamp_db") or 0.0, -PREAMP_LIMIT_DB,
                    PREAMP_LIMIT_DB)
    if preamp:
        # The preamp is what keeps a boosted curve from clipping the encoder;
        # a 0 dB one is not emitted at all (same reason as the flat filters).
        parts.append(f"volume={_num(preamp)}dB")
    for filt in profile.get("filters") or ():
        if not filt.get("on", True):
            continue
        if filt["type"] in _GAIN_TYPES and not filt.get("gain"):
            continue
        parts.append(_filter_af(filt))
    return parts


def to_af(profile):
    """The whole profile as the comma-joined ``-af`` string ffmpeg takes."""
    return ",".join(chain(profile))


# ---------------------------------------------------------------------------
# Built-in presets — APO text, so the parser above renders them exactly like an
# imported file and a user can copy one into Peace to tweak it.
# ---------------------------------------------------------------------------

_FLAT_TEXT = """
# Flat: the explicit "no equalizer" choice. Shipped as a preset so the Export
# page can offer it in the same list as the real curves.
"""

# Thin earbuds and small speakers lose the bottom two octaves; a low shelf is
# the standard correction for that (and the shape AutoEQ's own bass shelves
# use). The preamp pays for the boost, so the encoder cannot clip on material
# that was already near full scale.
_BASS_SHELF_TEXT = """
Preamp: -4.0 dB
Filter 1: ON LSC Fc 105 Hz Gain 6.0 dB Q 0.70
"""

# Presence lives at 2-4 kHz. Lifting it brings voices forward on a player
# whose output is mid-shy; the second, wider lift keeps the first one from
# sounding like a single honky peak. The preamp pays for the boosts.
_PRESENCE_TEXT = """
Preamp: -2.5 dB
Filter 1: ON PK Fc 2800 Hz Gain 3.0 dB Q 1.00
Filter 2: ON PK Fc 4500 Hz Gain 1.5 dB Q 1.40
"""

# Night listening: at low volume the ear stops hearing bass and treble, and a
# flat curve sounds thin and harsh. Cutting the bass shelf and the top octave
# while nudging the low mids is that correction, and it also keeps the quiet
# passage quiet instead of inviting the user to turn it up.
_NIGHT_TEXT = """
Filter 1: ON LSC Fc 120 Hz Gain -6.0 dB Q 0.70
Filter 2: ON PK Fc 1800 Hz Gain 1.5 dB Q 1.00
Filter 3: ON HS Fc 6000 Hz Gain -2.0 dB Q 0.70
"""

PRESETS = (
    {"id": "flat", "label": "Flat (no equalizer)", "text": _FLAT_TEXT},
    {"id": "bass_shelf", "label": "Bass shelf (+6 dB below 105 Hz)",
     "text": _BASS_SHELF_TEXT},
    {"id": "presence", "label": "Vocal presence (+3 dB at 2.8 kHz)",
     "text": _PRESENCE_TEXT},
    {"id": "night", "label": "Night / quiet listening", "text": _NIGHT_TEXT},
)

_PRESET_IDS = {p["id"] for p in PRESETS}


def preset_rows():
    """The built-in presets as rows for the API, with their filter chain."""
    rows = []
    for preset in PRESETS:
        profile = parse_apo(preset["text"], preset["id"])
        profile["id"] = preset["id"]
        profile["label"] = preset["label"]
        rows.append(profile)
    return rows


def _preset(eq_id):
    for preset in PRESETS:
        if preset["id"] == eq_id:
            profile = parse_apo(preset["text"], eq_id)
            profile["id"] = eq_id
            profile["label"] = preset["label"]
            return profile
    return None


# ---------------------------------------------------------------------------
# Imported profiles: <music>/.mlo/data/eq/<slug>.txt
# ---------------------------------------------------------------------------

def eq_dir(music_folder=None):
    """The folder holding imported profiles (created on demand)."""
    return os.path.join(app_data_dir(music_folder), EQ_DIRNAME)


def decode_profile(raw):
    """Profile text from the bytes a profile file holds.

    Windows tools write the encodings this has to survive: Equalizer APO and
    Peace save UTF-8 with a BOM, and a profile that travelled through Notepad
    or a forum paste can be UTF-16. Reading UTF-16 bytes as UTF-8 does not
    fail — it produces a NUL-ridden first line that parses as a profile with no
    filters at all, which is the one outcome worse than an error. So the BOM
    decides, a clean UTF-8 read is next, and a Windows code page is the last
    resort (it decodes anything).
    """
    if raw.startswith(codecs.BOM_UTF16_LE) or raw.startswith(codecs.BOM_UTF16_BE):
        return raw.decode("utf-16")
    if raw.startswith(codecs.BOM_UTF8):
        return raw.decode("utf-8-sig")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("cp1252", errors="replace")


def read_profile(path):
    """One profile file as text, decoded per its own encoding."""
    with open(path, "rb") as f:
        return decode_profile(f.read(MAX_PROFILE_BYTES + 1))


def _profile_path(music_folder, stem):
    return os.path.join(eq_dir(music_folder), stem + ".txt")


def _imported_at(path):
    try:
        return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(os.path.getmtime(path)))
    except OSError:
        return ""


def list_profiles(music_folder):
    """Every imported profile, newest first, files that cannot be read skipped.

    A file whose BAND lines cannot be read is still listed, with ``errors``
    filled in: a profile the user imported has to be visibly unusable rather
    than quietly vanish from the list it was imported into.
    """
    folder = eq_dir(music_folder)
    try:
        names = sorted(os.listdir(folder))
    except OSError:
        return []
    rows = []
    for name in names:
        if not name.lower().endswith(".txt"):
            continue
        path = os.path.join(folder, name)
        try:
            text = read_profile(path)
        except OSError:
            continue
        stem = name[:-4]
        profile = parse_apo(text, stem)
        profile["id"] = stem
        profile["label"] = stem
        profile["imported_at"] = _imported_at(path)
        rows.append(profile)
    rows.sort(key=lambda r: (r.get("imported_at") or "", r["id"]), reverse=True)
    return rows


def import_profile(music_folder, name, text):
    """Store one pasted or dropped profile and return its row.

    Re-importing the same name with the same text is a no-op (the file's
    timestamp, and so ``imported_at``, is left alone); with different text the
    stored profile is replaced, which is what re-importing after editing means.
    Raises ValueError with a message the UI can show for a name that is not a
    plain name, text past the size cap, a name that is a built-in preset's, or
    a BAND line the parser cannot read — that last one refuses the whole file
    and names the line, because storing the readable half would export a curve
    the user never wrote.
    """
    raw = str(name or "").strip()
    if not raw:
        raise ValueError("a profile name is required")
    if any(sep in raw for sep in ("/", "\\")) or ".." in raw or "\x00" in raw:
        raise ValueError(f"invalid profile name: {raw!r} — a name cannot contain a path")
    stem = slug_name(raw)
    if not stem:
        raise ValueError(f"invalid profile name: {raw!r}")
    if stem in _PRESET_IDS:
        # Presets and imported profiles share one id space (eq_profile names
        # one of them), so a collision would make the option ambiguous.
        raise ValueError(f"{stem!r} is a built-in preset — pick another name")
    body = str(text or "")
    size = len(body.encode("utf-8"))
    if size > MAX_PROFILE_BYTES:
        raise ValueError(
            "profile is %d KiB — the limit is %d KiB (that is not an Equalizer "
            "APO preset)" % (size // 1024, MAX_PROFILE_BYTES // 1024))
    parsed = parse_apo(body, stem)
    if parsed["errors"]:
        more = len(parsed["errors"]) - 1
        raise ValueError(
            "profile not imported — " + parsed["errors"][0]
            + (f" (and {more} more unreadable line(s))" if more else ""))
    folder = eq_dir(music_folder)
    path = _profile_path(music_folder, stem)
    try:
        os.makedirs(folder, exist_ok=True)
        if os.path.isfile(path):
            with open(path, "rb") as f:
                if decode_profile(f.read(MAX_PROFILE_BYTES + 1)) == body:
                    return _row(path, stem, body)
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(body)
    except OSError as e:
        raise ValueError(f"could not store the profile: {e}")
    return _row(path, stem, body)


def _row(path, stem, text):
    profile = parse_apo(text, stem)
    profile["id"] = stem
    profile["label"] = stem
    profile["imported_at"] = _imported_at(path)
    return profile


def delete_profile(music_folder, eq_id):
    """Drop one imported profile. False when there is no such profile."""
    stem, error = safe_segment(eq_id, "profile id")
    if error:
        raise ValueError(error)
    path = _profile_path(music_folder, stem)
    try:
        os.remove(path)
        return True
    except OSError:
        return False


def find(music_folder, eq_id):
    """The profile *eq_id* names — a built-in preset, else an imported file —
    or None when nothing has that id.

    A saved export config (server.exportconfigs) or a saved default can outlive
    the profile it names — the user deleted or renamed it, or the library moved
    — so a missing profile is a normal answer here and both the caller that
    loads a config and the run itself report it instead of exporting without
    the EQ.
    """
    raw = str(eq_id or "").strip()
    if not raw:
        return None
    preset = _preset(raw)
    if preset is not None:
        return preset
    stem, error = safe_segment(raw, "profile id")
    if error:
        return None
    path = _profile_path(music_folder, stem)
    try:
        text = read_profile(path)
    except OSError:
        return None
    return _row(path, stem, text)


def catalog(music_folder):
    """The whole EQ catalogue for the API: built-in presets, imported
    profiles, and the note the UI shows about the format."""
    return {
        "presets": preset_rows(),
        "profiles": list_profiles(music_folder),
        "note": ("Equalizer APO / Peace profile text: Preamp, Filter lines "
                 "(PK/LS/HS/LP/HP/BP/NO/LSC/HSC and APO's own spellings for "
                 "them — PEQ, Modal, LPQ, HPQ, 'LS 6dB'/'LS 12dB', 'HSC 6 dB', "
                 "ON/OFF, BW Oct instead of Q) and GraphicEQ band lists, in any "
                 "field order, as UTF-8 (with or without a BOM) or UTF-16. "
                 "Include: and any other line is reported as unsupported rather "
                 "than applied — so are AP and IIR filters, and the bands inside "
                 "an If:/ElseIf: block (this app does not evaluate APO's "
                 "expressions); a band line that cannot be read is refused with "
                 "its line number, never imported into a different curve."),
    }


# --------------------------------------------------------------------------- #
# AutoEq — the headphone corrections from github.com/jaakkopasanen/AutoEq
# --------------------------------------------------------------------------- #
# AutoEq publishes one directory per measured headphone under `results/`, and
# each directory holds the SAME correction in the formats this module already
# reads (`<model> ParametricEQ.txt`, `<model> GraphicEQ.txt`, a `.csv`, a
# `.png`) — so "search for my headphones" needs two things and no new format:
#
#   * an INDEX of what has been measured. That is the project's own
#     `results/INDEX.md`, one line per measurement
#     (`- [Sennheiser HD 600](./oratory1990/over-ear/Sennheiser%20HD%20600) by oratory1990`).
#     It is ~850 KiB, so it is fetched ONCE, kept beside the profiles, and
#     re-fetched on a long TTL: the measurement set changes by the week, and a
#     search box that downloads a megabyte per keystroke is unusable.
#   * ONE profile file, fetched by name. `<model> ParametricEQ.txt` is the
#     shape AutoEq's own `ParametricEQ` output has, and the parametric form is
#     what this parser reads natively (a `GraphicEQ` file is the fallback for a
#     model that has only that). No directory listing is needed for it, which
#     keeps the GitHub API — and its rate limit — out of the path entirely.
AUTOEQ_REPO = "jaakkopasanen/AutoEq"
AUTOEQ_BRANCH = "master"
AUTOEQ_RAW = (f"https://raw.githubusercontent.com/{AUTOEQ_REPO}/"
              f"{AUTOEQ_BRANCH}")
AUTOEQ_INDEX_NAME = "autoeq-index.md"
AUTOEQ_INDEX_URL = f"{AUTOEQ_RAW}/results/INDEX.md"
# A month: the index is a file LIST, and the profile itself is always fetched
# fresh, so a stale copy still names the right directories.
AUTOEQ_INDEX_TTL_S = 30 * 24 * 3600
AUTOEQ_INDEX_MAX_BYTES = 4 * 1024 * 1024
# The forms AutoEq writes, best first. `parametric` is the one APO ships and
# the one whose filters survive the round trip through this parser unchanged;
# `graphic` is the band list it is derived FROM, and the fixed-band form is the
# lowest-resolution one — a perfectly good fallback, never a first choice.
AUTOEQ_FORMS = (("parametric", "ParametricEQ"), ("graphic", "GraphicEQ"),
                ("fixed", "FixedBandEQ"))
_AUTOEQ_LINE_RE = re.compile(
    r"^\s*-\s*\[(?P<model>.+?)\]\((?P<path>\./[^)]+)\)\s+by\s+(?P<rest>.+?)\s*$")


def _http_get(url, timeout=30.0, max_bytes=0):
    """One GET as this app. GitHub answers 403 to urllib's default agent."""
    req = urllib.request.Request(url, headers={"User-Agent": "la-musica"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read(max_bytes) if max_bytes else resp.read()


def autoeq_index_path(music_folder=None):
    return os.path.join(app_data_dir(music_folder), AUTOEQ_INDEX_NAME)


def autoeq_index(music_folder=None, refresh=False):
    """AutoEq's index of measured headphones, cached on disk.

    Returns ``{"rows", "fetched_at", "error"}``. A refresh that FAILS falls
    back to the cached copy: a search box that stops working because GitHub
    hiccuped is worse than a list a month old, and ``error`` says what
    happened without hiding that the rows are still usable.
    """
    path = autoeq_index_path(music_folder)
    text, fetched_at = "", 0.0
    try:
        with open(path, "rb") as f:
            text = decode_profile(f.read(AUTOEQ_INDEX_MAX_BYTES))
        fetched_at = os.path.getmtime(path)
    except OSError:
        pass
    fresh = bool(text) and (time.time() - fetched_at) < AUTOEQ_INDEX_TTL_S
    error = ""
    if refresh or not fresh:
        try:
            fetched = decode_profile(_http_get(AUTOEQ_INDEX_URL, timeout=60.0,
                                               max_bytes=AUTOEQ_INDEX_MAX_BYTES))
            if fetched.strip():
                text, fetched_at = fetched, time.time()
                try:
                    os.makedirs(os.path.dirname(path), exist_ok=True)
                    with open(path, "w", encoding="utf-8", newline="\n") as f:
                        f.write(fetched)
                except OSError:
                    # A cache that cannot be written is a slower search, not a
                    # failed one: the rows in hand are already parsed below.
                    pass
        except Exception as e:
            error = f"could not fetch the AutoEq index ({e})"
    return {"rows": parse_autoeq_index(text), "fetched_at": fetched_at,
            "error": error}


def parse_autoeq_index(text):
    """INDEX.md as rows: ``{id, model, source, rig}``.

    ``id`` is the results-relative directory (``<source>/<rig>/<model>``), which
    is what a profile fetch is built from. Lines that do not carry a model and
    a link are skipped: the file's own prose headings are not measurements."""
    rows = []
    for line in str(text or "").splitlines():
        m = _AUTOEQ_LINE_RE.match(line)
        if not m:
            continue
        model = m.group("model").strip()
        rel = unquote(m.group("path").strip()).lstrip("./").strip("/")
        if not model or not rel:
            continue
        # "by oratory1990" (no rig stated) and "by crinacle on 711 in-ear".
        source, _, rig = m.group("rest").strip().partition(" on ")
        rows.append({"id": rel, "model": model, "source": source.strip(),
                     "rig": rig.strip()})
    return rows


def autoeq_search(query, rows, limit=40):
    """The measurements whose model name matches *query*, best first.

    Every word of the query must appear in the model name — "hd 600" is an AND,
    not an OR — and the ranking is exact name, then name starting with the
    query, then where the first word lands. That order is what makes typing
    "hd 600" offer the HD 600 before the twenty models that merely contain
    those characters later on.
    """
    words = [w for w in re.split(r"[^0-9a-z+]+", str(query or "").lower()) if w]
    if not words:
        return []
    joined = " ".join(words)
    scored = []
    for row in rows:
        low = re.sub(r"\s+", " ", row["model"].lower()).strip()
        if not all(w in low for w in words):
            continue
        at = low.find(words[0])
        if low == joined:
            rank = 0
        elif low.startswith(joined):
            rank = 1
        elif at <= 3:
            rank = 2
        else:
            rank = 3
        scored.append(((rank, at, len(low), low), row))
    scored.sort(key=lambda pair: pair[0])
    return [dict(row) for _, row in scored[:max(1, int(limit or 40))]]


def _autoeq_dir(eq_id):
    """The results-relative directory an id names, or (\"\", why not)."""
    raw = unquote(str(eq_id or "").strip()).strip("/")
    parts = raw.split("/")
    if (not raw or "://" in raw or ".." in parts
            or not 2 <= len(parts) <= 4 or not all(p.strip() for p in parts)):
        return "", f"invalid AutoEq id: {eq_id!r}"
    return raw, ""


def autoeq_import(music_folder, eq_id, form="parametric"):
    """Fetch ONE AutoEq correction and store it as a profile; returns its row.

    The requested form is tried first and the others follow, so a model that
    only has a `GraphicEQ` file still imports (the parser converts that band
    list to peaking filters exactly as AutoEq's own parametric output does).
    The stored name is the model's, so re-importing a model the user already
    has REPLACES it — which is what asking for it again means.
    """
    rel, error = _autoeq_dir(eq_id)
    if error:
        raise ValueError(error)
    model = unquote(rel.split("/")[-1])
    order = [f for f in AUTOEQ_FORMS if f[0] == str(form or "").strip().lower()]
    order += [f for f in AUTOEQ_FORMS if f not in order]
    tried = []
    for _, suffix in order:
        name = f"{model} {suffix}.txt"
        url = f"{AUTOEQ_RAW}/results/{quote(f'{rel}/{name}', safe='/')}"
        try:
            text = decode_profile(_http_get(
                url, timeout=30.0, max_bytes=MAX_PROFILE_BYTES * 2))
        except Exception as e:
            tried.append(f"{name}: {e}")
            continue
        if not text.strip():
            tried.append(f"{name}: empty")
            continue
        try:
            return import_profile(music_folder, model, text)
        except ValueError as e:
            raise ValueError(f"{model}: {e}")
    raise ValueError(f"no AutoEq profile could be fetched for {model} "
                     f"({'; '.join(tried[:3])})")
