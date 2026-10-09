//! mlo-audio — la musica's native audio-analysis helper.
//!
//! The first pass it owns is DYNAMIC RANGE (`mlo.dr.measure_track_detailed`):
//! the dr.loudness-war.info block math, done here so a decoded track never has
//! to travel into Python. The helper spawns the app's own ffmpeg, reads its
//! `pcm_f32le` stdout, and prints ONE json line on stdout:
//!
//!     {"dr": 7, "reason": "", "failed": false}
//!     {"dr": null, "reason": "silent, or shorter than two 3-second blocks — no DR", "failed": false}
//!
//! The wording of every `reason` mirrors `mlo/dr.py` exactly, because the
//! Python side hands it straight to the run log and the tests match on it.
//!
//! Zero dependencies: the arithmetic is the standard library's, and the helper
//! has to build offline in a container stage and on a dev machine alike.
//!
//! The second pass it owns is `analyze` — the tempo, key and mood/energy
//! features that used to be two librosa passes (`mlo.audiometa.detect_key_bpm`
//! and `mlo.moods.classify`). It spawns the same ffmpeg, decodes pcm_f32le
//! mono itself, does the DSP here and prints ONE json line:
//!
//!     {"bpm":128.0,"key":"C# minor","key_index":1,"key_minor":true,
//!      "mood":"energetic","energy":0.72,"valence":0.55,"confidence":0.6,
//!      "features":{...},"failed":false,"reason":""}
//!
//! `key_index` / `key_minor` let Python map the key back to the (tonic, minor)
//! tuple its public functions return; `features` carries the exact dict
//! `mlo.moods._features` returns, and Python re-derives the verdict from it so
//! the mood vocabulary stays owned by `mlo.moods`.
//!
//! Usage:
//!     mlo-audio dr --ffmpeg <ffmpeg> --input <track> --channels N
//!                  [--rate 44100] [--block-seconds 3] [--threads N]
//!     mlo-audio analyze --ffmpeg <ffmpeg> --input <track>
//!                  [--sr 22050] [--max-seconds N] [--trim]

mod analysis;
mod fft;

use std::fmt::Write as _;
use std::io::{Read, Write as _};
use std::process::{Command, Stdio};

const SAMPLE_BYTES: usize = 4; // pcm_f32le
const DEFAULT_RATE: u32 = 44100;
const DEFAULT_BLOCK_SECONDS: f64 = 3.0;
const READ_SIZE: usize = 1 << 20;
/// How much of ffmpeg's stderr is kept for one log line.
const TAIL_LIMIT: usize = 160;

/// The ffmpeg child, built with CREATE_NO_WINDOW on Windows.
///
/// The Python engine spawns this helper itself with CREATE_NO_WINDOW
/// (mlo/subproc.py) so the helper owns no console; an ffmpeg launched WITHOUT
/// the flag would allocate one of its own and flash a terminal window in the
/// packaged desktop app.
fn ffmpeg_cmd(exe: &str, args: &[String]) -> Command {
    let mut cmd = Command::new(exe);
    cmd.args(args)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        const CREATE_NO_WINDOW: u32 = 0x0800_0000;
        cmd.creation_flags(CREATE_NO_WINDOW);
    }
    cmd
}

struct Opts {
    ffmpeg: String,
    input: String,
    channels: usize,
    rate: u32,
    block_seconds: f64,
    threads: u32,
}

/// `analyze`'s arguments (see `analysis.rs` for the pipeline they drive).
struct AnalyzeOpts {
    ffmpeg: String,
    input: String,
    sr: u32,
    max_seconds: f64,
    trim: bool,
}

const DEFAULT_ANALYZE_SR: u32 = 22050;

/// The usage line both failure shapes are built from.
const USAGE: &str = "mlo-audio dr|analyze --ffmpeg <exe> --input <file>";

