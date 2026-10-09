//! The `analyze` pass: tempo, key and the mood/energy features.
//!
//! This is a port of two librosa passes that dominated the chain's CPU cost
//! (`tools/perf_after_local.json`: "Mood & Energy" 8.95 s and "Key & BPM"
//! 2.39 s of a 20.1 s local run). The owner accepts that the values written to
//! BPM / INITIALKEY / MOOD / ENERGY may drift a little from librosa's — speed
//! wins — but they must stay MUSICALLY correct: a 128 BPM track reads 128, a
//! C# minor track reads C# minor. Every ambiguous choice below is therefore
//! made the way `mlo/audiometa.py` / `mlo/moods.py` make it:
//!
//!   * tempo is autocorrelation tempo estimation over the onset envelope with
//!     a log-normal prior around 120 BPM (octave errors folded into the DJ
//!     range), refined by dynamic-programming beat tracking — a conventional
//!     estimator, never a bare dominant-frequency guess;
//!   * the key is a chroma (a log-frequency filterbank approximating the CQT)
//!     correlated, over all 12 rotations, against the same three published
//!     key profiles and averaged the same way;
//!   * the mood features are `mlo.moods._features`' numbers (RMS/dynamic
//!     range, onset rate, spectral centroid, percussive ratio, tempo, mode);
//!     the verdict itself (`_score`/`_verdict`) is re-derived in Python from
//!     these features, so the verdict vocabulary has exactly one owner.
//!
//! What is approximated, and why it is allowed to be:
//!   * the harmonic/percussive split is a median-filter soft mask over a
//!     magnitude STFT (librosa's hpss with the same kernel size) instead of a
//!     time-domain ISTFT reconstruction — the percussive ENERGY ratio and the
//!     harmonic chroma are what the callers consume;
//!   * the onset envelope is a mel-band spectral flux, not librosa's exact
//!     `onset_strength` reference filter;
//!   * the chroma filterbank is triangular per semitone, not librosa's CQT.
//!
//! Zero dependencies, like the rest of the helper.

use crate::fft::Fft;
use crate::{json_string, round_half_even_f64 as round_half_even};

// The mood knobs, verbatim from `mlo/moods.py` — the same thresholds, so the
// same verdicts come out of the same features.
const RMS_DB_LO: f64 = -35.0;
const RMS_DB_HI: f64 = -12.0;
const ONSET_FLOOR_DB: f64 = -45.0;
const ONSET_RATE_LO: f64 = 0.5;
const ONSET_RATE_HI: f64 = 6.0;
const TEMPO_LO: f64 = 70.0;
const TEMPO_HI: f64 = 170.0;
const CENTROID_LO: f64 = 500.0;
const CENTROID_HI: f64 = 4000.0;
const DYN_RANGE_LO: f64 = 3.0;
const DYN_RANGE_HI: f64 = 20.0;
const AROUSAL_MID: f64 = 0.50;
const VALENCE_MID: f64 = 0.50;
const DREAMY_BRIGHTNESS: f64 = 0.50;
const DARK_CENTROID: f64 = 0.45;
const PARTY_TEMPO: f64 = 128.0;
const PARTY_AROUSAL: f64 = 0.60;
const PARTY_VALENCE: f64 = 0.55;
const PARTY_PERCUSSIVE: f64 = 0.50;
const AGGRESSIVE_AROUSAL: f64 = 0.60;
const AGGRESSIVE_VALENCE: f64 = 0.45;
const AGGRESSIVE_PERCUSSIVE: f64 = 0.45;
const DARK_VALENCE: f64 = 0.35;

const W_AROUSAL_RMS: f64 = 0.40;
const W_AROUSAL_ONSET: f64 = 0.25;
const W_AROUSAL_TEMPO: f64 = 0.25;
const W_AROUSAL_DYN: f64 = 0.10;
const W_VALENCE_BRIGHT: f64 = 0.35;
const W_VALENCE_KEY: f64 = 0.30;
const W_VALENCE_TEMPO: f64 = 0.20;
const W_VALENCE_PERC: f64 = 0.15;
const VALENCE_MAJOR: f64 = 1.00;
const VALENCE_MINOR: f64 = 0.15;
const VALENCE_UNKNOWN_KEY: f64 = 0.50;

const CONF_BASE: f64 = 0.35;
const CONF_SLOPE: f64 = 0.55;
const CONF_NO_KEY: f64 = 0.90;
const CONF_NO_TEMPO: f64 = 0.85;
const CONF_MIN: f64 = 0.05;
const CONF_MAX: f64 = 0.95;

// The three key profiles `mlo/audiometa.py::_detect_key` averages, indexed
// from C. Kept as (major, minor) triples in the module's own order.
const KS_MAJOR: [f64; 12] = [6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88];
const KS_MINOR: [f64; 12] = [6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17];
const TEMPERLEY_MAJOR: [f64; 12] =
    [0.748, 0.060, 0.488, 0.082, 0.670, 0.460, 0.096, 0.715, 0.104, 0.366, 0.057, 0.400];
