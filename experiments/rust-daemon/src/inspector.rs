// Port of src/safehalo/targeted_inspector.py (TargetedFallInspector)

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum InspState {
    Standby,
    Triggered,
    HighFreqBurst,
    ScanningRespiration,
    AwaitingChallenge,
    VerdictFall,
    VerdictObject,
    VerdictRecovered,
}

impl InspState {
    pub fn name(&self) -> &'static str {
        match self {
            InspState::Standby => "STANDBY",
            InspState::Triggered => "TRIGGERED",
            InspState::HighFreqBurst => "HIGH_FREQ_BURST",
            InspState::ScanningRespiration => "SCANNING_RESPIRATION",
            InspState::AwaitingChallenge => "AWAITING_CHALLENGE",
            InspState::VerdictFall => "VERDICT_FALL",
            InspState::VerdictObject => "VERDICT_OBJECT",
            InspState::VerdictRecovered => "VERDICT_RECOVERED",
        }
    }
}

#[derive(Clone, Debug)]
pub struct Verdict {
    pub state: InspState,
    pub has_respiration: bool,
    pub respiration_bpm: f64,
    pub body_recovered: bool,
    pub challenge_intercepted: bool,
    pub is_inanimate_object: bool,
    pub confidence: f64,
}

impl Verdict {
    fn new(state: InspState) -> Self {
        Verdict {
            state,
            has_respiration: false,
            respiration_bpm: 0.0,
            body_recovered: false,
            challenge_intercepted: false,
            is_inanimate_object: false,
            confidence: 0.0,
        }
    }
}

pub enum InspEvent {
    Burst { ts: f64, rate: u32 },
    Challenge { ts: f64 },
    Progress { ts: f64, state: &'static str },
}

pub struct Inspector {
    pub duration: f64,
    pub state: InspState,
    start_time: f64,
    manual_safe: bool,
    wave: Vec<f64>,
    ratios: Vec<f64>,
    pub events: Vec<InspEvent>,
    pub enable_peak_regularity: bool,
    pub peak_regularity_cv: f64,
    pub peak_regularity_min_range: f64,
}

impl Inspector {
    pub fn new(duration: f64) -> Self {
        Inspector {
            duration,
            state: InspState::Standby,
            start_time: 0.0,
            manual_safe: false,
            wave: Vec::new(),
            ratios: Vec::new(),
            events: Vec::new(),
            enable_peak_regularity: false,
            peak_regularity_cv: 0.35,
            peak_regularity_min_range: 0.15,
        }
    }

    pub fn acknowledge_safe(&mut self) {
        if self.state != InspState::Standby {
            self.manual_safe = true;
        }
    }

    fn switch(&mut self, new_state: InspState, ts: f64) {
        self.state = new_state;
        self.events.push(InspEvent::Progress { ts, state: new_state.name() });
    }

    pub fn trigger(&mut self, ts: f64) {
        self.state = InspState::Triggered;
        self.start_time = ts;
        self.manual_safe = false;
        self.wave.clear();
        self.ratios.clear();
        self.events.push(InspEvent::Burst { ts, rate: 100 });
        self.switch(InspState::HighFreqBurst, ts);
    }

    fn reset(&mut self, ts: f64) {
        self.events.push(InspEvent::Burst { ts, rate: 30 });
        self.state = InspState::Standby;
        self.wave.clear();
        self.ratios.clear();
    }

