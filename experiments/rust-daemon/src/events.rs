// Structured event log + in-memory ring, compatible with the Python service's
// logs/events.jsonl schema. Also drives the SSE stream via the shared Condvar.

use std::collections::VecDeque;
use std::fs;
use std::io::Write;
use std::path::{Path, PathBuf};

use crate::json;
use crate::timefmt;

const RING_CAP: usize = 512;
const MAX_BYTES: u64 = 2 * 1024 * 1024;
const BACKUPS: usize = 5;

#[derive(Clone)]
pub struct EventRecord {
    pub seq: u64,
    pub timestamp: String,
    pub time_short: String,
    pub event_type: String,
    pub level: String,
    pub message: String,
    pub data: String,
}

impl EventRecord {
    pub fn to_json(&self) -> String {
        format!(
            "{{\"seq\":{},\"timestamp\":\"{}\",\"time_short\":\"{}\",\"type\":\"{}\",\"level\":\"{}\",\"message\":\"{}\",\"data\":{}}}",
            self.seq,
            json::escape(&self.timestamp),
            json::escape(&self.time_short),
            json::escape(&self.event_type),
            json::escape(&self.level),
            json::escape(&self.message),
            if self.data.is_empty() { "{}" } else { &self.data }
        )
    }

    pub fn brief_json(&self) -> String {
        format!(
            "{{\"time\":\"{}\",\"text\":\"{}\"}}",
            json::escape(&self.time_short),
            json::escape(&self.message)
        )
    }
}

pub struct EventLog {
    dir: PathBuf,
    events_path: PathBuf,
    log_path: PathBuf,
    ring: VecDeque<EventRecord>,
    seq: u64,
}

fn rotate(path: &Path) {
    let too_big = fs::metadata(path).map(|m| m.len() >= MAX_BYTES).unwrap_or(false);
    if !too_big {
        return;
    }
    let name = path.file_name().and_then(|s| s.to_str()).unwrap_or("events.jsonl");
    for i in (1..BACKUPS).rev() {
        let from = path.with_file_name(format!("{}.{}", name, i));
        let to = path.with_file_name(format!("{}.{}", name, i + 1));
        if from.exists() {
            let _ = fs::rename(&from, &to);
        }
    }
    let _ = fs::rename(path, path.with_file_name(format!("{}.1", name)));
}

impl EventLog {
    pub fn new(dir: &str) -> EventLog {
        let _ = fs::create_dir_all(dir);
        let dir = PathBuf::from(dir);
        let events_path = dir.join("events.jsonl");
        let log_path = dir.join("safehalo.log");
        let mut ring = VecDeque::new();
        let mut seq = 0u64;
        if let Ok(text) = fs::read_to_string(&events_path) {
            let lines: Vec<&str> = text.lines().filter(|l| !l.trim().is_empty()).collect();
            let start = lines.len().saturating_sub(RING_CAP);
            for line in &lines[start..] {
                if let Some(v) = json::parse(line) {
                    seq += 1;
                    ring.push_back(EventRecord {
                        seq,
                        timestamp: v.get("timestamp").and_then(|x| x.as_str()).unwrap_or("").to_string(),
                        time_short: v.get("time_short").and_then(|x| x.as_str()).unwrap_or("").to_string(),
                        event_type: v.get("type").and_then(|x| x.as_str()).unwrap_or("EVENT").to_string(),
                        level: v.get("level").and_then(|x| x.as_str()).unwrap_or("INFO").to_string(),
                        message: v.get("message").and_then(|x| x.as_str()).unwrap_or("").to_string(),
                        data: v.get("data").map(json::write).unwrap_or_else(|| "{}".into()),
                    });
                }
            }
        }
        EventLog {
            dir,
            events_path,
            log_path,
            ring,
            seq,
        }
    }

    pub fn push(&mut self, event_type: &str, level: &str, message: &str, data: &str) -> EventRecord {
        self.seq += 1;
        let st = timefmt::now();
        let rec = EventRecord {
            seq: self.seq,
            timestamp: st.date_time.clone(),
            time_short: st.time_short.clone(),
            event_type: event_type.to_string(),
            level: level.to_string(),
            message: message.to_string(),
            data: data.to_string(),
        };
        if self.ring.len() >= RING_CAP {
            self.ring.pop_front();
        }
        self.ring.push_back(rec.clone());
        self.append_file(&rec);
        rec
    }

    fn append_file(&mut self, rec: &EventRecord) {
        rotate(&self.events_path);
        rotate(&self.log_path);
        if let Ok(mut f) = fs::OpenOptions::new().create(true).append(true).open(&self.events_path) {
            let _ = writeln!(f, "{}", rec.to_json());
            #[cfg(unix)]
            {
                use std::os::unix::fs::PermissionsExt;
                let _ = fs::set_permissions(&self.events_path, fs::Permissions::from_mode(0o600));
            }
        }
        if let Ok(mut f) = fs::OpenOptions::new().create(true).append(true).open(&self.log_path) {
            let _ = writeln!(
                f,
                "[{}] [{}] [{}] {}",
                &rec.timestamp[..19.min(rec.timestamp.len())],
                rec.level,
                rec.event_type,
                rec.message
            );
            #[cfg(unix)]
            {
                use std::os::unix::fs::PermissionsExt;
                let _ = fs::set_permissions(&self.log_path, fs::Permissions::from_mode(0o600));
            }
        }
    }

    pub fn latest_seq(&self) -> u64 {
        self.seq
    }

    pub fn since(&self, after: u64, limit: usize) -> Vec<EventRecord> {
        self.ring
            .iter()
            .filter(|e| e.seq > after)
            .take(limit)
            .cloned()
            .collect()
    }

    pub fn recent(&self, limit: usize, type_filter: Option<&str>) -> Vec<EventRecord> {
        self.ring
            .iter()
            .rev()
            .filter(|e| match type_filter {
                Some(t) if t != "ALL" => e.event_type == t,
                _ => true,
            })
            .take(limit)
            .cloned()
            .collect()
    }

    pub fn tail(&self, lines: usize) -> Vec<String> {
        let text = fs::read_to_string(&self.log_path).unwrap_or_default();
        let all: Vec<&str> = text.lines().collect();
        let start = all.len().saturating_sub(lines);
        if start == all.len() {
            return vec!["(暂无日志记录)".into()];
        }
        all[start..].iter().map(|s| s.to_string()).collect()
    }

    pub fn log_size(&self) -> u64 {
        fs::metadata(&self.log_path).map(|m| m.len()).unwrap_or(0)
    }

    pub fn log_path(&self) -> &Path {
        &self.log_path
    }

    pub fn clear(&mut self) {
        self.ring.clear();
        let _ = fs::File::create(&self.log_path);
        let _ = fs::File::create(&self.events_path);
    }

    pub fn dir(&self) -> &Path {
        &self.dir
    }
}