const TEMPERLEY_MINOR: [f64; 12] =
    [0.712, 0.084, 0.474, 0.618, 0.049, 0.460, 0.105, 0.747, 0.404, 0.067, 0.133, 0.330];
const ALBRECHT_MAJOR: [f64; 12] =
    [0.900, 0.091, 0.211, 0.137, 0.344, 0.382, 0.130, 0.800, 0.099, 0.207, 0.094, 0.306];
const ALBRECHT_MINOR: [f64; 12] =
    [0.938, 0.110, 0.268, 0.513, 0.188, 0.418, 0.086, 0.868, 0.450, 0.108, 0.065, 0.317];
const PROFILES: [(&[f64; 12], &[f64; 12]); 3] = [
    (&KS_MAJOR, &KS_MINOR),
    (&TEMPERLEY_MAJOR, &TEMPERLEY_MINOR),
    (&ALBRECHT_MAJOR, &ALBRECHT_MINOR),
];

/// The engine's key spelling. The tag itself is still rendered by
/// `mlo.audiometa._key_notation` (its flat convention is the user-facing one);
/// this string is the helper's own display, and the numeric `key_index` /
/// `key_minor` beside it are what Python parses back to a tonic.
const SHARP_NAMES: [&str; 12] = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"];

// STFT framing: librosa's defaults for this analysis (n_fft 2048, hop 512).
const N_FFT: usize = 2048;
const HOP: usize = 512;
const N_BINS: usize = N_FFT / 2 + 1;
const N_MELS: usize = 128;
const HPSS_KERNEL: usize = 31;
/// Frames kept for the harmonic/percussive split and the chroma: an hour-long
/// file would otherwise hold a 600 MB magnitude spectrogram. Decimating the
/// frame axis keeps the cost and the memory bounded, and 8000 frames is far
/// more than the key needs.
const MAX_CHROMA_FRAMES: usize = 8000;
/// The chroma/hpss frame stride is at least this, so the median filter's
/// effective window stays close to librosa's 31 frames at hop 512.
const MIN_CHAIN: usize = 4;

// ----------------------------------------------------------------------
// Features
// ----------------------------------------------------------------------

#[derive(Clone)]
pub struct Features {
    pub duration: f64,
    pub rms_db: f64,
    pub dynamic_range_db: f64,
    pub onset_rate: f64,
    pub centroid_hz: f64,
    pub percussive_ratio: f64,
    /// Integer-valued BPM (as Python's `_detect_bpm` returns), or None.
    pub tempo: Option<f64>,
    /// (tonic index from C, is_minor), or None.
    pub key: Option<(usize, bool)>,
}

impl Features {
    fn key_mode(&self) -> Option<&'static str> {
        self.key.map(|(_, minor)| if minor { "minor" } else { "major" })
    }
    fn key_name(&self) -> Option<String> {
        self.key
            .map(|(idx, minor)| format!("{} {}", SHARP_NAMES[idx % 12], if minor { "minor" } else { "major" }))
    }
}

// ----------------------------------------------------------------------
// Formatting helpers
// ----------------------------------------------------------------------

fn fmt_f64(v: f64) -> String {
    if !v.is_finite() {
        return "null".into();
    }
    let mut s = format!("{v}");
    if !s.contains('.') && !s.contains('e') && !s.contains('E') {
        s.push_str(".0");
    }
    s
}

fn fmt_opt(v: Option<f64>) -> String {
    match v {
        Some(x) if x.is_finite() => fmt_f64(x),
        _ => "null".into(),
    }
}

// ----------------------------------------------------------------------
// Small numeric helpers
// ----------------------------------------------------------------------

fn power_to_db(s: f64) -> f64 {
    const AMIN: f64 = 1e-10;
    10.0 * s.max(AMIN).log10()
}

/// numpy's linear-interpolated percentile.
fn percentile(sorted: &[f64], q: f64) -> f64 {
    let n = sorted.len();
    if n == 0 {
        return 0.0;
    }
    if n == 1 {
        return sorted[0];
    }
    let rank = (q / 100.0) * (n - 1) as f64;
    let lo = rank.floor() as usize;
    let hi = rank.ceil() as usize;
    if lo == hi {
        sorted[lo]
    } else {
        sorted[lo] + (rank - lo as f64) * (sorted[hi] - sorted[lo])
    }
}

fn fold_bpm(v: f64) -> f64 {
    if !(v > 0.0) {
        return v;
    }
    let mut x = v;
    while x < 70.0 {
        x *= 2.0;
    }
    while x > 180.0 {
        x /= 2.0;
    }
    x
}