    pub fn update_frame(
        &mut self,
        ts: f64,
        diff_energy: f64,
        base_diff: f64,
        amps: &[f64],
    ) -> Option<Verdict> {
        if self.state == InspState::Standby {
            return None;
        }
        let elapsed = ts - self.start_time;
        let diff_ratio = if base_diff > 1e-6 { diff_energy / base_diff } else { 1.0 };
        self.ratios.push(diff_ratio);

        if self.manual_safe {
            self.switch(InspState::VerdictRecovered, ts);
            self.reset(ts);
            let mut v = Verdict::new(InspState::VerdictRecovered);
            v.body_recovered = true;
            v.challenge_intercepted = true;
            v.confidence = 0.98;
            return Some(v);
        }

        let in_challenge = self.state == InspState::AwaitingChallenge;
        let recovery_thresh = if in_challenge { 2.1 } else { 2.8 };
        if diff_ratio > recovery_thresh && elapsed > 0.8 {
            self.switch(InspState::VerdictRecovered, ts);
            self.reset(ts);
            let mut v = Verdict::new(InspState::VerdictRecovered);
            v.body_recovered = true;
            v.challenge_intercepted = in_challenge;
            v.confidence = 0.94;
            return Some(v);
        }

        if !amps.is_empty() {
            let mean = amps.iter().sum::<f64>() / amps.len() as f64;
            self.wave.push(mean);
            if self.wave.len() > 150 {
                self.wave.remove(0);
            }
        }

        let scan_t = 0.8_f64.max(self.duration * 0.33);
        let chal_t = 1.6_f64.max(self.duration * 0.66);
        if elapsed >= scan_t && self.state == InspState::HighFreqBurst {
            self.switch(InspState::ScanningRespiration, ts);
        }
        if elapsed >= chal_t && self.state == InspState::ScanningRespiration {
            self.switch(InspState::AwaitingChallenge, ts);
            self.events.push(InspEvent::Challenge { ts });
        }

        if elapsed >= self.duration {
            let (has_resp, bpm) = self.evaluate_respiration();
            let start = self.ratios.len().saturating_sub(60);
            let tail = &self.ratios[start..];
            let avg = if tail.is_empty() {
                1.0
            } else {
                tail.iter().sum::<f64>() / tail.len() as f64
            };
            let v = if has_resp || avg > 1.28 {
                let eff_bpm = if has_resp { bpm } else { 16.0 };
                self.switch(InspState::VerdictFall, ts);
                let mut v = Verdict::new(InspState::VerdictFall);
                v.has_respiration = true;
                v.respiration_bpm = eff_bpm;
                v.confidence = 0.96;
                v
            } else {
                self.switch(InspState::VerdictObject, ts);
                let mut v = Verdict::new(InspState::VerdictObject);
                v.is_inanimate_object = true;
                v.confidence = 0.88;
                v
            };
            self.reset(ts);
            return Some(v);
        }
        None
    }

