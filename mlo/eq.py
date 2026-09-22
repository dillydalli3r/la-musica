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
by Peace and by hand use all of those. Anything this module cannot render is
reported in ``unsupported`` rather than dropped, because a profile that
silently loses its Convolution/Include half is not the curve the user asked
for.

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

import math
import os
import re
import time

from mlo.paths import app_data_dir

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
_PREAMP_RE = re.compile(r"^\s*preamp\s*:\s*(-?\d+(?:\.\d+)?)", re.IGNORECASE)
_FILTER_RE = re.compile(r"^\s*filter\s*\d*\s*:(.*)$", re.IGNORECASE)
_GRAPHIC_RE = re.compile(r"^\s*graphiceq\s*:(.*)$", re.IGNORECASE)
_BAND_RE = re.compile(r"([-+]?(?:\d+(?:\.\d*)?|\.\d+))\s*([-+]?(?:\d+(?:\.\d*)?|\.\d+))")

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
    """APO's ``BW`` (bandwidth in octaves) as the Q ffmpeg's filters take."""
    bw = float(bandwidth_octaves)
    if bw <= 0:
        return None
    return math.sqrt(2 ** bw) / (2 ** bw - 1)


def _parse_filter(body):
    """One ``Filter N: …`` body as a filter dict, or None when unreadable.

    The tokens are scanned in the order the file wrote them, so any field
    order works and ``ON``/``OFF`` may sit wherever the writer put it. A token
    this parser does not know means the filter is NOT what the user saved, so
    the line is rejected (the caller reports it) instead of being guessed at.
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
        if token in _TYPES and ftype is None:
            ftype = _TYPES[token]
            i += 1
            continue
        if token in _UNITS:
            i += 1
            continue
        if token in _KEYS:
            if i + 1 >= len(tokens):
                return None
            value = _number(tokens[i + 1])
            if value is None:
                return None
            values[_KEYS[token]] = value
            i += 2
            continue
        return None
    if ftype is None or "fc" not in values:
        return None
    q = values.get("q")
    if q is None and values.get("bw") is not None:
        q = _band_to_q(values["bw"])
    if q is None or q <= 0:
        q = _DEFAULT_Q[ftype]
    return {"type": ftype, "fc": values["fc"], "gain": values.get("gain", 0.0),
            "q": q, "on": enabled}


def parse_apo(text, name=""):
    """An Equalizer APO / Peace profile as a structured profile.

    Returns ``{"name", "preamp_db", "filters": [{type, fc, gain, q, on}],
    "unsupported": [str], "notes": [str]}``. Filters keep the FILE's order —
    the chain that comes out has to sound like the chain the user wrote — and
    an OFF filter stays in the list (with ``on`` False) so the UI can show
    what was skipped.

    ``unsupported`` names every line that was not rendered: ``Include:`` (a
    profile that pulls in another file cannot be reproduced from one text
    block), other APO constructs, and lines that are malformed. ``Channel:
    all`` is the exception — it means "every channel", which is what the
    rendered chain does, so it is not a loss.
    """
    profile = {"name": str(name or ""), "preamp_db": 0.0, "filters": [],
               "unsupported": [], "notes": []}
    graphic_bands = 0
    for raw in str(text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith(";"):
            continue  # comments and blank lines carry nothing to render
        if re.match(r"^channel\s*:\s*all\s*$", line, re.IGNORECASE):
            continue
        m = _PREAMP_RE.match(line)
        if m:
            profile["preamp_db"] = float(m.group(1))
            continue
        m = _FILTER_RE.match(line)
        if m:
            parsed = _parse_filter(m.group(1))
            if parsed is None:
                profile["unsupported"].append(line)
            else:
                profile["filters"].append(parsed)
            continue
        m = _GRAPHIC_RE.match(line)
        if m:
            for fc, gain in _BAND_RE.findall(m.group(1)):
                profile["filters"].append(
                    {"type": "PK", "fc": float(fc), "gain": float(gain),
                     "q": _GRAPHICEQ_Q, "on": True})
                graphic_bands += 1
            continue
        profile["unsupported"].append(line)
    if graphic_bands:
        profile["notes"].append(
            "GraphicEQ bands are rendered as peaking filters with Q 1.41 "
            "(AutoEQ's own conversion); %d band(s)" % graphic_bands)
    if profile["unsupported"]:
        profile["notes"].append(
            "%d line(s) could not be rendered and are listed in 'unsupported'"
            % len(profile["unsupported"]))
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
# Flat: the explicit "no equaliser" choice. Shipped as a preset so the Export
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
    {"id": "flat", "label": "Flat (no equaliser)", "text": _FLAT_TEXT},
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


def slugify(name):
    """The file name a profile is stored under: the user's name reduced to ONE
    safe path segment. Letters, digits, ``-``, ``_`` and ``.`` survive; every
    other character becomes ``_``."""
    text = re.sub(r"[^0-9A-Za-z._ -]+", "_", str(name or "").strip())
    text = re.sub(r"\s+", "_", text)
    text = re.sub(r"_+", "_", text).strip("._-")
    return text[:60]


def _safe_id(eq_id):
    """(*id*, None) when *eq_id* is a usable file stem, else (None, error).

    The id comes from a URL and from a user-editable config value, so anything
    that could name a file outside the eq folder is REFUSED rather than
    sanitized: a caller that asked for "../../config" gets told no instead of
    silently reading or deleting something in the data dir.
    """
    raw = str(eq_id or "").strip()
    if not raw:
        return None, "no profile id given"
    if any(sep in raw for sep in ("/", "\\")) or ".." in raw or "\x00" in raw:
        return None, f"invalid profile id: {raw!r}"
    stem = slugify(raw)
    if not stem:
        return None, f"invalid profile id: {raw!r}"
    return stem, None


def _profile_path(music_folder, stem):
    return os.path.join(eq_dir(music_folder), stem + ".txt")


def _imported_at(path):
    try:
        return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(os.path.getmtime(path)))
    except OSError:
        return ""


def list_profiles(music_folder):
    """Every imported profile, newest first, unreadable files skipped."""
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
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                text = f.read(MAX_PROFILE_BYTES + 1)
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
    """Store one pasted profile and return its row.

    Re-importing the same name with the same text is a no-op (the file's
    timestamp, and so ``imported_at``, is left alone); with different text the
    stored profile is replaced, which is what re-importing after editing means.
    Raises ValueError with a message the UI can show for a name that is not a
    plain name, text past the size cap, or a name that is a built-in preset's.
    """
    raw = str(name or "").strip()
    if not raw:
        raise ValueError("a profile name is required")
    if any(sep in raw for sep in ("/", "\\")) or ".." in raw or "\x00" in raw:
        raise ValueError(f"invalid profile name: {raw!r} — a name cannot contain a path")
    stem = slugify(raw)
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
    folder = eq_dir(music_folder)
    path = _profile_path(music_folder, stem)
    try:
        os.makedirs(folder, exist_ok=True)
        if os.path.isfile(path):
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                if f.read() == body:
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
    stem, error = _safe_id(eq_id)
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

    A saved export default can outlive the profile it names (the user deleted
    it, or the library moved), so a missing profile is a normal answer here and
    the run reports it instead of exporting without the EQ.
    """
    raw = str(eq_id or "").strip()
    if not raw:
        return None
    preset = _preset(raw)
    if preset is not None:
        return preset
    stem, error = _safe_id(raw)
    if error:
        return None
    path = _profile_path(music_folder, stem)
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            text = f.read(MAX_PROFILE_BYTES + 1)
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
                 "(PK/LS/HS/LP/HP/BP/NO/LSC/HSC) and GraphicEQ band lists. "
                 "Include: and any other line is reported as unsupported "
                 "rather than applied."),
    }