fn pearson(a: &[f64], b: &[f64]) -> f64 {
    let n = a.len() as f64;
    let ma = a.iter().sum::<f64>() / n;
    let mb = b.iter().sum::<f64>() / n;
    let mut num = 0.0;
    let mut da = 0.0;
    let mut db = 0.0;
    for i in 0..a.len() {
        let x = a[i] - ma;
        let y = b[i] - mb;
        num += x * y;
        da += x * x;
        db += y * y;
    }
    let den = (da * db).sqrt();
    if den > 0.0 && num.is_finite() {
        num / den
    } else {
        0.0
    }
}

// ----------------------------------------------------------------------
// Filterbanks
// ----------------------------------------------------------------------

/// Slaney's mel scale, the one librosa uses with `htk=False`.
fn hz_to_mel(f: f64) -> f64 {
    let f_sp = 200.0 / 3.0;
    if f < 1000.0 {
        f / f_sp
    } else {
        let min_log_hz = 1000.0;
        let min_log_mel = min_log_hz / f_sp;
        let logstep = 6.4f64.ln() / 27.0;
        min_log_mel + (f / min_log_hz).ln() / logstep
    }
}

fn mel_to_hz(m: f64) -> f64 {
    let f_sp = 200.0 / 3.0;
    let min_log_hz = 1000.0;
    let min_log_mel = min_log_hz / f_sp;
    let logstep = 6.4f64.ln() / 27.0;
    if m < min_log_mel {
        m * f_sp
    } else {
        min_log_hz * ((m - min_log_mel) * logstep).exp()
    }
}

/// Triangular mel filters as (bin, weight) pairs per band.
fn mel_filterbank(sr: f64) -> Vec<Vec<(usize, f64)>> {
    let mel_min = hz_to_mel(0.0);
    let mel_max = hz_to_mel(sr / 2.0);
    let pts: Vec<f64> = (0..N_MELS + 2)
        .map(|i| {
            let m = mel_min + (mel_max - mel_min) * i as f64 / (N_MELS + 1) as f64;
            mel_to_hz(m) * N_FFT as f64 / sr
        })
        .collect();
    let mut fb = Vec::with_capacity(N_MELS);
    for m in 0..N_MELS {
        let (left, center, right) = (pts[m], pts[m + 1], pts[m + 2]);
        let mut row = Vec::new();
        let lo = left.floor().max(0.0) as usize;
        let hi = (right.ceil() as usize).min(N_BINS - 1);
        for k in lo..=hi {
            let w = if (k as f64) <= center {
                (k as f64 - left) / (center - left).max(1e-9)
            } else {
                (right - k as f64) / (right - center).max(1e-9)
            };
            if w > 0.0 {
                row.push((k, w));
            }
        }
        fb.push(row);
    }
    fb
}

/// A log-frequency chroma map: each FFT bin drops into the one or two nearest
/// semitones with linear weight (a triangular kernel a semitone wide).
fn chroma_filterbank(sr: f64) -> Vec<Vec<(usize, f64)>> {
    const F_MIN: f64 = 32.70319566257483; // C1
    let mut fb = Vec::with_capacity(N_BINS);
    for k in 0..N_BINS {
        let f = k as f64 * sr / N_FFT as f64;
        if f < F_MIN || f > sr / 2.0 {
            fb.push(Vec::new());
            continue;
        }
        let p = 12.0 * (f / F_MIN).log2();
        let lo = p.floor();
        let mut row = Vec::new();
        for s in [lo, lo + 1.0] {
            let w = 1.0 - (p - s).abs();
            if w > 0.0 {
                let idx = ((s as i64).rem_euclid(12)) as usize;
                row.push((idx, w));
            }
        }
        fb.push(row);
    }
    fb
}

// ----------------------------------------------------------------------
// HPSS + chroma
// ----------------------------------------------------------------------

/// Sliding median along the time axis (per frequency bin), edges clamped.
fn median_time(mag: &[f32], frames: usize, bins: usize, k: usize) -> Vec<f32> {
    let half = k / 2;
    let mut out = vec![0f32; frames * bins];
    let mut buf = vec![0f32; k];
    for f in 0..bins {
        for t in 0..frames {
            for i in 0..k {
                let off = (t + i) as isize - half as isize;
                let idx = off.clamp(0, frames as isize - 1) as usize;
                buf[i] = mag[idx * bins + f];
            }
            buf.select_nth_unstable_by(half, |a, b| a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal));
            out[t * bins + f] = buf[half];
        }
    }
    out
}

/// Sliding median along the frequency axis (per frame), edges clamped.
fn median_freq(mag: &[f32], frames: usize, bins: usize, k: usize) -> Vec<f32> {
    let half = k / 2;
    let mut out = vec![0f32; frames * bins];
    let mut buf = vec![0f32; k];
    for t in 0..frames {
        let row = &mag[t * bins..t * bins + bins];
        for f in 0..bins {
            for i in 0..k {
                let off = (f + i) as isize - half as isize;
                let idx = off.clamp(0, bins as isize - 1) as usize;
                buf[i] = row[idx];
            }
            buf.select_nth_unstable_by(half, |a, b| a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal));
            out[t * bins + f] = buf[half];
        }
    }
    out
}

