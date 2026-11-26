// Port of src/safehalo/detector.py (RouterCSIDetector)

use crate::calibrator::Calibrator;
use crate::dsp::{round_n, Features};
use crate::inspector::{InspEvent, InspState, Inspector};
use crate::object_confirm::ObjectConfirmWatcher;
use crate::pet::PetFilter;

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum HumanState {
    Calibrating,
    Empty,
    Static,
    Moving,
    PetActivity,
    FallSuspect,
    FallConfirmed,
    RouterOffline,
}

impl HumanState {
    pub fn name(&self) -> &'static str {
        match self {
            HumanState::Calibrating => "CALIBRATING",
            HumanState::Empty => "EMPTY",
            HumanState::Static => "STATIC",
            HumanState::Moving => "MOVING",
            HumanState::PetActivity => "PET_ACTIVITY",
            HumanState::FallSuspect => "FALL_SUSPECT",
            HumanState::FallConfirmed => "FALL_CONFIRMED",
            HumanState::RouterOffline => "ROUTER_OFFLINE",
        }
    }
}

pub enum TraceEvent {
    StateChange { ts: f64, from: &'static str, to: &'static str, variance: f64, diff_energy: f64 },
    Impact { ts: f64, spike_ratio: f64, diff_energy: f64 },
    Burst { ts: f64, rate: u32 },
    Challenge { ts: f64 },
    Inspection { ts: f64, state: &'static str },
    FallAlarm { ts: f64, bpm: f64, confidence: f64 },
    ChallengeIntercepted { ts: f64 },
    ObjectWatch { ts: f64, phase: &'static str, peaks: Option<u32>, intervals: Option<u32>, cv: Option<f64> },
}

pub struct Detector {
    pub calibration_seconds: f64,
    pub fall_spike_threshold: f64,
    pub state: HumanState,
    pub state_start_time: f64,
    pub calibrator: Calibrator,
    pub base_variance: f64,
    pub base_diff: f64,
    calib_start: Option<f64>,
    calib_vars: Vec<f64>,
    calib_diffs: Vec<f64>,
    fall_spike_time: Option<f64>,
    pub pet: PetFilter,
    pub inspector: Inspector,
    object_confirm_seconds: f64,
    object_confirm_cv: f64,
    object_confirm_min_intervals: usize,
    object_watcher: Option<ObjectConfirmWatcher>,
    pending: Option<HumanState>,
    hold: u32,
    pub trace: Vec<TraceEvent>,
    pub fall_alarms: u64,
    pub last_fall_bpm: f64,
    pub last_fall_confidence: f64,
    pub latest_spr: f64,
    pub last_ts: Option<f64>,
}

impl Detector {
    #[allow(clippy::too_many_arguments)]
    pub fn new(
        calibration_seconds: f64,
        stillness_seconds: f64,
        object_confirm_seconds: f64,
        object_confirm_cv: f64,
        object_confirm_min_intervals: usize,
        enable_peak_regularity: bool,
        peak_regularity_cv: f64,
        calibrator: Calibrator,
    ) -> Self {
        let base_variance = calibrator.state.base_variance;
        let base_diff = calibrator.state.base_diff;
        let mut inspector = Inspector::new(stillness_seconds);
        inspector.enable_peak_regularity = enable_peak_regularity;
        inspector.peak_regularity_cv = peak_regularity_cv;
        Detector {
            calibration_seconds,
            fall_spike_threshold: 2.0,
            state: HumanState::Calibrating,
            state_start_time: 0.0,
            calibrator,
            base_variance,
            base_diff,
            calib_start: None,
            calib_vars: Vec::new(),
            calib_diffs: Vec::new(),
            fall_spike_time: None,
            pet: PetFilter::new(),
            inspector,
            object_confirm_seconds,
            object_confirm_cv,
            object_confirm_min_intervals,
            object_watcher: None,
            pending: None,
            hold: 0,
            trace: Vec::new(),
            fall_alarms: 0,
            last_fall_bpm: 0.0,
            last_fall_confidence: 0.0,
            latest_spr: 0.0,
            last_ts: None,
        }
    }

    pub fn start_calibration(&mut self) {
        self.state = HumanState::Calibrating;
        self.calib_start = None;
        self.calib_vars.clear();
        self.calib_diffs.clear();
    }

    fn switch(&mut self, new_state: HumanState, f: &Features) {
        let old = self.state;
        self.state = new_state;
        self.state_start_time = f.timestamp;
        self.trace.push(TraceEvent::StateChange {
            ts: f.timestamp,
            from: old.name(),
            to: new_state.name(),
            variance: round_n(f.mean_variance, 6),
            diff_energy: round_n(f.diff_energy, 5),
        });
    }

    fn drain_inspector_events(&mut self) {
        for ev in self.inspector.events.drain(..) {
            match ev {
                InspEvent::Burst { ts, rate } => self.trace.push(TraceEvent::Burst { ts, rate }),
                InspEvent::Challenge { ts } => self.trace.push(TraceEvent::Challenge { ts }),
                InspEvent::Progress { ts, state } => {
                    self.trace.push(TraceEvent::Inspection { ts, state })
                }
            }
        }
    }

    /// Control-plane entry used by the web console's manual inspection.
    pub fn force_inspect(&mut self, fallback_ts: f64) {
        let ts = self.last_ts.unwrap_or(fallback_ts);
        if self.object_watcher.take().is_some() {
            self.trace.push(TraceEvent::ObjectWatch {
                ts,
                phase: "aborted",
                peaks: None,
                intervals: None,
                cv: None,
            });
        }
        if self.state != HumanState::FallSuspect {
            let old = self.state;
            self.state = HumanState::FallSuspect;
            self.state_start_time = ts;
            self.trace.push(TraceEvent::StateChange {
                ts,
                from: old.name(),
                to: "FALL_SUSPECT",
                variance: 0.0,
                diff_energy: 0.0,
            });
        }
        self.fall_spike_time = Some(ts);
        self.inspector.trigger(ts);
    }

    pub fn acknowledge_safe(&mut self) {
        self.inspector.acknowledge_safe();
    }

    pub fn update(&mut self, f: &Features) -> HumanState {
        let now = f.timestamp;
        self.last_ts = Some(now);

        if f.sample_rate_hz < 3.0 {
            if self.object_watcher.take().is_some() {
                // Samples inside a dropout are not trustworthy: abandon this watch.
                self.trace.push(TraceEvent::ObjectWatch {
                    ts: now, phase: "aborted", peaks: None, intervals: None, cv: None,
                });
            }
            if self.state != HumanState::RouterOffline {
                self.switch(HumanState::RouterOffline, f);
            }
            return self.state;
        }

        if self.state == HumanState::Calibrating {
            if self.calib_start.is_none() {
                self.calib_start = Some(now);
            }
            self.calib_vars.push(f.mean_variance);
            self.calib_diffs.push(f.diff_energy);
            let elapsed = now - self.calib_start.unwrap();
            if elapsed >= self.calibration_seconds {
                self.calib_vars.sort_by(|a, b| a.partial_cmp(b).unwrap());
                self.calib_diffs.sort_by(|a, b| a.partial_cmp(b).unwrap());
                let n = self.calib_vars.len();
                let cand_var = self.calib_vars[n / 2].max(1e-6);
                let cand_diff = self.calib_diffs[n / 2].max(1e-5);
                let (eff_var, eff_diff, is_clean) = self.calibrator.validate_initial_calibration(
                    cand_var, cand_diff, now,
                );
                // Python quirk preserved: clean boot adopts the unrounded candidate.
                self.base_variance = if !is_clean { eff_var } else { cand_var };
                self.base_diff = if !is_clean { eff_diff } else { cand_diff };
                self.switch(HumanState::Empty, f);
            }
            return self.state;
        }

        let var_ratio = f.mean_variance / self.base_variance;
        let diff_ratio = f.diff_energy / self.base_diff;

        let pet_res = self.pet.evaluate(
            &f.subcarrier_variances,
            self.base_variance,
            f.diff_energy,
            self.base_diff,
        );
        self.latest_spr = pet_res.spr;

        let is_high_impact = (f.spike_ratio >= self.fall_spike_threshold && diff_ratio > 2.5)
            || (var_ratio > 8.0 && f.spike_ratio > 1.8)
            || (f.diff_energy > 0.035 && f.mean_variance > 0.003);

        if is_high_impact
            && (self.state == HumanState::Moving || self.state == HumanState::Static)
        {
            self.fall_spike_time = Some(now);
            self.trace.push(TraceEvent::Impact {
                ts: now,
                spike_ratio: f.spike_ratio,
                diff_energy: f.diff_energy,
            });
        }

        if let Some(spike) = self.fall_spike_time {
            if now - spike <= 3.5 && diff_ratio < 2.5 && self.state != HumanState::FallSuspect {
                self.switch(HumanState::FallSuspect, f);
                return self.state;
            }
        }

        // 3.5 Stage-2 object confirmation watch in progress: stay in
        // FALL_SUSPECT (no alarm) until the long window closes. Escalation to
        // FALL is the only allowed direction.
        if self.state == HumanState::FallSuspect {
            if let Some(w) = self.object_watcher.as_mut() {
                let mut mean_amp = 0.0;
                for a in &f.current_amplitudes {
                    mean_amp += *a;
                }
                if !f.current_amplitudes.is_empty() {
                    mean_amp /= f.current_amplitudes.len() as f64;
                }
                let result = w.feed(now, mean_amp);
                if let Some(r) = result {
                    self.object_watcher = None;
                    if r.escalate {
                        self.trace.push(TraceEvent::ObjectWatch {
                            ts: now, phase: "escalate",
                            peaks: Some(r.peaks), intervals: Some(r.intervals), cv: Some(r.cv),
                        });
                        self.fall_alarms += 1;
                        self.last_fall_bpm = r.bpm;
                        self.last_fall_confidence = 0.90;
                        self.trace.push(TraceEvent::FallAlarm {
                            ts: now,
                            bpm: r.bpm,
                            confidence: 0.90,
                        });
                        self.fall_spike_time = None;
                        self.switch(HumanState::FallConfirmed, f);
                    } else {
                        let phase = if r.insufficient { "insufficient" } else { "confirmed" };
                        self.trace.push(TraceEvent::ObjectWatch {
                            ts: now, phase,
                            peaks: Some(r.peaks), intervals: Some(r.intervals), cv: Some(r.cv),
                        });
                        self.fall_spike_time = None;
                        self.switch(HumanState::Empty, f);
                    }
                }
                return self.state;
            }
        }

        if self.state == HumanState::FallSuspect {
            if self.inspector.state == InspState::Standby {
                self.inspector.trigger(now);
            }
            let verdict = self.inspector.update_frame(
                now,
                f.diff_energy,
                self.base_diff,
                &f.current_amplitudes,
            );
            self.drain_inspector_events();

            if let Some(v) = verdict {
                match v.state {
                    InspState::VerdictFall => {
                        self.fall_alarms += 1;
                        self.last_fall_bpm = v.respiration_bpm;
                        self.last_fall_confidence = v.confidence;
                        self.trace.push(TraceEvent::FallAlarm {
                            ts: now,
                            bpm: v.respiration_bpm,
                            confidence: v.confidence,
                        });
                        self.switch(HumanState::FallConfirmed, f);
                        return self.state;
                    }
                    InspState::VerdictRecovered => {
                        if v.challenge_intercepted {
                            self.trace.push(TraceEvent::ChallengeIntercepted { ts: now });
                        }
                        self.fall_spike_time = None;
                        self.switch(HumanState::Moving, f);
                        return self.state;
                    }
                    InspState::VerdictObject => {
                        if self.object_confirm_seconds > 0.0 {
                            // Stage 1 cannot resolve enough respiration cycles;
                            // keep FALL_SUSPECT (alarm-leaning) and watch longer.
                            self.object_watcher = Some(ObjectConfirmWatcher::new(
                                now,
                                self.object_confirm_seconds,
                                self.object_confirm_cv,
                                self.object_confirm_min_intervals,
                            ));
                            self.trace.push(TraceEvent::ObjectWatch {
                                ts: now, phase: "start", peaks: None, intervals: None, cv: None,
                            });
                            return self.state;
                        }
                        self.fall_spike_time = None;
                        self.switch(HumanState::Empty, f);
                        return self.state;
                    }
                    _ => {}
                }
            }
            return self.state;
        }

        if self.state == HumanState::FallConfirmed {
            if diff_ratio > 2.5 && var_ratio > 3.5 {
                self.fall_spike_time = None;
                self.switch(HumanState::Moving, f);
            }
            return self.state;
        }

        let is_moving = (diff_ratio > 2.5 && var_ratio > 3.0) || diff_ratio > 3.2;
        let is_present = var_ratio > 1.35 || diff_ratio > 1.3;

        let raw_target = if pet_res.is_pet && self.fall_spike_time.is_none() {
            HumanState::PetActivity
        } else if is_moving {
            HumanState::Moving
        } else if is_present {
            HumanState::Static
        } else {
            if let Some((v, d)) =
                self.calibrator
                    .observe_frame(true, f.mean_variance, f.diff_energy, now)
            {
                self.base_variance = v;
                self.base_diff = d;
            }
            HumanState::Empty
        };

        if raw_target != HumanState::Empty {
            self.calibrator
                .observe_frame(false, f.mean_variance, f.diff_energy, now);
        }

        if raw_target != self.state {
            if self.pending == Some(raw_target) {
                self.hold += 1;
                if self.hold >= 3 {
                    self.switch(raw_target, f);
                    self.pending = None;
                    self.hold = 0;
                }
            } else {
                self.pending = Some(raw_target);
                self.hold = 1;
            }
        } else {
            self.pending = None;
            self.hold = 0;
        }
        self.state
    }
}