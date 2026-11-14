// SecurityGuard: port of src/safehalo/security_guard.py
// Intelligent security & intrusion detection leveraging the same Wi-Fi CSI sensing.
// Supports:
// - FALL_GUARD (DISARMED): Normal fall monitoring, intrusion alerts disabled
// - ARMED_AWAY: House should be empty; alerts if human moving (>=2 frames) or lurking (>=10 frames static)
// - ARMED_NIGHT: Night perimeter mode; permits static sleep breathing, alerts on persistent walking (>=3 frames)
// - CURFEW: Senior anti-wandering mode; alerts if monitored area remains empty for >= timeout

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum SecurityMode {
    FallGuard,
    ArmedAway,
    ArmedNight,
    Curfew,
}

impl SecurityMode {
    pub fn parse(s: &str) -> Self {
        match s.trim().to_ascii_uppercase().as_str() {
            "ARMED_AWAY" | "ARM_AWAY" | "AWAY" => SecurityMode::ArmedAway,
            "ARMED_NIGHT" | "ARM_NIGHT" | "NIGHT" | "ARM_HOME" => SecurityMode::ArmedNight,
            "CURFEW" => SecurityMode::Curfew,
            _ => SecurityMode::FallGuard,
        }
    }

    pub fn as_str(&self) -> &'static str {
        match self {
            SecurityMode::FallGuard => "FALL_GUARD",
            SecurityMode::ArmedAway => "ARMED_AWAY",
            SecurityMode::ArmedNight => "ARMED_NIGHT",
            SecurityMode::Curfew => "CURFEW",
        }
    }
}

pub struct SecurityGuard {
    pub mode: SecurityMode,
    arm_time: Option<f64>,
    grace_period_secs: f64,
    curfew_empty_timeout_secs: f64,
    consecutive_human_frames: u32,
    last_alarm_time: Option<f64>,
    empty_start_time: Option<f64>,
}

#[derive(Clone, Debug, PartialEq)]
pub struct SecurityAlert {
    pub level: &'static str, // "ALARM" or "SUSPECT"
    pub title: String,
    pub message: String,
}

impl SecurityGuard {
    pub fn new(initial_mode: SecurityMode) -> Self {
        SecurityGuard {
            mode: initial_mode,
            arm_time: None,
            grace_period_secs: 15.0,
            curfew_empty_timeout_secs: 300.0,
            consecutive_human_frames: 0,
            last_alarm_time: None,
            empty_start_time: None,
        }
    }

    pub fn set_mode(&mut self, new_mode: SecurityMode) {
        self.mode = new_mode;
        self.arm_time = None;
        self.consecutive_human_frames = 0;
        self.empty_start_time = None;
    }