/// (percussive energy ratio, per-frame chroma vectors of the harmonic part).
fn hpss_and_chroma(
    mag: &[f32],
    frames: usize,
    chain: usize,
    sr: f64,
) -> (f64, Vec<[f64; 12]>) {
    if frames == 0 {
        return (0.0, Vec::new());
    }
    let mut kt = (HPSS_KERNEL as f64 / chain as f64).round() as usize;
    if kt < 3 {
        kt = 3;
    }
    if kt % 2 == 0 {
        kt += 1;
    }
    let harm = median_time(mag, frames, N_BINS, kt);
    let perc = median_freq(mag, frames, N_BINS, HPSS_KERNEL);

    let chroma_fb = chroma_filterbank(sr);
    let mut chromas = Vec::with_capacity(frames);
    let mut total = 0.0f64;
    let mut perc_energy = 0.0f64;
    for t in 0..frames {
        let mut ch = [0.0f64; 12];
        for k in 0..N_BINS {
            let s = mag[t * N_BINS + k] as f64;
            let e = s * s;
            total += e;
            let h = harm[t * N_BINS + k] as f64;
            let p = perc[t * N_BINS + k] as f64;
            let hp = h * h;
            let pp = p * p;
            let denom = hp + pp + 1e-12;
            perc_energy += e * (pp / denom); // percussive soft mask
            let sh = s * (hp / denom); // harmonic soft mask
            for &(idx, w) in &chroma_fb[k] {
                ch[idx] += w * sh;
            }
        }
        // chroma_cqt normalizes each frame to its peak (norm=inf).
        let mx = ch.iter().cloned().fold(0.0f64, f64::max);
        if mx > 0.0 {
            for v in ch.iter_mut() {
                *v /= mx;
            }
        }
        chromas.push(ch);
    }
    let ratio = if total > 0.0 { (perc_energy / total).clamp(0.0, 1.0) } else { 0.0 };
    (ratio, chromas)
}

/// The key: mean+median chroma correlated against every rotation of the three
/// published profiles, averaged exactly as `_detect_key` averages them.
fn detect_key(chromas: &[[f64; 12]]) -> Option<(usize, bool)> {
    if chromas.is_empty() {
        return None;
    }
    let n = chromas.len();
    let mut mean = [0.0f64; 12];
    let mut median = [0.0f64; 12];
    for i in 0..12 {
        let sum: f64 = chromas.iter().map(|c| c[i]).sum();
        mean[i] = sum / n as f64;
        let mut col: Vec<f64> = chromas.iter().map(|c| c[i]).collect();
        col.sort_by(|a, b| a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal));
        median[i] = col[n / 2];
    }
    let mut vec = [0.0f64; 12];
    let mut total = 0.0;
    for i in 0..12 {
        vec[i] = mean[i] + median[i];
        total += vec[i];
    }
    if !(total > 0.0) || !vec.iter().all(|v| v.is_finite()) {
        return None;
    }
    for v in vec.iter_mut() {
        *v /= total;
    }

    let mut best = (-2.0f64, 0usize, false);
    for idx in 0..12 {
        // np.roll(vec, -idx): rotated[j] = vec[(j + idx) % 12].
        let rotated: Vec<f64> = (0..12).map(|j| vec[(j + idx) % 12]).collect();
        for minor in [false, true] {
            let mut acc = 0.0;
            for (maj, min) in PROFILES.iter() {
                let prof: &[f64; 12] = if minor { min } else { maj };
                acc += pearson(&rotated, prof);
            }
            let avg = acc / PROFILES.len() as f64;
            if avg > best.0 {
                best = (avg, idx, minor);
            }
        }
    }
    Some((best.1, best.2))
}

// ----------------------------------------------------------------------
// Tempo
// ----------------------------------------------------------------------

