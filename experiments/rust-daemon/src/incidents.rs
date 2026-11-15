// Port of IncidentFeedbackManager storage: logs/incidents.jsonl with the same
// AlertIncidentRecord fields so Python tools and the mini-program backend keep
// reading Rust-produced records.

use std::fs;
use std::io::Write;
use std::path::{Path, PathBuf};

use crate::auth::random_bytes;
use crate::json;
use crate::timefmt;

pub const STATUS_PENDING: &str = "PENDING";
pub const STATUS_CONFIRMED_TRUE: &str = "CONFIRMED_TRUE";
pub const STATUS_FALSE_ALARM: &str = "FALSE_ALARM";
#[allow(dead_code)]
pub const STATUS_CHALLENGE_INTERCEPTED: &str = "CHALLENGE_INTERCEPTED";

#[derive(Clone)]
pub struct Incident {
    pub event_id: String,
    pub timestamp: f64,
    pub time_str: String,
    pub household_id: String,
    pub room_name: String,
    pub event_type: String,
    pub confidence: f64,
    pub respiration_bpm: f64,
    pub details: String,
    pub status: String,
    pub feedback_category: String,
    pub feedback_notes: String,
    pub feedback_by: String,
    pub feedback_at: String,
    pub miniprogram_pushed: bool,
}

impl Incident {
    pub fn to_json(&self) -> String {
        format!(
            "{{\"event_id\":\"{}\",\"timestamp\":{},\"time_str\":\"{}\",\"household_id\":\"{}\",\"room_name\":\"{}\",\"event_type\":\"{}\",\"confidence\":{},\"respiration_bpm\":{},\"details\":\"{}\",\"status\":\"{}\",\"feedback_category\":\"{}\",\"feedback_notes\":\"{}\",\"feedback_by\":\"{}\",\"feedback_at\":\"{}\",\"miniprogram_pushed\":{}}}",
            json::escape(&self.event_id),
            self.timestamp,
            json::escape(&self.time_str),
            json::escape(&self.household_id),
            json::escape(&self.room_name),
            json::escape(&self.event_type),
            self.confidence,
            self.respiration_bpm,
            json::escape(&self.details),
            json::escape(&self.status),
            json::escape(&self.feedback_category),
            json::escape(&self.feedback_notes),
            json::escape(&self.feedback_by),
            json::escape(&self.feedback_at),
            self.miniprogram_pushed
        )
    }
}

pub struct IncidentStore {
    file: PathBuf,
    pub items: Vec<Incident>,
}

fn s(v: &json::Json, k: &str) -> String {
    v.get(k).and_then(|x| x.as_str()).unwrap_or("").to_string()
}

fn f(v: &json::Json, k: &str) -> f64 {
    v.get(k).and_then(|x| x.as_f64()).unwrap_or(0.0)
}

impl IncidentStore {
    pub fn new(dir: &str) -> IncidentStore {
        let _ = fs::create_dir_all(dir);
        let file = Path::new(dir).join("incidents.jsonl");
        let mut items = Vec::new();
        if let Ok(text) = fs::read_to_string(&file) {
            for line in text.lines().filter(|l| !l.trim().is_empty()) {
                if let Some(v) = json::parse(line) {
                    items.push(Incident {
                        event_id: s(&v, "event_id"),
                        timestamp: f(&v, "timestamp"),
                        time_str: s(&v, "time_str"),
                        household_id: s(&v, "household_id"),
                        room_name: s(&v, "room_name"),
                        event_type: s(&v, "event_type"),
                        confidence: f(&v, "confidence"),
                        respiration_bpm: f(&v, "respiration_bpm"),
                        details: s(&v, "details"),
                        status: {
                            let st = s(&v, "status");
                            if st.is_empty() { STATUS_PENDING.into() } else { st }
                        },
                        feedback_category: s(&v, "feedback_category"),
                        feedback_notes: s(&v, "feedback_notes"),
                        feedback_by: s(&v, "feedback_by"),
                        feedback_at: s(&v, "feedback_at"),
                        miniprogram_pushed: v.get("miniprogram_pushed").and_then(|x| x.as_bool()).unwrap_or(false),
                    });
                }
            }
        }
        IncidentStore { file, items }
    }