    pub fn evaluate(&mut self, state: &str, now: f64, diff_energy: f64) -> Option<SecurityAlert> {
        let _ = diff_energy;
        // In FALL_GUARD / DISARMED mode, intrusion detection is inactive
        if self.mode == SecurityMode::FallGuard {
            return None;
        }

        if self.arm_time.is_none() {
            self.arm_time = Some(now);
        }

        // Grace period (allow occupant time to leave)
        if self.grace_period_secs > 0.0 && (now - self.arm_time.unwrap()) < self.grace_period_secs {
            return None;
        }

        // Alarm cooldown (30 seconds between successive alarms)
        if let Some(last) = self.last_alarm_time {
            if (now - last) < 30.0 {
                return None;
            }
        }

        // Mode: CURFEW (Senior anti-wandering)
        if self.mode == SecurityMode::Curfew {
            if state == "EMPTY" {
                if self.empty_start_time.is_none() {
                    self.empty_start_time = Some(now);
                } else if (now - self.empty_start_time.unwrap()) >= self.curfew_empty_timeout_secs {
                    self.last_alarm_time = Some(now);
                    let secs = (now - self.empty_start_time.unwrap()) as u64;
                    return Some(SecurityAlert {
                        level: "ALARM",
                        title: "⚠️【防走失告警】老人离家超时未归！".into(),
                        message: format!("防走失布防告警：监测区域已连续 {} 秒无人，请关注老人是否深夜开门外出！", secs),
                    });
                }
            } else if matches!(state, "MOVING" | "STATIC" | "FALL_SUSPECT" | "FALL_CONFIRMED") {
                self.empty_start_time = None;
            }
            return None;
        }

        // In active intrusion modes, pet activity or empty ignores intrusion
        if matches!(state, "EMPTY" | "PET_ACTIVITY" | "CALIBRATING") {
            self.consecutive_human_frames = 0;
            return None;
        }

        // Mode: ARMED_AWAY (House should be completely empty)
        if self.mode == SecurityMode::ArmedAway {
            if state == "MOVING" {
                self.consecutive_human_frames += 1;
                if self.consecutive_human_frames >= 2 {
                    self.last_alarm_time = Some(now);
                    return Some(SecurityAlert {
                        level: "ALARM",
                        title: "🛡️【防盗告警】SafeHalo 检测到非法人员入侵！".into(),
                        message: "离家布防模式下检测到人员走动入侵！".into(),
                    });
                }
            } else if state == "STATIC" {
                self.consecutive_human_frames += 1;
                if self.consecutive_human_frames >= 10 {
                    self.last_alarm_time = Some(now);
                    return Some(SecurityAlert {
                        level: "SUSPECT",
                        title: "🛡️【可疑微动】SafeHalo 检测到人员潜伏！".into(),
                        message: "离家布防模式下检测到人员潜伏静止 (呼吸微动)！".into(),
                    });
                }
            }
        } else if self.mode == SecurityMode::ArmedNight {
            // Mode: ARMED_NIGHT (Allow sleeping breathing, alert on persistent movement)
            if state == "MOVING" {
                self.consecutive_human_frames += 1;
                if self.consecutive_human_frames >= 3 {
                    self.last_alarm_time = Some(now);
                    return Some(SecurityAlert {
                        level: "ALARM",
                        title: "🛡️【夜间防盗】SafeHalo 检测到异常走动！".into(),
                        message: "夜间布防模式下检测到持续异常走动闯入！".into(),
                    });
                }
            } else {
                self.consecutive_human_frames = 0;
            }
        }

        None
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_mode_parse_and_str() {
        assert_eq!(SecurityMode::parse("fall_guard"), SecurityMode::FallGuard);
        assert_eq!(SecurityMode::parse("disarmed"), SecurityMode::FallGuard);
        assert_eq!(SecurityMode::parse("armed_away"), SecurityMode::ArmedAway);
        assert_eq!(SecurityMode::parse("away"), SecurityMode::ArmedAway);
        assert_eq!(SecurityMode::parse("armed_night"), SecurityMode::ArmedNight);
        assert_eq!(SecurityMode::parse("night"), SecurityMode::ArmedNight);
        assert_eq!(SecurityMode::parse("curfew"), SecurityMode::Curfew);
        assert_eq!(SecurityMode::ArmedAway.as_str(), "ARMED_AWAY");
    }

    #[test]
    fn test_armed_away_moving_triggers_alarm() {
        let mut guard = SecurityGuard::new(SecurityMode::ArmedAway);
        guard.grace_period_secs = 0.0; // disable grace period for testing

        // First frame moving -> no alert yet (< 2 frames)
        let a1 = guard.evaluate("MOVING", 100.0, 0.05);
        assert!(a1.is_none());

        // Second frame moving -> ALARM triggered
        let a2 = guard.evaluate("MOVING", 101.0, 0.05);
        assert!(a2.is_some());
        let alert = a2.unwrap();
        assert_eq!(alert.level, "ALARM");
        assert!(alert.message.contains("离家布防模式下检测到人员走动入侵"));

        // Third frame moving within 30s cooldown -> None
        let a3 = guard.evaluate("MOVING", 102.0, 0.05);
        assert!(a3.is_none());
    }

    #[test]
    fn test_pet_does_not_trigger_intrusion() {
        let mut guard = SecurityGuard::new(SecurityMode::ArmedAway);
        guard.grace_period_secs = 0.0;
        for i in 0..15 {
            let a = guard.evaluate("PET_ACTIVITY", 100.0 + i as f64, 0.02);
            assert!(a.is_none());
        }
    }

    #[test]
    fn test_curfew_timeout_alert() {
        let mut guard = SecurityGuard::new(SecurityMode::Curfew);
        guard.grace_period_secs = 0.0;
        guard.curfew_empty_timeout_secs = 10.0;

        // Empty started at t=100
        assert!(guard.evaluate("EMPTY", 100.0, 0.001).is_none());
        // Still empty at t=105 -> no alert yet
        assert!(guard.evaluate("EMPTY", 105.0, 0.001).is_none());
        // Empty reaches 10s at t=110 -> ALARM
        let a = guard.evaluate("EMPTY", 110.0, 0.001);
        assert!(a.is_some());
        assert!(a.unwrap().title.contains("防走失告警"));
    }
}