/// Autocorrelation tempo with a log-normal prior around 120 BPM, plus a
/// parabolic peak refinement, then folded into the 70-180 DJ range.
fn autocorr_tempo(env: &[f64], fps: f64) -> Option<f64> {
    let n = env.len();
    if n < 8 {
        return None;
    }
    let mean = env.iter().sum::<f64>() / n as f64;
    let x: Vec<f64> = env.iter().map(|v| v - mean).collect();
    let l_min = ((60.0 * fps / 200.0).round() as usize).max(1);
    let l_max = ((60.0 * fps / 40.0).ceil() as usize).min(n - 1);
    if l_max <= l_min {
        return None;
    }
    let score_at = |lag: usize| -> f64 {
        if lag >= n {
            return f64::NEG_INFINITY;
        }
        let mut r = 0.0;
        for t in lag..n {
            r += x[t] * x[t - lag];
        }
        r /= (n - lag) as f64;
        let bpm = 60.0 * fps / lag as f64;
        let w = (-0.5 * (bpm / 120.0).log2().powi(2)).exp();
        r * w
    };
    let mut best_lag = 0usize;
    let mut best = f64::NEG_INFINITY;
    for lag in l_min..=l_max {
        let s = score_at(lag);
        if s > best {
            best = s;
            best_lag = lag;
        }
    }
    if best_lag == 0 || !best.is_finite() {
        return None;
    }
    // No positive autocorrelation anywhere means no periodic structure at all
    // (silence, a steady drone): a tempo would be fabricated, and the callers
    // already treat a missing tempo as "weigh the other features only".
    if best <= 0.0 {
        return None;
    }
    // Parabolic interpolation between the peak and its neighbours.
    let mut frac = best_lag as f64;
    if best_lag > 1 && best_lag + 1 < n {
        let y0 = score_at(best_lag - 1);
        let y1 = best;
        let y2 = score_at(best_lag + 1);
        let denom = y0 - 2.0 * y1 + y2;
        if denom.abs() > 1e-12 {
            let d = (0.5 * (y0 - y2) / denom).clamp(-0.5, 0.5);
            frac = best_lag as f64 + d;
        }
    }
    Some(fold_bpm(60.0 * fps / frac))
}

/// Ellis-style dynamic-programming beat tracking over the onset envelope.
/// Returns the median beat rate, folded, or None when too few beats were
/// found for a stable interval.
fn beat_track(env: &[f64], bpm: f64, fps: f64) -> Option<f64> {
    let period = 60.0 * fps / bpm;
    if period < 2.0 || (env.len() as f64) < 4.0 * period {
        return None;
    }
    let sigma = period / 32.0;
    let n = env.len();
    // Gaussian smoothing of the onset envelope = the local beat score.
    let rad = (3.0 * sigma).ceil().max(1.0) as usize;
    let mut kernel = Vec::with_capacity(2 * rad + 1);
    let mut ksum = 0.0;
    for i in -(rad as isize)..=(rad as isize) {
        let w = (-0.5 * (i as f64 / sigma).powi(2)).exp();
        kernel.push(w);
        ksum += w;
    }
    for w in kernel.iter_mut() {
        *w /= ksum;
    }
    let mut localscore = vec![0.0f64; n];
    for t in 0..n {
        let mut acc = 0.0;
        for (i, w) in kernel.iter().enumerate() {
            let j = t as isize + i as isize - rad as isize;
            if j >= 0 && (j as usize) < n {
                acc += env[j as usize] * w;
            } else {
                acc += 0.0;
            }
        }
        localscore[t] = acc;
    }

    const TIGHTNESS: f64 = 100.0;
    let mut cum = vec![0.0f64; n];
    let mut back = vec![usize::MAX; n];
    for i in 0..n {
        let lo = (i as f64 - 2.0 * period).ceil().max(0.0) as usize;
        let hi = i as f64 - 0.5 * period;
        let mut best = f64::NEG_INFINITY;
        let mut bj = usize::MAX;
        if hi >= 0.0 {
            let hi = (hi as usize).min(i);
            for j in lo..hi {
                let ratio = (i as f64 - j as f64) / period;
                let penalty = TIGHTNESS * ratio.ln().powi(2);
                let s = cum[j] - penalty;
                if s > best {
                    best = s;
                    bj = j;
                }
            }
        }
        if bj == usize::MAX {
            cum[i] = localscore[i];
        } else {
            cum[i] = localscore[i] + best;
            back[i] = bj;
        }
    }

    let mut last = 0usize;
    let mut best = f64::NEG_INFINITY;
    for i in 0..n {
        if cum[i] > best {
            best = cum[i];
            last = i;
        }
    }
    let mut beats = Vec::new();
    let mut cur = last;
    loop {
        beats.push(cur);
        if back[cur] == usize::MAX {
            break;
        }
        cur = back[cur];
    }
    beats.reverse();
    if beats.len() < 4 {
        return None;
    }
    let mut iv: Vec<f64> = beats.windows(2).map(|w| (w[1] - w[0]) as f64).collect();
    iv.sort_by(|a, b| a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal));
    let med = iv[iv.len() / 2];
    if !(med > 0.0) {
        return None;
    }
    Some(fold_bpm(60.0 * fps / med))
}

/// The analyzer's tempo: the two estimators combined the way `_detect_bpm`
/// combines them — averaged when they agree within 8 %, otherwise the measured
/// beat-interval median wins (it is measured, not interpolated).
fn estimate_tempo(env: &[f64], fps: f64) -> Option<f64> {
    let ac = autocorr_tempo(env, fps)?;
    match beat_track(env, ac, fps) {
        Some(bt) => {
            let hi = ac.max(bt);
            if (ac - bt).abs() / hi <= 0.08 {
                Some(round_half_even((ac + bt) / 2.0))
            } else {
                Some(round_half_even(bt))
            }
        }
        None => Some(round_half_even(ac)),
    }
}

