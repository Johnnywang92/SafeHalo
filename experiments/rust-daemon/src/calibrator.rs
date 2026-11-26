// Port of src/safehalo/auto_calibrator.py (ContinuousAutoCalibrator)
// Civil date conversion uses UTC; the Python reference runs with TZ=UTC to match.

use std::fs;
use std::path::PathBuf;

use crate::dsp::round_n;
use crate::json;

const MAX_CLEAN_BOOT_VARIANCE: f64 = 0.0025;
const MAX_CLEAN_BOOT_DIFF: f64 = 0.015;

#[derive(Clone)]
pub struct CalState {
    pub base_variance: f64,
    pub base_diff: f64,
    pub hourly_variance: Vec<f64>,
    pub hourly_diff: Vec<f64>,
    pub quiet_windows_learned: u64,
    pub learning_days: Vec<String>,
    pub boot_contamination_rejected: u64,
    pub drift_adaptations_count: u64,
    pub last_updated: String,
}

impl Default for CalState {
    fn default() -> Self {
        CalState {
            base_variance: 0.0001,
            base_diff: 0.001,
            hourly_variance: vec![0.0001; 24],
            hourly_diff: vec![0.001; 24],
            quiet_windows_learned: 0,
            learning_days: Vec::new(),
            boot_contamination_rejected: 0,
            drift_adaptations_count: 0,
            last_updated: String::new(),
        }
    }
}

#[derive(Clone)]
pub struct CalSummary {
    pub base_variance: f64,
    pub base_diff: f64,
    pub learning_days: usize,
    pub quiet_windows_learned: u64,
    pub maturity_percent: f64,
    pub boot_contamination_rejected: u64,
    pub drift_adaptations_count: u64,
    pub last_updated: String,
}

pub struct Calibrator {
    pub state: CalState,
    pub has_persisted: bool,
    model_file: PathBuf,
    quiet_window_min_seconds: f64,
    ema_alpha: f64,
    quiet_start: Option<f64>,
    quiet_vars: Vec<f64>,
    quiet_diffs: Vec<f64>,
}

fn utc_hour_day(ts: f64) -> (usize, String) {
    let days = (ts / 86400.0).floor() as i64;
    let secs = ts - days as f64 * 86400.0;
    let hour = (secs / 3600.0).floor() as usize;
    // Howard Hinnant's civil_from_days algorithm.
    let z = days + 719468;
    let era = if z >= 0 { z } else { z - 146096 } / 146097;
    let doe = z - era * 146097;
    let yoe = (doe - doe / 1460 + doe / 36524 - doe / 146096) / 365;
    let mut y = yoe + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = doy - (153 * mp + 2) / 5 + 1;
    let m = if mp < 10 { mp + 3 } else { mp - 9 };
    if m <= 2 {
        y += 1;
    }
    (hour.min(23), format!("{:04}-{:02}-{:02}", y, m, d))
}

fn wall_now_string() -> String {
    let secs = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs() as f64)
        .unwrap_or(0.0);
    let (hour, day) = utc_hour_day(secs);
    let rem = secs - (secs / 86400.0).floor() * 86400.0;
    let minute = (rem / 60.0) as u32 % 60;
    let second = rem as u32 % 60;
    format!("{} {:02}:{:02}:{:02}", day, hour, minute, second)
}

impl Calibrator {
    pub fn new(storage_dir: &str) -> Self {
        let dir = PathBuf::from(storage_dir);
        let _ = fs::create_dir_all(&dir);
        let model_file = dir.join("baseline_model.json");
        let mut c = Calibrator {
            state: CalState::default(),
            has_persisted: false,
            model_file,
            quiet_window_min_seconds: 30.0,
            ema_alpha: 0.08,
            quiet_start: None,
            quiet_vars: Vec::new(),
            quiet_diffs: Vec::new(),
        };
        c.has_persisted = c.load();
        c
    }