    fn append_record(&self, rec: &Incident) {
        if let Ok(mut f) = fs::OpenOptions::new().create(true).append(true).open(&self.file) {
            let _ = writeln!(f, "{}", rec.to_json());
            #[cfg(unix)]
            {
                use std::os::unix::fs::PermissionsExt;
                let _ = fs::set_permissions(&self.file, fs::Permissions::from_mode(0o600));
            }
        }
    }

    fn save(&self) {
        let tmp = self.file.with_extension("jsonl.tmp");
        if let Ok(mut f) = fs::File::create(&tmp) {
            for rec in &self.items {
                let _ = writeln!(f, "{}", rec.to_json());
            }
            let _ = fs::rename(&tmp, &self.file);
            #[cfg(unix)]
            {
                use std::os::unix::fs::PermissionsExt;
                let _ = fs::set_permissions(&self.file, fs::Permissions::from_mode(0o600));
            }
        }
    }

    pub fn record(
        &mut self,
        room: &str,
        event_type: &str,
        confidence: f64,
        bpm: f64,
        details: &str,
        household: &str,
    ) -> Incident {
        let now = timefmt::now_epoch();
        let st = timefmt::format_epoch(now);
        let compact: String = st.date_time[..19]
            .chars()
            .filter(|c| c.is_ascii_digit())
            .collect();
        let id_time = format!("{}_{}", &compact[..8], &compact[8..14]);
        let rec = Incident {
            event_id: format!("evt_{}_{}", id_time, hex6(&random_bytes(3))),
            timestamp: now,
            time_str: st.date_time[..19].to_string(),
            household_id: household.to_string(),
            room_name: room.to_string(),
            event_type: event_type.to_string(),
            confidence: (confidence * 100.0).round() / 100.0,
            respiration_bpm: (bpm * 10.0).round() / 10.0,
            details: details.to_string(),
            status: STATUS_PENDING.to_string(),
            feedback_category: String::new(),
            feedback_notes: String::new(),
            feedback_by: String::new(),
            feedback_at: String::new(),
            miniprogram_pushed: false,
        };
        self.items.push(rec.clone());
        if self.items.len() > 500 {
            self.items.remove(0);
            self.save();
        } else {
            self.append_record(&rec);
        }
        rec
    }

    pub fn mark_pushed(&mut self, event_id: &str, pushed: bool) {
        if let Some(i) = self.items.iter().position(|r| r.event_id == event_id) {
            self.items[i].miniprogram_pushed = pushed;
            self.save();
        }
    }

    pub fn list_recent(&self, limit: usize) -> Vec<Incident> {
        self.items.iter().rev().take(limit).cloned().collect()
    }

    pub fn submit_feedback(
        &mut self,
        event_id: Option<&str>,
        is_false: bool,
        category: &str,
        notes: &str,
        operator: &str,
    ) -> (bool, String, Option<Incident>) {
        let idx = match event_id {
            Some(id) => self.items.iter().position(|r| r.event_id == id),
            None => self.items.len().checked_sub(1),
        };
        let idx = match idx {
            Some(i) => i,
            None => return (false, "未找到可反馈的告警事件".into(), None),
        };
        let rec = &mut self.items[idx];
        rec.status = if is_false {
            STATUS_FALSE_ALARM.into()
        } else {
            STATUS_CONFIRMED_TRUE.into()
        };
        if is_false {
            rec.feedback_category = category.to_string();
        }
        rec.feedback_notes = notes.to_string();
        rec.feedback_by = operator.to_string();
        rec.feedback_at = timefmt::now().date_time[..19].to_string();
        let out = rec.clone();
        self.save();
        let msg = if is_false {
            "已记录误报反馈，系统将据此自适应调整"
        } else {
            "已确认真实告警事件"
        };
        (true, msg.to_string(), Some(out))
    }
}

fn hex6(bytes: &[u8]) -> String {
    let mut s = String::new();
    for b in bytes {
        s.push_str(&format!("{:02x}", b));
    }
    s
}
