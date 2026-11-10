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
        for i in 1..n - 1 {
            if zm[i] > zm[i - 1]
                && zm[i] > zm[i + 1]
                && zm[i] > 0.000015
                && (i as i64 - last_peak >= 25)
            {
                peaks += 1;
                last_peak = i as i64;
            }
        }
        if (1..=6).contains(&peaks) {
            let dur = 2.0_f64.max(self.duration - 1.0);
            let bpm = peaks as f64 / dur * 60.0;
            (true, bpm.max(12.0).min(26.0))
        } else {
            (false, 0.0)
        }
    }
}