    fn load(&mut self) -> bool {
        let text = match fs::read_to_string(&self.model_file) {
            Ok(t) => t,
            Err(_) => return false,
        };
        let root = match json::parse(&text) {
            Some(v) => v,
            None => {
                eprintln!("[AutoCalibrator] 读取持久化基线模型失败: invalid JSON");
                return false;
            }
        };
        let num = |k: &str, d: f64| root.get(k).and_then(|v| v.as_f64()).unwrap_or(d);
        let uint = |k: &str| root.get(k).and_then(|v| v.as_u64()).unwrap_or(0);
        let hourly = |k: &str, d: f64| -> Vec<f64> {
            let mut v: Vec<f64> = root
                .get(k)
                .and_then(|x| x.as_arr())
                .map(|a| a.iter().filter_map(|x| x.as_f64()).take(24).collect())
                .unwrap_or_else(|| vec![d; 24]);
            while v.len() < 24 {
                v.push(d);
            }
            v
        };
        self.state = CalState {
            base_variance: num("base_variance", 0.0001),
            base_diff: num("base_diff", 0.001),
            hourly_variance: hourly("hourly_variance", 0.0001),
            hourly_diff: hourly("hourly_diff", 0.001),
            quiet_windows_learned: uint("quiet_windows_learned"),
            learning_days: root
                .get("learning_days_set")
                .and_then(|x| x.as_arr())
                .map(|a| a.iter().filter_map(|x| x.as_str().map(String::from)).collect())
                .unwrap_or_default(),
            boot_contamination_rejected: uint("boot_contamination_rejected"),
            drift_adaptations_count: uint("drift_adaptations_count"),
            last_updated: root
                .get("last_updated")
                .and_then(|x| x.as_str())
                .unwrap_or("")
                .to_string(),
        };
        true
    }