// ----------------------------------------------------------------------
// Onset envelope + rate
// ----------------------------------------------------------------------

/// librosa's `peak_pick` with `onset_detect`'s defaults.
fn peak_pick(x: &[f64], delta: f64, wait: usize) -> usize {
    let (pre_max, post_max, pre_avg, post_avg) = (3usize, 3usize, 3usize, 5usize);
    let n = x.len();
    let mut count = 0usize;
    let mut last: isize = -(1 << 30);
    for i in 0..n {
        let a = i.saturating_sub(pre_max);
        let b = (i + post_max).min(n - 1);
        let mut mx = f64::NEG_INFINITY;
        for v in &x[a..=b] {
            if *v > mx {
                mx = *v;
            }
        }
        if x[i] < mx {
            continue;
        }
        let a = i.saturating_sub(pre_avg);
        let b = (i + post_avg).min(n - 1);
        let avg = x[a..=b].iter().sum::<f64>() / (b - a + 1) as f64;
        if x[i] < avg + delta {
            continue;
        }
        if (i as isize) < last + wait as isize {
            continue;
        }
        count += 1;
        last = i as isize;
    }
    count
}

// ----------------------------------------------------------------------
// The analysis
// ----------------------------------------------------------------------

pub fn analyze(samples: &[f32], sr: u32) -> Features {
    let sr = sr as f64;
    let n = samples.len();
    let duration = n as f64 / sr;

    let hann: Vec<f64> = (0..N_FFT)
        .map(|i| 0.5 - 0.5 * (2.0 * std::f64::consts::PI * i as f64 / N_FFT as f64).cos())
        .collect();
    let frames = if n == 0 { 0 } else { 1 + n / HOP };
    let chain = (frames.div_ceil(MAX_CHROMA_FRAMES)).max(MIN_CHAIN);

    let mel_fb = mel_filterbank(sr);
    let fft = Fft::new(N_FFT);
    let mut re = vec![0.0f64; N_FFT];
    let mut im = vec![0.0f64; N_FFT];
    let mut mag = vec![0.0f64; N_BINS];

    let mut rms_vals: Vec<f64> = Vec::with_capacity(frames);
    let mut centroid_sum = 0.0;
    let mut centroid_cnt = 0usize;
    let mut mel_prev: Option<Vec<f64>> = None;
    let mut onset: Vec<f64> = Vec::with_capacity(frames);
    let mut dec_frames = 0usize;
    let mut dec_mag: Vec<f32> = Vec::with_capacity(frames.div_ceil(chain) * N_BINS);

    for t in 0..frames {
        let base = (t * HOP) as isize - (N_FFT / 2) as isize;
        let mut sumsq = 0.0;
        for i in 0..N_FFT {
            let idx = base + i as isize;
            let x = if idx >= 0 && (idx as usize) < n {
                samples[idx as usize] as f64
            } else {
                0.0
            };
            sumsq += x * x;
            re[i] = x * hann[i];
            im[i] = 0.0;
        }
        rms_vals.push((sumsq / N_FFT as f64).sqrt());
        fft.forward(&mut re, &mut im);
        for k in 0..N_BINS {
            mag[k] = (re[k] * re[k] + im[k] * im[k]).sqrt();
        }

        // Spectral centroid: each frame normalized by the L1 norm (librosa's
        // `spectral_centroid` divides by the SUM of the magnitudes).
        let norm = mag.iter().sum::<f64>();
        if norm > 0.0 {
            let mut c = 0.0;
            for k in 0..N_BINS {
                c += (k as f64 * sr / N_FFT as f64) * (mag[k] / norm);
            }
            centroid_sum += c;
            centroid_cnt += 1;
        }

        // Mel-band dB + half-wave-rectified flux: the onset envelope.
        let mut meldb = vec![0.0f64; N_MELS];
        for (m, row) in mel_fb.iter().enumerate() {
            let mut p = 0.0;
            for &(k, w) in row {
                let v = mag[k];
                p += w * v * v;
            }
            meldb[m] = power_to_db(p);
        }
        let mx = meldb.iter().cloned().fold(f64::MIN, f64::max);
        for v in meldb.iter_mut() {
            if *v < mx - 80.0 {
                *v = mx - 80.0;
            }
        }
        if let Some(prev) = &mel_prev {
            let mut f = 0.0;
            for m in 0..N_MELS {
                let d = meldb[m] - prev[m];
                if d > 0.0 {
                    f += d;
                }
            }
            onset.push(f);
        } else {
            onset.push(0.0);
        }
        mel_prev = Some(meldb);

        if t % chain == 0 {
            dec_mag.extend(mag.iter().map(|v| *v as f32));
            dec_frames += 1;
        }
    }

    // --- features ---
    let rms_mean = if rms_vals.is_empty() {
        0.0
    } else {
        rms_vals.iter().sum::<f64>() / rms_vals.len() as f64
    };
    let rms_db = 20.0 * rms_mean.max(1e-9).log10();
    let dyn_db = if rms_vals.len() >= 4 {
        let mut sorted = rms_vals.clone();
        sorted.sort_by(|a, b| a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal));
        let p95 = percentile(&sorted, 95.0);
        let p05 = percentile(&sorted, 5.0);
        20.0 * (p95.max(1e-9) / p05.max(1e-9)).log10()
    } else {
        0.0
    };
    let centroid_hz = if centroid_cnt > 0 {
        centroid_sum / centroid_cnt as f64
    } else {
        0.0
    };

    let fps = sr / HOP as f64;
    let tempo = estimate_tempo(&onset, fps);

    // Onset rate only above the silence floor (see `ONSET_FLOOR_DB`).
    let onset_rate = if rms_db >= ONSET_FLOOR_DB && !onset.is_empty() {
        let mx = onset.iter().cloned().fold(0.0f64, f64::max);
        if mx > 1e-10 {
            let norm: Vec<f64> = onset.iter().map(|v| v / mx).collect();
            peak_pick(&norm, 0.07, 3) as f64 / duration.max(1e-6)
        } else {
            0.0
        }
    } else {
        0.0
    };

    let (percussive_ratio, chromas) = hpss_and_chroma(&dec_mag, dec_frames, chain, sr);
    let key = detect_key(&chromas);

    Features {
        duration: round_dec(duration, 3),
        rms_db: round_dec(rms_db, 2),
        dynamic_range_db: round_dec(dyn_db, 2),
        onset_rate: round_dec(onset_rate, 3),
        centroid_hz: round_dec(centroid_hz, 1),
        percussive_ratio: round_dec(percussive_ratio, 3),
        tempo,
        key,
    }
}