fn parse_dr(argv: &[String]) -> Result<Opts, String> {
    let mut o = Opts {
        ffmpeg: String::new(),
        input: String::new(),
        channels: 0,
        rate: DEFAULT_RATE,
        block_seconds: DEFAULT_BLOCK_SECONDS,
        threads: 0,
    };
    let mut i = 0;
    while i < argv.len() {
        let key = argv[i].as_str();
        let val = || -> Result<String, String> {
            argv.get(i + 1)
                .cloned()
                .ok_or_else(|| format!("{key} needs a value"))
        };
        match key {
            "--ffmpeg" => o.ffmpeg = val()?,
            "--input" => o.input = val()?,
            "--channels" => o.channels = val()?.parse().map_err(|_| "bad --channels".to_string())?,
            "--rate" => o.rate = val()?.parse().map_err(|_| "bad --rate".to_string())?,
            "--block-seconds" => {
                o.block_seconds = val()?.parse().map_err(|_| "bad --block-seconds".to_string())?
            }
            "--threads" => o.threads = val()?.parse().map_err(|_| "bad --threads".to_string())?,
            other => return Err(format!("unknown option {other}")),
        }
        i += 2;
    }
    if o.ffmpeg.is_empty() || o.input.is_empty() {
        return Err("--ffmpeg and --input are required".into());
    }
    if o.channels < 1 {
        return Err("--channels must be >= 1 (the caller probes it)".into());
    }
    Ok(o)
}

fn parse_analyze(argv: &[String]) -> Result<AnalyzeOpts, String> {
    let mut o = AnalyzeOpts {
        ffmpeg: String::new(),
        input: String::new(),
        sr: DEFAULT_ANALYZE_SR,
        max_seconds: 0.0,
        trim: false,
    };
    let mut i = 0;
    while i < argv.len() {
        let key = argv[i].as_str();
        let val = || -> Result<String, String> {
            argv.get(i + 1)
                .cloned()
                .ok_or_else(|| format!("{key} needs a value"))
        };
        match key {
            "--ffmpeg" => o.ffmpeg = val()?,
            "--input" => o.input = val()?,
            "--sr" => o.sr = val()?.parse().map_err(|_| "bad --sr".to_string())?,
            "--max-seconds" => {
                o.max_seconds = val()?.parse().map_err(|_| "bad --max-seconds".to_string())?
            }
            "--trim" => {
                o.trim = true;
                i += 1;
                continue;
            }
            other => return Err(format!("unknown option {other}")),
        }
        i += 2;
    }
    if o.ffmpeg.is_empty() || o.input.is_empty() {
        return Err("--ffmpeg and --input are required".into());
    }
    if o.sr < 1000 {
        return Err("--sr must be >= 1000".into());
    }
    Ok(o)
}

/// The meter's decode: first audio stream, float32, resampled, stdout.
fn decode_argv(o: &Opts) -> Vec<String> {
    let mut v = vec![
        o.ffmpeg.clone(),
        "-loglevel".into(),
        "fatal".into(),
        "-nostdin".into(),
    ];
    if o.threads > 0 {
        v.push("-threads".into());
        v.push(o.threads.to_string());
    }
    v.extend([
        "-i".into(),
        o.input.clone(),
        "-map".into(),
        "0:a:0".into(),
        "-c:a".into(),
        "pcm_f32le".into(),
        "-ar".into(),
        o.rate.to_string(),
        "-f".into(),
        "f32le".into(),
        "-".into(),
    ]);
    v
}

fn f32_at(bytes: &[u8], frame: usize, channels: usize, ch: usize) -> f32 {
    let off = (frame * channels + ch) * SAMPLE_BYTES;
    let b = [
        bytes[off],
        bytes[off + 1],
        bytes[off + 2],
        bytes[off + 3],
    ];
    f32::from_le_bytes(b)
}

/// One whole block's (peak, rms) per channel, in the meter's own units.
fn block_metrics(block: &[u8], channels: usize) -> Option<(Vec<f64>, Vec<f64>)> {
    let frame_bytes = SAMPLE_BYTES * channels;
    let usable = block.len() - block.len() % frame_bytes;
    if usable == 0 {
        return None;
    }
    let frames = usable / frame_bytes;
    let mut peaks = vec![0.0f64; channels];
    let mut sums = vec![0.0f64; channels];
    for f in 0..frames {
        for c in 0..channels {
            let x = f32_at(&block[..usable], f, channels, c) as f64;
            let a = x.abs();
            if a > peaks[c] {
                peaks[c] = a;
            }
            sums[c] += x * x;
        }
    }
    let rms: Vec<f64> = sums
        .iter()
        .map(|s| (2.0 * s / frames as f64).sqrt())
        .collect();
    Some((peaks, rms))
}

