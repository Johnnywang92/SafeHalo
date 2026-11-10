// Port of src/safehalo/detector.py (RouterCSIDetector)

use crate::calibrator::Calibrator;
use crate::dsp::{round_n, Features};
use crate::inspector::{InspEvent, InspState, Inspector};
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
    FallAlarm { ts: f64 },
    ChallengeIntercepted { ts: f64 },
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
    pet: PetFilter,
    pub inspector: Inspector,
    pending: Option<HumanState>,
    hold: u32,
    pub trace: Vec<TraceEvent>,
    pub fall_alarms: u64,
    pub last_fall_bpm: f64,
    pub last_fall_confidence: f64,
}

impl Detector {
    pub fn new(calibration_seconds: f64, stillness_seconds: f64, calibrator: Calibrator) -> Self {
        let base_variance = calibrator.state.base_variance;
        let base_diff = calibrator.state.base_diff;
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
            inspector: Inspector::new(stillness_seconds),
            pending: None,
            hold: 0,
            trace: Vec::new(),
            fall_alarms: 0,
            last_fall_bpm: 0.0,
            last_fall_confidence: 0.0,
        }
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

    pub fn update(&mut self, f: &Features) -> HumanState {
        let now = f.timestamp;

        if f.sample_rate_hz < 3.0 {
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
                        self.trace.push(TraceEvent::FallAlarm { ts: now });
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