fn round_dec(v: f64, d: i32) -> f64 {
    if !v.is_finite() {
        return v;
    }
    let f = 10f64.powi(d);
    round_half_even(v * f) / f
}

/// `librosa.effects.trim(y, top_db=35)` at this module's framing: drop the
/// lead-in/lead-out frames whose RMS is more than 35 dB below the loudest
/// frame. `mlo.audiometa.detect_key_bpm` trims before it analyses, so the
/// helper's `--trim` must too — near-silent padding skews both the onset
/// envelope and the chroma.
pub fn trim(samples: &[f32]) -> Vec<f32> {
    let n = samples.len();
    if n == 0 {
        return Vec::new();
    }
    let frames = 1 + n / HOP;
    let mut rms = vec![0.0f64; frames];
    let mut peak = 0.0f64;
    for t in 0..frames {
        let base = (t * HOP) as isize - (N_FFT / 2) as isize;
        let mut s = 0.0;
        for i in 0..N_FFT {
            let idx = base + i as isize;
            if idx >= 0 && (idx as usize) < n {
                let x = samples[idx as usize] as f64;
                s += x * x;
            }
        }
        rms[t] = (s / N_FFT as f64).sqrt();
        if rms[t] > peak {
            peak = rms[t];
        }
    }
    let threshold = peak * 10f64.powf(-35.0 / 20.0);
    let first = rms.iter().position(|v| *v > threshold);
    let last = rms.iter().rposition(|v| *v > threshold);
    match (first, last) {
        (Some(a), Some(b)) => {
            let start = a * HOP;
            let end = ((b + 1) * HOP).min(n);
            if end > start {
                samples[start..end].to_vec()
            } else {
                Vec::new()
            }
        }
        _ => Vec::new(),
    }
}

// ----------------------------------------------------------------------
// Score / verdict (ported from mlo.moods)
// ----------------------------------------------------------------------

fn norm01(v: f64, lo: f64, hi: f64) -> f64 {
    if lo == hi {
        return 0.0;
    }
    ((v - lo) / (hi - lo)).clamp(0.0, 1.0)
}

fn weighted(pairs: &[(f64, Option<f64>)]) -> f64 {
    let mut num = 0.0;
    let mut tot = 0.0;
    for &(w, v) in pairs {
        if let Some(v) = v {
            num += w * v;
            tot += w;
        }
    }
    if tot <= 0.0 {
        0.0
    } else {
        num / tot
    }
}

