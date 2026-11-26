// Port of src/safehalo/object_confirm.py (ObjectConfirmWatcher)
//
// Stage 2 runs after stage 1 (targeted inspection) returned VERDICT_OBJECT:
// keep accumulating the mean ground amplitude for object_confirm_seconds, then
// test interval regularity. Only OBJECT -> FALL escalation is possible, so the
// watch can lower missed alarms but never creates an OBJECT verdict on its own.
// Disabled by default (object_confirm_seconds <= 0).

pub const DECIM_HZ: f64 = 10.0;
pub const DECIM_INTERVAL: f64 = 1.0 / DECIM_HZ;
pub const DECIM_EPS: f64 = 1e-9;
pub const SMOOTH_SECONDS: f64 = 1.0;
pub const MIN_PEAK_SECONDS: f64 = 1.0;
pub const AMP_THRESHOLD: f64 = 0.000015;
pub const MIN_INTERVALS: usize = 3;
pub const MIN_SAMPLES: usize = 30;
// Placeholder: no real-capture calibration yet (see the Python module note).
pub const DEFAULT_CV_THRESHOLD: f64 = 0.35;

pub struct ConfirmResult {
    pub escalate: bool,
    pub confirmed_object: bool,
    pub insufficient: bool,
    pub peaks: u32,
    pub intervals: u32,
    pub cv: f64,
    pub bpm: f64,
}

pub struct ObjectConfirmWatcher {
    start_ts: f64,
    duration_seconds: f64,
    cv_threshold: f64,
    min_intervals: usize,
    samples: Vec<f64>,
    sample_ts: Vec<f64>,
    last_sample_ts: f64,
}

impl ObjectConfirmWatcher {
    pub fn new(
        start_ts: f64,
        duration_seconds: f64,
        cv_threshold: f64,
        min_intervals: usize,
    ) -> Self {
        ObjectConfirmWatcher {
            start_ts,
            duration_seconds,
            cv_threshold,
            min_intervals,
            samples: Vec::new(),
            sample_ts: Vec::new(),
            // let the first frame be sampled immediately
            last_sample_ts: start_ts - DECIM_INTERVAL,
        }
    }

    /// Feed one ground-amplitude frame; returns the verdict on the frame that
    /// closes the observation window, None before that.
    pub fn feed(&mut self, ts: f64, amplitude: f64) -> Option<ConfirmResult> {
        if ts - self.last_sample_ts >= DECIM_INTERVAL - DECIM_EPS {
            self.samples.push(amplitude);
            self.sample_ts.push(ts);
            self.last_sample_ts = ts;
        }
        if ts - self.start_ts >= self.duration_seconds - DECIM_EPS {
            return Some(self.analyze());
        }
        None
    }

    fn analyze(&self) -> ConfirmResult {
        let n = self.samples.len();
        let mut r = ConfirmResult {
            escalate: false,
            confirmed_object: true,
            insufficient: true,
            peaks: 0,
            intervals: 0,
            cv: 0.0,
            bpm: 0.0,
        };
        if n < MIN_SAMPLES {
            return r;
        }

        let w = (SMOOTH_SECONDS * DECIM_HZ).round() as usize;
        let mut smoothed = Vec::with_capacity(n);
        for i in 0..n {
            let start = i.saturating_sub(w);
            let end = (i + w + 1).min(n);
            let mut total = 0.0;
            for j in start..end {
                total += self.samples[j];
            }
            smoothed.push(total / (end - start) as f64);
        }

        let mut mean_v = 0.0;
        for x in &smoothed {
            mean_v += *x;
        }
        mean_v /= n as f64;
        let zm: Vec<f64> = smoothed.iter().map(|x| x - mean_v).collect();

        let min_dist = (MIN_PEAK_SECONDS * DECIM_HZ).round() as i64;
        let mut peak_idx: Vec<usize> = Vec::new();
        let mut last_peak: i64 = -min_dist;
        for i in 1..n - 1 {
            // left strictly greater, right allows equality: a pure sine sampled
            // on the 10 Hz grid often puts the peak between samples; award the
            // tie to the left sample so it is not lost.
            if zm[i] > zm[i - 1]
                && zm[i] >= zm[i + 1]
                && zm[i] > AMP_THRESHOLD
                && (i as i64 - last_peak >= min_dist)
            {
                peak_idx.push(i);
                last_peak = i as i64;
            }
        }

        r.peaks = peak_idx.len() as u32;
        if peak_idx.len().saturating_sub(1) < self.min_intervals {
            return r;
        }

        let mut intervals: Vec<f64> = Vec::with_capacity(peak_idx.len() - 1);
        for k in 1..peak_idx.len() {
            intervals.push(self.sample_ts[peak_idx[k]] - self.sample_ts[peak_idx[k - 1]]);
        }

        let mut mean_i = 0.0;
        for x in &intervals {
            mean_i += *x;
        }
        mean_i /= intervals.len() as f64;
        let mut var_i = 0.0;
        for x in &intervals {
            var_i += (x - mean_i) * (x - mean_i);
        }
        var_i /= intervals.len() as f64;
        let cv = if mean_i > 1e-9 { var_i.sqrt() / mean_i } else { f64::INFINITY };
        let bpm = if mean_i > 1e-9 { 60.0 / mean_i } else { 0.0 };

        r.intervals = intervals.len() as u32;
        r.cv = cv;
        r.bpm = bpm;
        r.confirmed_object = cv > self.cv_threshold;
        r.escalate = !r.confirmed_object;
        r.insufficient = false;
        r
    }
}
