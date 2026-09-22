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
  audio is worse than a refusal, so ``import_profile`` refuses the whole file
  and the run refuses to apply such a profile from disk.

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

_NUMBER_RE = re.compile(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)")
# The preamp is matched by its NAME, not by "a name followed by a number": a
# "Preamp: high" line must come back as an error naming the line, never fall
# through to the ignored bucket, because the preamp is what keeps a boosted
# curve from clipping.
_PREAMP_RE = re.compile(r"^\s*preamp\s*:\s*(.*)$", re.IGNORECASE)
_FILTER_RE = re.compile(r"^\s*filter\s*\d*\s*:(.*)$", re.IGNORECASE)
_GRAPHIC_RE = re.compile(r"^\s*graphiceq\s*:(.*)$", re.IGNORECASE)

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
    (``Filter 1: ON None``), or None with *error* saying what could not be
    read. The tokens are scanned in the order the file wrote them, so any field
    order works and ``ON``/``OFF`` may sit wherever the writer put it. A token
    this parser does not know is an ERROR, not a loss to report: guessing at it
    (or dropping the band) would apply a curve the user did not write.
    """
    tokens = body.replace(",", " ").split()
    enabled, ftype, values = True, None, {}
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
        if token in _TYPES and ftype is None:
            ftype = _TYPES[token]
            i += 1
            continue
        if token in _UNITS:
            i += 1
            continue
        if token in _KEYS:
            name = _KEYS[token]
            if i + 1 >= len(tokens):
                return None, f"{token.capitalize()} has no value"
            value = _number(tokens[i + 1])
            if value is None:
                return None, (f"{token.capitalize()} needs a number, got "
                              f"{tokens[i + 1]!r}")
            values[name] = value
            i += 2
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
    reproduced from one text block), other APO constructs, a Peace banner.
    ``errors`` names every line that carries a band this module could not read,
    with its line number, and the caller must REFUSE those rather than import
    the remains — a curve missing the band that failed to parse is a different
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
    curve_points = 0
    curve_qs = []
    curve_meta = {}
    # A BOM can arrive from a FILE and from a paste through the API: Windows
    # tools write one, and an un-stripped "\ufeffPreamp:" is a line this parser
    # would otherwise have to call unknown — losing the preamp without saying
    # why. Every encoding is handled here, on the text.
    body = str(text or "").lstrip("\ufeff")
    for lineno, raw in enumerate(body.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith(";"):
            continue  # comments and blank lines carry nothing to render
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
            elif parsed != "none":
                if parsed["type"] in ("LSC", "HSC"):
                    slope_shelves += 1
                if "bw" in parsed:
                    bw_filters += 1
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
    if graphic_bands:
        profile["notes"].append(
            "GraphicEQ bands are rendered as peaking filters with Q 1.41 "
            "(AutoEQ's own conversion); %d band(s)" % graphic_bands)
    if slope_shelves:
        # APO's LSC/HSC take a slope; ffmpeg's shelf takes a Q. The slope and
        # the Q are two spellings of the same shaped curve (APO's own shelf Q
        # is what its slope produces), but the file's own value is not carried
        # through, so the result says so instead of looking exact.
        profile["notes"].append(
            "LSC/HSC shelves are rendered as ffmpeg's LS/HS shelf with the "
            "profile's own shelf Q; APO's slope parameter itself is not "
            "carried over; %d filter(s)" % slope_shelves)
    if bw_filters:
        profile["notes"].append(
            "BW (bandwidth in octaves) was converted to the Q ffmpeg's filters "
            "take, with APO's own conversion; %d filter(s)" % bw_filters)
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
    """One filter as an ffmpeg ``-af`` entry."""
    ftype = filt["type"]
    name = _FFMPEG_FILTER[ftype]
    if ftype in ("LP", "HP", "BP", "NO"):
        return f"{name}=f={_num(filt['fc'])}:t=q:w={_num(filt['q'])}"
    if name == "equalizer":
        return (f"{name}=f={_num(filt['fc'])}:t=q:w={_num(filt['q'])}"
                f":g={_num(filt['gain'])}")
    # bass/treble: the gain leads, the corner frequency and the shelf width
    # follow — the same three numbers, the order those filters document.
    return (f"{name}=g={_num(filt['gain'])}:f={_num(filt['fc'])}"
            f":t=q:w={_num(filt['q'])}")


def chain(profile):
    """The ffmpeg ``-af`` filters for *profile*, preamp first, file order
    kept and OFF filters skipped. Empty when the profile is a no-op.

    A gain filter set to 0 dB is skipped too: it is transparent, and rendering
    it would multiply every sample of every exported file for nothing.
    ``GraphicEQ`` band lists are full of them.
    """
    if not profile:
        return []
    parts = []
    preamp = profile.get("preamp_db") or 0.0
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
                 "(PK/LS/HS/LP/HP/BP/NO/LSC/HSC, ON/OFF, BW instead of Q) and "
                 "GraphicEQ band lists, in any field order, as UTF-8 (with or "
                 "without a BOM) or UTF-16. Include: and any other line is "
                 "reported as unsupported rather than applied; a band line that "
                 "cannot be read is refused with its line number, never "
                 "imported into a different curve."),
    }