fn score(f: &Features) -> (f64, f64, f64) {
    let tempo_s = f.tempo.map(|t| norm01(t, TEMPO_LO, TEMPO_HI));
    let rms_s = norm01(f.rms_db, RMS_DB_LO, RMS_DB_HI);
    let onset_s = norm01(f.onset_rate, ONSET_RATE_LO, ONSET_RATE_HI);
    let dyn_s = norm01(f.dynamic_range_db, DYN_RANGE_LO, DYN_RANGE_HI);
    let bright_s = norm01(f.centroid_hz, CENTROID_LO, CENTROID_HI);
    let perc_s = f.percussive_ratio.clamp(0.0, 1.0);
    let key_s = match f.key {
        Some((_, false)) => VALENCE_MAJOR,
        Some((_, true)) => VALENCE_MINOR,
        None => VALENCE_UNKNOWN_KEY,
    };
    let arousal = weighted(&[
        (W_AROUSAL_RMS, Some(rms_s)),
        (W_AROUSAL_ONSET, Some(onset_s)),
        (W_AROUSAL_TEMPO, tempo_s),
        (W_AROUSAL_DYN, Some(1.0 - dyn_s)),
    ]);
    let valence = weighted(&[
        (W_VALENCE_BRIGHT, Some(bright_s)),
        (W_VALENCE_KEY, Some(key_s)),
        (W_VALENCE_TEMPO, tempo_s),
        (W_VALENCE_PERC, Some(perc_s)),
    ]);
    let margin = (arousal - AROUSAL_MID).abs().max((valence - VALENCE_MID).abs());
    let mut conf = (CONF_BASE + CONF_SLOPE * (margin * 2.0).min(1.0)).min(CONF_MAX);
    if f.key.is_none() {
        conf *= CONF_NO_KEY;
    }
    if f.tempo.is_none() {
        conf *= CONF_NO_TEMPO;
    }
    (arousal, valence, conf.max(CONF_MIN))
}

pub fn verdict(f: &Features) -> (&'static str, f64, f64, f64) {
    let (a, v, conf) = score(f);
    let bright = norm01(f.centroid_hz, CENTROID_LO, CENTROID_HI);
    let perc = f.percussive_ratio.clamp(0.0, 1.0);
    let tempo = f.tempo.unwrap_or(0.0);
    let mood = if a >= PARTY_AROUSAL && v >= PARTY_VALENCE && perc >= PARTY_PERCUSSIVE && tempo >= PARTY_TEMPO {
        "party"
    } else if a >= AGGRESSIVE_AROUSAL && v < AGGRESSIVE_VALENCE && perc >= AGGRESSIVE_PERCUSSIVE {
        "aggressive"
    } else if a >= AROUSAL_MID && v >= VALENCE_MID {
        "happy"
    } else if a >= AROUSAL_MID {
        "energetic"
    } else if bright >= DREAMY_BRIGHTNESS && v >= DARK_VALENCE {
        "dreamy"
    } else if v < DARK_VALENCE && bright < DARK_CENTROID {
        "dark"
    } else if v < VALENCE_MID {
        "sad"
    } else {
        "calm"
    };
    (mood, a, v, conf)
}

// ----------------------------------------------------------------------
// JSON
// ----------------------------------------------------------------------

pub fn report(feats: Option<&Features>, failed: bool, reason: &str) -> String {
    match feats {
        None => format!(
            "{{\"bpm\":null,\"key\":null,\"key_index\":null,\"key_minor\":null,\
             \"mood\":null,\"energy\":null,\"valence\":null,\"confidence\":null,\
             \"features\":null,\"failed\":{failed},\"reason\":{}}}",
            json_string(reason)
        ),
        Some(f) => {
            let (mood, energy, valence, conf) = verdict(f);
            let key_name = match f.key_name() {
                Some(k) => json_string(&k),
                None => "null".to_string(),
            };
            let (ki, km) = match f.key {
                Some((idx, minor)) => (idx.to_string(), minor.to_string()),
                None => ("null".to_string(), "null".to_string()),
            };
            let mode = match f.key_mode() {
                Some(m) => json_string(m),
                None => "null".to_string(),
            };
            let feats_json = format!(
                "{{\"duration\":{},\"rms_db\":{},\"dynamic_range_db\":{},\
                 \"onset_rate\":{},\"centroid_hz\":{},\"percussive_ratio\":{},\
                 \"tempo\":{},\"key\":{}}}",
                fmt_f64(f.duration),
                fmt_f64(f.rms_db),
                fmt_f64(f.dynamic_range_db),
                fmt_f64(f.onset_rate),
                fmt_f64(f.centroid_hz),
                fmt_f64(f.percussive_ratio),
                fmt_opt(f.tempo),
                mode,
            );
            format!(
                "{{\"bpm\":{},\"key\":{},\"key_index\":{},\"key_minor\":{},\
                 \"mood\":{},\"energy\":{},\"valence\":{},\"confidence\":{},\
                 \"features\":{},\"failed\":{failed},\"reason\":{}}}",
                fmt_opt(f.tempo),
                key_name,
                ki,
                km,
                json_string(mood),
                fmt_f64(round_dec(energy, 3)),
                fmt_f64(round_dec(valence, 3)),
                fmt_f64(round_dec(conf, 3)),
                feats_json,
                json_string(reason),
            )
        }
    }
}