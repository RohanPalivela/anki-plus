// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

//! Deterministic SplitMix64 PRNG for the Speedrun models.
//!
//! Self-contained (no external RNG dependency) so seeded output is byte-for-byte
//! reproducible on every platform — desktop and Android share this engine and
//! the Readiness Monte Carlo / synthetic seeding rely on identical draws.

/// SplitMix64 generator (Steele et al.). Fast, tiny, and reproducible.
pub(crate) struct SplitMix64 {
    state: u64,
}

impl SplitMix64 {
    pub(crate) fn new(seed: u64) -> Self {
        SplitMix64 { state: seed }
    }

    pub(crate) fn next_u64(&mut self) -> u64 {
        self.state = self.state.wrapping_add(0x9E37_79B9_7F4A_7C15);
        let mut z = self.state;
        z = (z ^ (z >> 30)).wrapping_mul(0xBF58_476D_1CE4_E5B9);
        z = (z ^ (z >> 27)).wrapping_mul(0x94D0_49BB_1331_11EB);
        z ^ (z >> 31)
    }

    /// Uniform in [0, 1).
    pub(crate) fn next_f64(&mut self) -> f64 {
        (self.next_u64() >> 11) as f64 / (1u64 << 53) as f64
    }

    /// Standard normal via Box–Muller.
    pub(crate) fn next_normal(&mut self) -> f64 {
        let u1 = self.next_f64().max(1e-12);
        let u2 = self.next_f64();
        (-2.0 * u1.ln()).sqrt() * (std::f64::consts::TAU * u2).cos()
    }
}

#[cfg(test)]
mod test {
    use super::*;

    #[test]
    fn deterministic_and_in_range() {
        let mut a = SplitMix64::new(42);
        let mut b = SplitMix64::new(42);
        for _ in 0..1000 {
            assert_eq!(a.next_u64(), b.next_u64());
        }
        let mut r = SplitMix64::new(7);
        let mut sum = 0.0;
        for _ in 0..10_000 {
            let u = r.next_f64();
            assert!((0.0..1.0).contains(&u));
            sum += r.next_normal();
        }
        assert!((sum / 10_000.0).abs() < 0.1);
    }
}