    fn evaluate_respiration(&self) -> (bool, f64) {
        let n = self.wave.len();
        if n < 30 {
            return (false, 0.0);
        }
        let w = 12usize;
        let smoothed: Vec<f64> = (0..n)
            .map(|i| {
                let s = i.saturating_sub(w);
                let e = (i + w + 1).min(n);
                self.wave[s..e].iter().sum::<f64>() / (e - s) as f64
            })
            .collect();
        let mean = smoothed.iter().sum::<f64>() / n as f64;
        let zm: Vec<f64> = smoothed.iter().map(|x| x - mean).collect();

        let mut peaks = 0u32;
        let mut last_peak: i64 = -25;
        let mut peak_indices: Vec<usize> = Vec::new();
        for i in 1..n - 1 {
            if zm[i] > zm[i - 1]
                && zm[i] > zm[i + 1]
                && zm[i] > 0.000015
                && (i as i64 - last_peak >= 25)
            {
                peaks += 1;
                last_peak = i as i64;
                peak_indices.push(i);
            }
        }

        if !self.enable_peak_regularity {
            if (1..=6).contains(&peaks) {
                let dur = 2.0_f64.max(self.duration - 1.0);
                let bpm = peaks as f64 / dur * 60.0;
                return (true, bpm.max(12.0).min(26.0));
            } else {
                return (false, 0.0);
            }
        }

        // P0-3: 呼吸峰规整度与准周期性抗噪检验
        if peaks < 1 || peaks > 6 {
            return (false, 0.0);
        }

        let max_zm = zm.iter().cloned().fold(f64::NEG_INFINITY, f64::max);
        let min_zm = zm.iter().cloned().fold(f64::INFINITY, f64::min);
        let dyn_range = max_zm - min_zm;
        if dyn_range < self.peak_regularity_min_range {
            return (false, 0.0);
        }

        let dur = 2.0_f64.max(self.duration - 1.0);
        let bpm = peaks as f64 / dur * 60.0;

        if peaks == 1 {
            let p = peak_indices[0];
            let mut left = p;
            while left > 0 && zm[left] > 0.0 {
                left -= 1;
            }
            let mut right = p;
            while right < n - 1 && zm[right] > 0.0 {
                right += 1;
            }
            let lobe_width = right - left;
            let min_width = 20.max((0.20 * n as f64) as usize);
            if lobe_width < min_width {
                return (false, 0.0);
            }
            return (true, bpm.max(12.0).min(26.0));
        }

        let intervals: Vec<f64> = peak_indices
            .windows(2)
            .map(|w| (w[1] - w[0]) as f64)
            .collect();

        if intervals.len() >= 2 {
            let mean = intervals.iter().sum::<f64>() / intervals.len() as f64;
            let var = intervals.iter().map(|&x| (x - mean).powi(2)).sum::<f64>() / intervals.len() as f64;
            let cv = var.sqrt() / mean;
            if cv > self.peak_regularity_cv {
                return (false, 0.0);
            }
        } else if intervals.len() == 1 {
            let fs = n as f64 / dur;
            let interval_s = intervals[0] / fs;
            if interval_s < 2.0 || interval_s > 6.0 {
                return (false, 0.0);
            }
        }

        (true, bpm.max(12.0).min(26.0))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_legacy_vulnerability_on_noisy_object() {
        let mut insp = Inspector::new(5.0);
        insp.enable_peak_regularity = false;
        insp.trigger(0.0);

        // Simulate 150 frames of noisy still floor with small ripples
        for i in 0..150 {
            let t = (i + 1) as f64 * (5.0 / 150.0);
            let ripple = if i % 30 == 15 { 0.05 } else { 0.0 };
            let amps = vec![30.0 + ripple; 52];
            let v = insp.update_frame(t, 1.05, 1.0, &amps);
            if i == 149 {
                assert!(v.is_some());
                let verdict = v.unwrap();
                // In legacy mode, random ripples trigger false alarm VERDICT_FALL
                assert_eq!(verdict.state, InspState::VerdictFall);
            }
        }
    }

    #[test]
    fn test_peak_regularity_fixes_noisy_object() {
        let mut insp = Inspector::new(5.0);
        insp.enable_peak_regularity = true;
        insp.peak_regularity_cv = 0.35;
        insp.trigger(0.0);

        for i in 0..150 {
            let t = (i + 1) as f64 * (5.0 / 150.0);
            let ripple = if i % 30 == 15 { 0.05 } else { 0.0 };
            let amps = vec![30.0 + ripple; 52];
            let v = insp.update_frame(t, 1.05, 1.0, &amps);
            if i == 149 {
                assert!(v.is_some());
                let verdict = v.unwrap();
                // With peak regularity enabled, low dynamic range noise ripples are rejected
                assert_eq!(verdict.state, InspState::VerdictObject);
                assert!(verdict.is_inanimate_object);
            }
        }
    }

    #[test]
    fn test_real_human_breathing_passes_regularity() {
        let mut insp = Inspector::new(5.0);
        insp.enable_peak_regularity = true;
        insp.peak_regularity_cv = 0.35;
        insp.trigger(0.0);

        for i in 0..150 {
            let t = (i + 1) as f64 * (5.0 / 150.0);
            // 16 bpm = 0.267 Hz
            let breath = 0.8 * (2.0 * std::f64::consts::PI * (16.0 / 60.0) * t).sin();
            let amps = vec![30.0 + breath; 52];
            let v = insp.update_frame(t, 1.1, 1.0, &amps);
            if i == 149 {
                assert!(v.is_some());
                let verdict = v.unwrap();
                assert_eq!(verdict.state, InspState::VerdictFall);
                assert!(verdict.has_respiration);
                assert!(verdict.respiration_bpm >= 12.0 && verdict.respiration_bpm <= 26.0);
            }
        }
    }

    #[test]
    fn test_single_noise_ripple_rejected() {
        let mut insp = Inspector::new(5.0);
        insp.enable_peak_regularity = true;

        let mut wave = vec![30.0; 100];
        // Subtle single hump with tiny amplitude
        for i in 0..100 {
            let diff = (i as f64 - 50.0).powi(2) / 30.0;
            wave[i] += 0.08 * (-diff).exp();
        }
        insp.wave = wave;
        let (has_resp, _) = insp.evaluate_respiration();
        assert!(!has_resp);

        // In legacy mode (false), it mistakenly accepts it:
        insp.enable_peak_regularity = false;
        let (has_resp_legacy, _) = insp.evaluate_respiration();
        assert!(has_resp_legacy);
    }

    #[test]
    fn test_irregular_peaks_rejected() {
        let mut insp = Inspector::new(6.0);
        insp.enable_peak_regularity = true;
        insp.peak_regularity_cv = 0.35;

        let mut wave = vec![30.0; 150];
        for &p in &[20usize, 50, 140] {
            for di in -4..=4 {
                let idx = p as i32 + di;
                if idx >= 0 && idx < 150 {
                    wave[idx as usize] += 0.8 * (1.0 - (di.abs() as f64 / 5.0));
                }
            }
        }
        insp.wave = wave;
        let (has_resp, _) = insp.evaluate_respiration();
        assert!(!has_resp);
    }
}