/// The meter's steps in the meter's order (see `mlo/dr.py::value_from_blocks`).
fn value_from_blocks(peaks: &[Vec<f64>], rms: &[Vec<f64>]) -> (Option<i64>, String) {
    let blocks = peaks.len();
    if blocks < 2 {
        return (
            None,
            "silent, or shorter than two 3-second blocks - no DR".into(),
        );
    }
    let channels = peaks[0].len();
    let rms_count = std::cmp::max(1, (blocks as f64 * 0.2).floor() as usize);
    let mut per_channel = Vec::with_capacity(channels);
    for c in 0..channels {
        let mut pk: Vec<f64> = (0..blocks).map(|b| peaks[b][c]).collect();
        pk.sort_by(|a, b| b.partial_cmp(a).unwrap_or(std::cmp::Ordering::Equal));
        let second_peak = pk[1];
        let mut r: Vec<f64> = (0..blocks).map(|b| rms[b][c]).collect();
        r.sort_by(|a, b| b.partial_cmp(a).unwrap_or(std::cmp::Ordering::Equal));
        let top = &r[..rms_count.min(r.len())];
        let rms_top = (top.iter().map(|v| v * v).sum::<f64>() / top.len() as f64).sqrt();
        per_channel.push(-(rms_top / second_peak).log10() * 20.0);
    }
    let dr = per_channel.iter().sum::<f64>() / channels as f64;
    if !dr.is_finite() {
        return (None, "no measurable signal (silent track)".into());
    }
    if dr > 0.0 && dr < 40.0 {
        return (Some(round_half_even(dr)), String::new());
    }
    (None, format!("measured DR {dr:.1} is outside the meter's 0-40 range"))
}

/// Python's `round()`: ties go to the even integer.
fn round_half_even(v: f64) -> i64 {
    let floor = v.floor();
    let frac = v - floor;
    let n = if (frac - 0.5).abs() < f64::EPSILON {
        if (floor as i64) % 2 == 0 {
            floor
        } else {
            floor + 1.0
        }
    } else {
        v.round()
    };
    n as i64
}

/// The float-returning half of the same rule (see `round_half_even`).
pub(crate) fn round_half_even_f64(v: f64) -> f64 {
    let floor = v.floor();
    let frac = v - floor;
    if frac == 0.5 {
        if (floor as i64) % 2 == 0 {
            floor
        } else {
            floor + 1.0
        }
    } else {
        v.round()
    }
}

pub(crate) fn json_string(s: &str) -> String {
    let mut out = String::with_capacity(s.len() + 2);
    out.push('"');
    for ch in s.chars() {
        match ch {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if (c as u32) < 0x20 => {
                let _ = write!(out, "\\u{:04x}", c as u32);
            }
            c => out.push(c),
        }
    }
    out.push('"');
    out
}

fn report(dr: Option<i64>, reason: &str, failed: bool) -> String {
    let drs = match dr {
        Some(v) => v.to_string(),
        None => "null".into(),
    };
    format!(
        "{{\"dr\":{drs},\"reason\":{},\"failed\":{failed}}}",
        json_string(reason)
    )
}

fn tail(text: &str) -> String {
    text.lines()
        .rev()
        .map(str::trim)
        .find(|l| !l.is_empty())
        .map(|l| l.chars().take(TAIL_LIMIT).collect())
        .unwrap_or_else(|| "no output".into())
}

fn run_dr(o: &Opts) -> String {
    let mut child = match ffmpeg_cmd(&o.ffmpeg, &decode_argv(o)[1..]).spawn() {
        Ok(c) => c,
        Err(e) => return report(None, &format!("ffmpeg could not be started: {e}"), true),
    };
    // Drain stderr on its own thread: a full stderr pipe would stall ffmpeg
    // while this thread is blocked reading stdout.
    let mut err_pipe = child.stderr.take();
    let err_handle = std::thread::spawn(move || {
        let mut s = String::new();
        if let Some(p) = err_pipe.as_mut() {
            let _ = p.read_to_string(&mut s);
        }
        s
    });
    let mut out = match child.stdout.take() {
        Some(s) => s,
        None => return report(None, "ffmpeg gave no output stream", true),
    };

    let block_samples = std::cmp::max(1, (o.block_seconds * o.rate as f64) as usize);
    let block_bytes = block_samples * SAMPLE_BYTES * o.channels;
    let mut peaks: Vec<Vec<f64>> = Vec::new();
    let mut rms: Vec<Vec<f64>> = Vec::new();
    let mut pending: Vec<u8> = Vec::with_capacity(block_bytes + READ_SIZE);
    let mut buf = vec![0u8; READ_SIZE];
    loop {
        match out.read(&mut buf) {
            Ok(0) => break,
            Ok(n) => {
                pending.extend_from_slice(&buf[..n]);
                while pending.len() >= block_bytes {
                    let rest = pending.split_off(block_bytes);
                    let block = std::mem::replace(&mut pending, rest);
                    if let Some((p, r)) = block_metrics(&block, o.channels) {
                        peaks.push(p);
                        rms.push(r);
                    }
                }
            }
            Err(e) => {
                let _ = child.kill();
                return report(None, &format!("ffmpeg could not decode it: {e}"), true);
            }
        }
    }
    // The last block of a track is nearly always short, and the meter counts
    // it: only a missing block is missing.
    if !pending.is_empty() {
        if let Some((p, r)) = block_metrics(&pending, o.channels) {
            peaks.push(p);
            rms.push(r);
        }
    }
    let status = child.wait();
    let stderr = err_handle.join().unwrap_or_default();
    if let Ok(st) = status {
        if !st.success() {
            return report(
                None,
                &format!("ffmpeg could not decode it: {}", tail(&stderr)),
                true,
            );
        }
    }
    let (dr, reason) = value_from_blocks(&peaks, &rms);
    report(dr, &reason, false)
}