    fn save(&mut self) {
        self.state.last_updated = wall_now_string();
        let s = &self.state;
        let arr = |v: &[f64]| -> String {
            format!(
                "[{}]",
                v.iter().map(|x| format!("{}", x)).collect::<Vec<_>>().join(", ")
            )
        };
        let days = s
            .learning_days
            .iter()
            .map(|d| format!("\"{}\"", json::escape(d)))
            .collect::<Vec<_>>()
            .join(", ");
        let body = format!(
            "{{\n  \"base_variance\": {},\n  \"base_diff\": {},\n  \"hourly_variance\": {},\n  \"hourly_diff\": {},\n  \"quiet_windows_learned\": {},\n  \"learning_days_set\": [{}],\n  \"boot_contamination_rejected\": {},\n  \"drift_adaptations_count\": {},\n  \"last_updated\": \"{}\"\n}}",
            s.base_variance,
            s.base_diff,
            arr(&s.hourly_variance),
            arr(&s.hourly_diff),
            s.quiet_windows_learned,
            days,
            s.boot_contamination_rejected,
            s.drift_adaptations_count,
            json::escape(&s.last_updated)
        );
        let tmp = self.model_file.with_extension("json.tmp");
        if let Err(e) = fs::write(&tmp, body) {
            eprintln!("[AutoCalibrator] 保存基线模型失败: {}", e);
            return;
        }
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            let _ = fs::set_permissions(&tmp, fs::Permissions::from_mode(0o600));
        }
        if let Err(e) = fs::rename(&tmp, &self.model_file) {
            eprintln!("[AutoCalibrator] 原子替换基线模型失败: {}", e);
        }
    }

    pub fn validate_initial_calibration(
        &mut self,
        candidate_variance: f64,
        candidate_diff: f64,
        ts: f64,
    ) -> (f64, f64, bool) {
        let (hour, day) = utc_hour_day(ts);
        let contaminated =
            candidate_variance > MAX_CLEAN_BOOT_VARIANCE || candidate_diff > MAX_CLEAN_BOOT_DIFF;

        if contaminated {
            self.state.boot_contamination_rejected += 1;
            let (fb_var, fb_diff) = if self.has_persisted {
                (self.state.hourly_variance[hour], self.state.hourly_diff[hour])
            } else {
                (0.00012, 0.0012)
            };
            self.save();
            return (fb_var, fb_diff, false);
        }

        let (eff_var, eff_diff) =
            if self.has_persisted && self.state.quiet_windows_learned > 0 {
                (
                    0.6 * self.state.base_variance + 0.4 * candidate_variance,
                    0.6 * self.state.base_diff + 0.4 * candidate_diff,
                )
            } else {
                let v = candidate_variance.max(1e-6);
                let d = candidate_diff.max(1e-5);
                self.state.hourly_variance = vec![v; 24];
                self.state.hourly_diff = vec![d; 24];
                (v, d)
            };

        self.state.base_variance = round_n(eff_var, 7);
        self.state.base_diff = round_n(eff_diff, 6);
        self.state.hourly_variance[hour] = round_n(eff_var, 7);
        self.state.hourly_diff[hour] = round_n(eff_diff, 6);
        if !self.state.learning_days.contains(&day) {
            self.state.learning_days.push(day);
        }
        self.has_persisted = true;
        self.save();
        (self.state.base_variance, self.state.base_diff, true)
    }

    pub fn observe_frame(
        &mut self,
        is_quiet: bool,
        mean_variance: f64,
        diff_energy: f64,
        ts: f64,
    ) -> Option<(f64, f64)> {
        if !is_quiet
            || mean_variance > MAX_CLEAN_BOOT_VARIANCE
            || diff_energy > MAX_CLEAN_BOOT_DIFF
        {
            self.quiet_start = None;
            self.quiet_vars.clear();
            self.quiet_diffs.clear();
            return None;
        }
        if self.quiet_start.is_none() {
            self.quiet_start = Some(ts);
        }
        self.quiet_vars.push(mean_variance);
        self.quiet_diffs.push(diff_energy);

        let elapsed = ts - self.quiet_start.unwrap();
        if elapsed < self.quiet_window_min_seconds || self.quiet_vars.len() < 10 {
            return None;
        }

        let mut sv = self.quiet_vars.clone();
        let mut sd = self.quiet_diffs.clone();
        sv.sort_by(|a, b| a.partial_cmp(b).unwrap());
        sd.sort_by(|a, b| a.partial_cmp(b).unwrap());
        let med_v = sv[sv.len() / 2].max(1e-6);
        let med_d = sd[sd.len() / 2].max(1e-5);

        let (hour, day) = utc_hour_day(ts);
        let alpha = self.ema_alpha;
        let new_base_v = (1.0 - alpha) * self.state.base_variance + alpha * med_v;
        let new_base_d = (1.0 - alpha) * self.state.base_diff + alpha * med_d;
        let hour_v =
            (1.0 - alpha * 1.5) * self.state.hourly_variance[hour] + (alpha * 1.5) * med_v;
        let hour_d =
            (1.0 - alpha * 1.5) * self.state.hourly_diff[hour] + (alpha * 1.5) * med_d;

        self.state.base_variance = round_n(new_base_v, 7);
        self.state.base_diff = round_n(new_base_d, 6);
        self.state.hourly_variance[hour] = round_n(hour_v, 7);
        self.state.hourly_diff[hour] = round_n(hour_d, 6);
        self.state.quiet_windows_learned += 1;
        self.state.drift_adaptations_count += 1;
        if !self.state.learning_days.contains(&day) {
            self.state.learning_days.push(day);
        }
        self.quiet_start = Some(ts);
        self.quiet_vars.clear();
        self.quiet_diffs.clear();
        self.save();
        Some((self.state.base_variance, self.state.base_diff))
    }

    pub fn status_summary(&self) -> CalSummary {
        let days = self.state.learning_days.len();
        let maturity = round_n(
            days as f64 * 25.0 + (self.state.quiet_windows_learned as f64 * 1.25).min(25.0),
            1,
        )
        .min(100.0);
        CalSummary {
            base_variance: self.state.base_variance,
            base_diff: self.state.base_diff,
            learning_days: days,
            quiet_windows_learned: self.state.quiet_windows_learned,
            maturity_percent: maturity,
            boot_contamination_rejected: self.state.boot_contamination_rejected,
            drift_adaptations_count: self.state.drift_adaptations_count,
            last_updated: if self.state.last_updated.is_empty() {
                "刚刚".to_string()
            } else {
                self.state.last_updated.clone()
            },
        }
    }
}