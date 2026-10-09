//! A radix-2 FFT, the only transform the analyzer needs.
//!
//! Zero dependencies: `mlo-audio` builds offline from rustc + cargo alone (see
//! `rust/Cargo.toml`), and a Cooley-Tukey FFT is a page of arithmetic the
//! standard library already has everything for. The twiddles are precomputed
//! once per FFT size, so a whole spectrogram costs no trig calls.

use std::f64::consts::PI;

pub struct Fft {
    n: usize,
    // tw[k] = exp(-2*pi*i*k/n) for k in 0..n/2, indexed by k*step per stage.
    tw_re: Vec<f64>,
    tw_im: Vec<f64>,
}

impl Fft {
    pub fn new(n: usize) -> Fft {
        assert!(n.is_power_of_two(), "FFT size must be a power of two");
        let mut tw_re = Vec::with_capacity(n / 2);
        let mut tw_im = Vec::with_capacity(n / 2);
        for k in 0..n / 2 {
            let ang = -2.0 * PI * (k as f64) / (n as f64);
            tw_re.push(ang.cos());
            tw_im.push(ang.sin());
        }
        Fft { n, tw_re, tw_im }
    }

    /// In-place forward transform; `re`/`im` are `n` complex samples.
    pub fn forward(&self, re: &mut [f64], im: &mut [f64]) {
        let n = self.n;
        debug_assert_eq!(re.len(), n);
        debug_assert_eq!(im.len(), n);

        // Bit-reversal permutation.
        let mut j = 0usize;
        for i in 1..n {
            let mut bit = n >> 1;
            while j & bit != 0 {
                j ^= bit;
                bit >>= 1;
            }
            j |= bit;
            if i < j {
                re.swap(i, j);
                im.swap(i, j);
            }
        }

        // Butterfly stages, twiddles read from the table.
        let mut len = 2usize;
        while len <= n {
            let half = len / 2;
            let step = n / len;
            let mut i = 0usize;
            while i < n {
                for k in 0..half {
                    let (wr, wi) = (self.tw_re[k * step], self.tw_im[k * step]);
                    let ur = re[i + k];
                    let ui = im[i + k];
                    let xr = re[i + k + half];
                    let xi = im[i + k + half];
                    let vr = xr * wr - xi * wi;
                    let vi = xr * wi + xi * wr;
                    re[i + k] = ur + vr;
                    im[i + k] = ui + vi;
                    re[i + k + half] = ur - vr;
                    im[i + k + half] = ui - vi;
                }
                i += len;
            }
            len <<= 1;
        }
    }
}