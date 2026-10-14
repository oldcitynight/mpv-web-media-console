//! Parametric IIR low-pass for the adaptive PI servo input.
//!
//! Replaces the old fixed-α EMA on `control_available`. The user
//! configures a cutoff frequency in Hz and an order (1 = single pole,
//! 2 = Butterworth biquad). Coefficients are recomputed on each step
//! from the current sample period so the cutoff stays well-defined
//! even when the callback rate changes (eg. quantum renegotiation).

#[derive(Debug, Clone, Copy, Default)]
pub struct IirLowPassState {
    pub initialized: bool,
    /// y[n-1] for both orders.
    pub y1: f64,
    /// y[n-2] for order 2.
    pub y2: f64,
    /// x[n-1] for order 2.
    pub x1: f64,
    /// x[n-2] for order 2.
    pub x2: f64,
}

impl IirLowPassState {
    pub fn reset(&mut self) {
        *self = Self::default();
    }

    /// Filter one sample. `dt_s` is the time interval since the previous
    /// call (= callback period in seconds). `cutoff_hz` is the desired
    /// −3 dB point of the low-pass response. `order` is 1 (single pole)
    /// or 2 (Butterworth biquad).
    ///
    /// First call seeds all internal states to `input` so the output
    /// settles instantly on a meaningful value rather than ramping up
    /// from zero.
    pub fn step(&mut self, input: f64, cutoff_hz: f64, dt_s: f64, order: u32) -> f64 {
        if !self.initialized {
            self.y1 = input;
            self.y2 = input;
            self.x1 = input;
            self.x2 = input;
            self.initialized = true;
            return input;
        }
        // Guard against degenerate parameters that would produce NaN/Inf:
        // null sample period, non-positive cutoff, or cutoff above Nyquist
        // (the bilinear pre-warp explodes at fs/2 and goes negative beyond).
        // Fall back to pass-through; the caller's tuning step will catch a
        // bogus cutoff in the audio-domain telemetry.
        if dt_s <= 0.0 || cutoff_hz <= 0.0 {
            return input;
        }
        let fs = 1.0 / dt_s;
        let nyquist = fs * 0.5;
        let fc = cutoff_hz.min(nyquist * 0.49); // small safety margin
        match order {
            2 => self.step_biquad_butterworth(input, fc, fs),
            _ => self.step_one_pole(input, fc, fs),
        }
    }

    fn step_one_pole(&mut self, input: f64, fc: f64, fs: f64) -> f64 {
        // Impulse-invariant 1-pole: α = 1 − exp(−2π·fc·dt)
        // y[n] = y[n-1] + α · (x[n] − y[n-1])
        let alpha = 1.0 - (-std::f64::consts::TAU * fc / fs).exp();
        let y = self.y1 + alpha * (input - self.y1);
        self.y1 = y;
        y
    }

    fn step_biquad_butterworth(&mut self, input: f64, fc: f64, fs: f64) -> f64 {
        // Standard RBJ biquad low-pass with Butterworth Q = 1/√2.
        let omega = std::f64::consts::TAU * fc / fs;
        let cos_w = omega.cos();
        let sin_w = omega.sin();
        let q = std::f64::consts::FRAC_1_SQRT_2;
        let alpha_q = sin_w / (2.0 * q);

        let b0 = (1.0 - cos_w) * 0.5;
        let b1 = 1.0 - cos_w;
        let b2 = b0;
        let a0 = 1.0 + alpha_q;
        let a1 = -2.0 * cos_w;
        let a2 = 1.0 - alpha_q;

        let inv_a0 = 1.0 / a0;
        let b0n = b0 * inv_a0;
        let b1n = b1 * inv_a0;
        let b2n = b2 * inv_a0;
        let a1n = a1 * inv_a0;
        let a2n = a2 * inv_a0;

        let y = b0n * input + b1n * self.x1 + b2n * self.x2 - a1n * self.y1 - a2n * self.y2;
        self.x2 = self.x1;
        self.x1 = input;
        self.y2 = self.y1;
        self.y1 = y;
        y
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const DT: f64 = 0.021; // ~21 ms callback period
    const FC: f64 = 0.5; // 0.5 Hz cutoff (the servo's default ballpark)

    #[test]
    fn first_call_seeds_and_returns_input() {
        let mut s = IirLowPassState::default();
        assert_eq!(s.step(42.0, FC, DT, 1), 42.0);
        assert!(s.initialized);
    }

    #[test]
    fn passes_through_on_degenerate_params() {
        let mut s = IirLowPassState::default();
        s.step(0.0, FC, DT, 1); // initialise at 0
        assert_eq!(s.step(7.0, FC, 0.0, 1), 7.0); // dt_s <= 0 guard
        assert_eq!(s.step(9.0, 0.0, DT, 1), 9.0); // cutoff <= 0 guard
    }

    #[test]
    fn one_pole_converges_monotonically_to_dc() {
        let mut s = IirLowPassState::default();
        s.step(0.0, FC, DT, 1); // seed at 0
        let mut last = 0.0;
        for _ in 0..2000 {
            let y = s.step(100.0, FC, DT, 1);
            // Rises monotonically toward the target without overshooting (DC gain 1).
            assert!(y >= last - 1e-9 && y <= 100.0 + 1e-6, "y = {y}");
            last = y;
        }
        assert!((last - 100.0).abs() < 0.5, "settled at {last}");
    }

    #[test]
    fn biquad_converges_to_dc() {
        let mut s = IirLowPassState::default();
        s.step(0.0, FC, DT, 2);
        let mut y = 0.0;
        for _ in 0..2000 {
            y = s.step(100.0, FC, DT, 2);
        }
        assert!((y - 100.0).abs() < 0.5, "settled at {y}");
    }

    #[test]
    fn reset_clears_state_and_reseeds() {
        let mut s = IirLowPassState::default();
        s.step(50.0, FC, DT, 1);
        s.reset();
        assert!(!s.initialized);
        assert_eq!(s.step(3.0, FC, DT, 1), 3.0); // next call seeds again
    }

    #[test]
    fn cutoff_far_above_nyquist_stays_finite() {
        // The bilinear pre-warp blows up at fs/2; the clamp must keep output finite.
        let mut s = IirLowPassState::default();
        s.step(0.0, 1e9, DT, 2);
        for _ in 0..100 {
            assert!(s.step(1.0, 1e9, DT, 2).is_finite());
        }
    }
}