/// `analyze`'s decode: first audio stream, mono float32 at the analysis rate.
fn analyze_decode_argv(o: &AnalyzeOpts) -> Vec<String> {
    let mut v = vec![
        o.ffmpeg.clone(),
        "-loglevel".into(),
        "fatal".into(),
        "-nostdin".into(),
        "-i".into(),
        o.input.clone(),
        "-map".into(),
        "0:a:0".into(),
        "-ac".into(),
        "1".into(),
        "-ar".into(),
        o.sr.to_string(),
    ];
    if o.max_seconds > 0.0 {
        v.push("-t".into());
        v.push(format!("{}", o.max_seconds));
    }
    v.extend([
        "-c:a".into(),
        "pcm_f32le".into(),
        "-f".into(),
        "f32le".into(),
        "-".into(),
    ]);
    v
}

fn run_analyze(o: &AnalyzeOpts) -> String {
    let mut child = match ffmpeg_cmd(&o.ffmpeg, &analyze_decode_argv(o)[1..]).spawn() {
        Ok(c) => c,
        Err(e) => {
            return analysis::report(None, true, &format!("ffmpeg could not be started: {e}"))
        }
    };
    let mut err_pipe = child.stderr.take();
    let err_handle = std::thread::spawn(move || {
        let mut s = String::new();
        if let Some(p) = err_pipe.as_mut() {
            let _ = p.read_to_string(&mut s);
        }
        s
    });
    let mut out = match child.stdout.take() {
        Some(s) => s,
        None => return analysis::report(None, true, "ffmpeg gave no output stream"),
    };
    let mut bytes = Vec::new();
    if let Err(e) = out.read_to_end(&mut bytes) {
        let _ = child.kill();
        return analysis::report(None, true, &format!("ffmpeg could not decode it: {e}"));
    }
    let status = child.wait();
    let stderr = err_handle.join().unwrap_or_default();
    if let Ok(st) = status {
        if !st.success() {
            return analysis::report(
                None,
                true,
                &format!("ffmpeg could not decode it: {}", tail(&stderr)),
            );
        }
    }
    let mut samples: Vec<f32> = Vec::with_capacity(bytes.len() / SAMPLE_BYTES);
    for ch in bytes.chunks_exact(SAMPLE_BYTES) {
        samples.push(f32::from_le_bytes([ch[0], ch[1], ch[2], ch[3]]));
    }
    drop(bytes);
    if samples.is_empty() {
        return analysis::report(None, true, "ffmpeg produced no audio samples");
    }
    if o.trim {
        samples = analysis::trim(&samples);
        if samples.is_empty() {
            return analysis::report(None, true, "no audible signal after trimming silence");
        }
    }
    let feats = analysis::analyze(&samples, o.sr);
    analysis::report(Some(&feats), false, "")
}

fn main() {
    let argv: Vec<String> = std::env::args().skip(1).collect();
    let line = match argv.first().map(String::as_str) {
        Some("dr") => match parse_dr(&argv[1..]) {
            Ok(o) => run_dr(&o),
            Err(e) => report(None, &e, true),
        },
        Some("analyze") => match parse_analyze(&argv[1..]) {
            Ok(o) => run_analyze(&o),
            Err(e) => analysis::report(None, true, &e),
        },
        other => report(
            None,
            &format!(
                "unknown command {:?} — usage: {USAGE}",
                other.unwrap_or("")
            ),
            true,
        ),
    };
    let mut stdout = std::io::stdout();
    let _ = stdout.write_all(line.as_bytes());
    let _ = stdout.write_all(b"\n");
}
