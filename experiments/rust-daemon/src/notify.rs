// Alert fan-out: incident push to PushPlus / ServerChan / generic webhook /
// Home Assistant / SMS gateway / mini-program backend.
//
// Features:
// - Plain HTTP/1.1 in-tree (zero dependencies) + HTTPS via curl/wget/uclient-fetch
// - Persistent retry queue (pending_notifications.jsonl) surviving reboots
// - Exponential backoff with bounded retries and dead-letter queue (dead_letters.jsonl)
// - 30-second deduplication per room (except for manual test events)
// - Payload alignment (event_id, confidence float, respiration_bpm, details, timestamp)

use std::fs;
use std::io::{Read, Write};
use std::net::{TcpStream, ToSocketAddrs};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::mpsc::{self, Sender};
use std::sync::{Arc, Condvar, Mutex};
use std::time::{Duration, Instant};

use crate::json;
use crate::timefmt;

pub const CHAN_PUSHPLUS: &str = "pushplus";
pub const CHAN_SERVERCHAN: &str = "serverchan";
pub const CHAN_WEBHOOK: &str = "webhook";
pub const CHAN_HA_WEBHOOK: &str = "ha_webhook";
pub const CHAN_SMS_WEBHOOK: &str = "sms_webhook";
pub const CHAN_MINIPROGRAM: &str = "miniprogram";
pub const CHAN_BARK: &str = "bark";

pub const MAX_RETRIES: u32 = 5;

pub fn calculate_backoff(attempt: u32) -> Duration {
    match attempt {
        0 => Duration::from_millis(0),
        1 => Duration::from_secs(2),
        2 => Duration::from_secs(5),
        3 => Duration::from_secs(15),
        4 => Duration::from_secs(30),
        _ => Duration::from_secs(60),
    }
}

#[derive(Clone, Default)]
pub struct NotifyConfig {
    pub pushplus_token: String,
    pub serverchan_key: String,
    pub webhook_url: String,
    pub ha_webhook_url: String,
    pub sms_webhook_url: String,
    pub miniprogram_url: String,
    pub miniprogram_token: String,
    pub bark_key: String,
    pub timeout_secs: u64,
}

impl NotifyConfig {
    pub fn configured_channel_names(&self) -> Vec<&'static str> {
        let mut list = Vec::new();
        if !self.pushplus_token.is_empty() {
            list.push(CHAN_PUSHPLUS);
        }
        if !self.serverchan_key.is_empty() {
            list.push(CHAN_SERVERCHAN);
        }
        if !self.webhook_url.is_empty() {
            list.push(CHAN_WEBHOOK);
        }
        if !self.ha_webhook_url.is_empty() {
            list.push(CHAN_HA_WEBHOOK);
        }
        if !self.sms_webhook_url.is_empty() {
            list.push(CHAN_SMS_WEBHOOK);
        }
        if !self.miniprogram_url.is_empty() {
            list.push(CHAN_MINIPROGRAM);
        }
        if !self.bark_key.is_empty() {
            list.push(CHAN_BARK);
        }
        list
    }

    pub fn any_channel(&self) -> bool {
        !self.configured_channel_names().is_empty()
    }

    pub fn load(dir: &str) -> NotifyConfig {
        let path = PathBuf::from(dir).join("notifier_config.json");
        let mut cfg = NotifyConfig {
            timeout_secs: 5,
            ..Default::default()
        };
        if let Ok(text) = fs::read_to_string(path) {
            if let Some(v) = json::parse(&text) {
                let g = |k: &str| v.get(k).and_then(|x| x.as_str()).unwrap_or("").to_string();
                cfg.pushplus_token = g("pushplus_token");
                cfg.serverchan_key = g("serverchan_key");
                cfg.webhook_url = g("webhook_url");
                cfg.ha_webhook_url = g("ha_webhook_url");
                cfg.sms_webhook_url = g("sms_webhook_url");
                cfg.miniprogram_url = g("miniprogram_url");
                cfg.miniprogram_token = g("miniprogram_token");
                cfg.bark_key = g("bark_key");
                if let Some(t) = v.get("timeout_secs").and_then(|x| x.as_u64()) {
                    if t > 0 {
                        cfg.timeout_secs = t;
                    }
                }
            }
        }
        cfg
    }

    pub fn save(&self, dir: &str) {
        let _ = fs::create_dir_all(dir);
        let text = format!(
            "{{\n  \"pushplus_token\": \"{}\",\n  \"serverchan_key\": \"{}\",\n  \"webhook_url\": \"{}\",\n  \"ha_webhook_url\": \"{}\",\n  \"sms_webhook_url\": \"{}\",\n  \"miniprogram_url\": \"{}\",\n  \"miniprogram_token\": \"{}\",\n  \"bark_key\": \"{}\",\n  \"timeout_secs\": {}\n}}\n",
            json::escape(&self.pushplus_token),
            json::escape(&self.serverchan_key),
            json::escape(&self.webhook_url),
            json::escape(&self.ha_webhook_url),
            json::escape(&self.sms_webhook_url),
            json::escape(&self.miniprogram_url),
            json::escape(&self.miniprogram_token),
            json::escape(&self.bark_key),
            self.timeout_secs
        );
        let path = PathBuf::from(dir).join("notifier_config.json");
        let _ = fs::write(&path, text);
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            let _ = fs::set_permissions(&path, fs::Permissions::from_mode(0o600));
        }
    }
}

#[derive(Clone, Debug, PartialEq)]
pub enum Notice {
    Fall {
        room: String,
        confidence: f64,
        details: String,
        bpm: f64,
        event_id: String,
        household: String,
    },
    Generic {
        title: String,
        content: String,
        room: String,
        ha_event: String,
    },
}

impl Notice {
    pub fn to_json(&self) -> String {
        match self {
            Notice::Fall {
                room,
                confidence,
                details,
                bpm,
                event_id,
                household,
            } => {
                format!(
                    "{{\"type\":\"fall\",\"room\":\"{}\",\"confidence\":{},\"details\":\"{}\",\"bpm\":{},\"event_id\":\"{}\",\"household\":\"{}\"}}",
                    json::escape(room),
                    confidence,
                    json::escape(details),
                    bpm,
                    json::escape(event_id),
                    json::escape(household)
                )
            }
            Notice::Generic {
                title,
                content,
                room,
                ha_event,
            } => {
                format!(
                    "{{\"type\":\"generic\",\"title\":\"{}\",\"content\":\"{}\",\"room\":\"{}\",\"ha_event\":\"{}\"}}",
                    json::escape(title),
                    json::escape(content),
                    json::escape(room),
                    json::escape(ha_event)
                )
            }
        }
    }

    pub fn from_json(v: &json::Json) -> Option<Notice> {
        let t = v.get("type").and_then(|x| x.as_str())?;
        match t {
            "fall" => {
                let room = v.get("room").and_then(|x| x.as_str()).unwrap_or("").to_string();
                let confidence = v.get("confidence").and_then(|x| x.as_f64()).unwrap_or(0.96);
                let details = v.get("details").and_then(|x| x.as_str()).unwrap_or("").to_string();
                let bpm = v.get("bpm").and_then(|x| x.as_f64()).unwrap_or(18.0);
                let event_id = v.get("event_id").and_then(|x| x.as_str()).unwrap_or("").to_string();
                let household = v.get("household").and_then(|x| x.as_str()).unwrap_or("H01").to_string();
                Some(Notice::Fall {
                    room,
                    confidence,
                    details,
                    bpm,
                    event_id,
                    household,
                })
            }
            "generic" => {
                let title = v.get("title").and_then(|x| x.as_str()).unwrap_or("").to_string();
                let content = v.get("content").and_then(|x| x.as_str()).unwrap_or("").to_string();
                let room = v.get("room").and_then(|x| x.as_str()).unwrap_or("").to_string();
                let ha_event = v.get("ha_event").and_then(|x| x.as_str()).unwrap_or("").to_string();
                Some(Notice::Generic {
                    title,
                    content,
                    room,
                    ha_event,
                })
            }
            _ => None,
        }
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct PendingNotification {
    pub id: String,
    pub notice: Notice,
    pub attempts: u32,
    pub created_at: f64,
    pub next_retry_at: f64,
    pub failed_channels: Vec<String>,
}

impl PendingNotification {
    pub fn to_json_line(&self) -> String {
        let chs = self
            .failed_channels
            .iter()
            .map(|c| format!("\"{}\"", json::escape(c)))
            .collect::<Vec<_>>()
            .join(",");
        format!(
            "{{\"id\":\"{}\",\"attempts\":{},\"created_at\":{},\"next_retry_at\":{},\"failed_channels\":[{}],\"notice\":{}}}\n",
            json::escape(&self.id),
            self.attempts,
            self.created_at,
            self.next_retry_at,
            chs,
            self.notice.to_json()
        )
    }

    pub fn from_json(v: &json::Json) -> Option<PendingNotification> {
        let id = v.get("id").and_then(|x| x.as_str())?.to_string();
        let attempts = v.get("attempts").and_then(|x| x.as_u64()).unwrap_or(0) as u32;
        let created_at = v.get("created_at").and_then(|x| x.as_f64()).unwrap_or(0.0);
        let next_retry_at = v.get("next_retry_at").and_then(|x| x.as_f64()).unwrap_or(0.0);
        let failed_channels = v
            .get("failed_channels")
            .and_then(|x| x.as_arr())
            .map(|arr| {
                arr.iter()
                    .filter_map(|x| x.as_str().map(String::from))
                    .collect()
            })
            .unwrap_or_default();
        let n_obj = v.get("notice")?;
        let notice = Notice::from_json(n_obj)?;
        Some(PendingNotification {
            id,
            notice,
            attempts,
            created_at,
            next_retry_at,
            failed_channels,
        })
    }
}

pub fn load_pending_file(dir: &str) -> Vec<PendingNotification> {
    let path = Path::new(dir).join("pending_notifications.jsonl");
    let mut list = Vec::new();
    if let Ok(text) = fs::read_to_string(&path) {
        let now = now_epoch_secs();
        for line in text.lines().filter(|l| !l.trim().is_empty()) {
            if let Some(v) = json::parse(line) {
                if let Some(mut item) = PendingNotification::from_json(&v) {
                    item.next_retry_at = now;
                    list.push(item);
                }
            }
        }
    }
    list
}

pub fn save_pending_file(dir: &str, items: &[PendingNotification]) {
    let path = Path::new(dir).join("pending_notifications.jsonl");
    if items.is_empty() {
        let _ = fs::remove_file(path);
        return;
    }
    let mut buf = String::new();
    for it in items {
        buf.push_str(&it.to_json_line());
    }
    let tmp = Path::new(dir).join("pending_notifications.jsonl.tmp");
    if fs::write(&tmp, &buf).is_ok() {
        let _ = fs::rename(tmp, path);
    }
}

pub fn append_dead_letter(dir: &str, item: &PendingNotification, reason: &str) {
    let path = Path::new(dir).join("dead_letters.jsonl");
    let chs = item
        .failed_channels
        .iter()
        .map(|c| format!("\"{}\"", json::escape(c)))
        .collect::<Vec<_>>()
        .join(",");
    let line = format!(
        "{{\"id\":\"{}\",\"dropped_at\":{},\"attempts\":{},\"reason\":\"{}\",\"failed_channels\":[{}],\"notice\":{}}}\n",
        json::escape(&item.id),
        now_epoch_secs(),
        item.attempts,
        json::escape(reason),
        chs,
        item.notice.to_json()
    );
    if let Ok(mut f) = fs::OpenOptions::new().create(true).append(true).open(path) {
        let _ = f.write_all(line.as_bytes());
    }
}

#[derive(Clone)]
pub struct Notifier {
    tx: Sender<Notice>,
    cfg: Arc<Mutex<NotifyConfig>>,
    dir: Arc<String>,
    pending: Arc<(Mutex<usize>, Condvar)>,
}

impl Notifier {
    pub fn new(cfg: NotifyConfig, dir: &str) -> Notifier {
        let dir = Arc::new(dir.to_string());
        let cfg = Arc::new(Mutex::new(cfg.clone()));
        let pending = Arc::new((Mutex::new(0usize), Condvar::new()));
        let (tx, rx) = mpsc::channel::<Notice>();
        let worker_dir = Arc::clone(&dir);
        let worker_cfg = Arc::clone(&cfg);
        let worker_pending = Arc::clone(&pending);
        let dedupe = Arc::new(Mutex::new((0.0f64, String::new())));

        std::thread::Builder::new()
            .name("notify".into())
            .stack_size(256 * 1024)
            .spawn(move || {
                let mut pending_items = load_pending_file(&worker_dir);
                if !pending_items.is_empty() {
                    eprintln!(
                        "[notify] recovered {} pending notifications from disk",
                        pending_items.len()
                    );
                    let (lock, _) = &*worker_pending;
                    let mut n = lock.lock().unwrap_or_else(|e| e.into_inner());
                    *n += pending_items.len();
                }

                loop {
                    let now = now_epoch_secs();
                    let mut timeout = Duration::from_millis(500);
                    if let Some(earliest) = pending_items
                        .iter()
                        .map(|it| it.next_retry_at)
                        .min_by(|a, b| a.partial_cmp(b).unwrap())
                    {
                        if earliest > now {
                            let diff = (earliest - now).max(0.05).min(2.0);
                            timeout = Duration::from_secs_f64(diff);
                        } else {
                            timeout = Duration::from_millis(50);
                        }
                    }

                    match rx.recv_timeout(timeout) {
                        Ok(notice) => {
                            let snapshot =
                                worker_cfg.lock().unwrap_or_else(|e| e.into_inner()).clone();
                            if !snapshot.any_channel() {
                                eprintln!("[notify] no channels configured; alarm kept local");
                                let (lock, cv) = &*worker_pending;
                                let mut n = lock.lock().unwrap_or_else(|e| e.into_inner());
                                *n = n.saturating_sub(1);
                                cv.notify_all();
                            } else {
                                let is_dupe = match &notice {
                                    Notice::Fall { room, event_id, .. } => {
                                        if event_id == "evt_test" {
                                            false
                                        } else {
                                            let mut d = dedupe
                                                .lock()
                                                .unwrap_or_else(|e| e.into_inner());
                                            if d.1 == *room && (now - d.0) < 30.0 {
                                                true
                                            } else {
                                                d.0 = now;
                                                d.1 = room.clone();
                                                false
                                            }
                                        }
                                    }
                                    _ => false,
                                };

                                if is_dupe {
                                    eprintln!(
                                        "[notify] suppressed duplicate fall alert within 30s"
                                    );
                                    let (lock, cv) = &*worker_pending;
                                    let mut n = lock.lock().unwrap_or_else(|e| e.into_inner());
                                    *n = n.saturating_sub(1);
                                    cv.notify_all();
                                } else {
                                    let configured_channels = snapshot.configured_channel_names();
                                    let timeout_dur =
                                        Duration::from_secs(snapshot.timeout_secs.max(1));
                                    let mut failed_channels = Vec::new();

                                    for ch in configured_channels {
                                        let ok =
                                            deliver_channel(&snapshot, ch, &notice, timeout_dur);
                                        report(ch, ok);
                                        if !ok {
                                            failed_channels.push(ch.to_string());
                                        }
                                    }

                                    if failed_channels.is_empty() {
                                        let (lock, cv) = &*worker_pending;
                                        let mut n = lock.lock().unwrap_or_else(|e| e.into_inner());
                                        *n = n.saturating_sub(1);
                                        cv.notify_all();
                                    } else {
                                        let id = match &notice {
                                            Notice::Fall { event_id, .. }
                                                if !event_id.is_empty() =>
                                            {
                                                event_id.clone()
                                            }
                                            _ => format!("notif_{:.0}", now_epoch_secs()),
                                        };
                                        let backoff = calculate_backoff(1);
                                        let item = PendingNotification {
                                            id,
                                            notice,
                                            attempts: 1,
                                            created_at: now,
                                            next_retry_at: now + backoff.as_secs_f64(),
                                            failed_channels,
                                        };
                                        pending_items.push(item);
                                        save_pending_file(&worker_dir, &pending_items);
                                    }
                                }
                            }
                        }
                        Err(mpsc::RecvTimeoutError::Timeout) => {}
                        Err(mpsc::RecvTimeoutError::Disconnected) => {
                            break;
                        }
                    }

                    if !pending_items.is_empty() {
                        let now = now_epoch_secs();
                        let snapshot =
                            worker_cfg.lock().unwrap_or_else(|e| e.into_inner()).clone();
                        let timeout_dur = Duration::from_secs(snapshot.timeout_secs.max(1));
                        let mut modified = false;
                        let mut remaining_items = Vec::new();

                        for mut item in pending_items.drain(..) {
                            if now >= item.next_retry_at {
                                item.failed_channels.retain(|ch| {
                                    let ok =
                                        deliver_channel(&snapshot, ch, &item.notice, timeout_dur);
                                    report(ch, ok);
                                    !ok
                                });

                                if item.failed_channels.is_empty() {
                                    eprintln!(
                                        "[notify] item {} delivered on attempt {}",
                                        item.id, item.attempts
                                    );
                                    modified = true;
                                    let (lock, cv) = &*worker_pending;
                                    let mut n = lock.lock().unwrap_or_else(|e| e.into_inner());
                                    *n = n.saturating_sub(1);
                                    cv.notify_all();
                                } else {
                                    item.attempts += 1;
                                    if item.attempts >= MAX_RETRIES {
                                        eprintln!(
                                            "[notify] CRITICAL: item {} failed after {} attempts; dropping to dead letter queue",
                                            item.id, item.attempts
                                        );
                                        append_dead_letter(
                                            &worker_dir,
                                            &item,
                                            "max_retries_exceeded",
                                        );
                                        modified = true;
                                        let (lock, cv) = &*worker_pending;
                                        let mut n = lock.lock().unwrap_or_else(|e| e.into_inner());
                                        *n = n.saturating_sub(1);
                                        cv.notify_all();
                                    } else {
                                        let backoff = calculate_backoff(item.attempts);
                                        item.next_retry_at = now + backoff.as_secs_f64();
                                        modified = true;
                                        remaining_items.push(item);
                                    }
                                }
                            } else {
                                remaining_items.push(item);
                            }
                        }

                        pending_items = remaining_items;
                        if modified {
                            save_pending_file(&worker_dir, &pending_items);
                        }
                    }
                }
            })
            .ok();
        Notifier {
            tx,
            cfg,
            dir,
            pending,
        }
    }

    pub fn config(&self) -> NotifyConfig {
        self.cfg.lock().unwrap_or_else(|e| e.into_inner()).clone()
    }

    pub fn update<F: FnOnce(&mut NotifyConfig)>(&self, f: F) {
        let snapshot = {
            let mut c = self.cfg.lock().unwrap_or_else(|e| e.into_inner());
            f(&mut c);
            c.clone()
        };
        snapshot.save(&self.dir);
    }

    pub fn send(&self, n: Notice) {
        let (lock, _) = &*self.pending;
        *lock.lock().unwrap_or_else(|e| e.into_inner()) += 1;
        if self.tx.send(n).is_err() {
            let mut n = lock.lock().unwrap_or_else(|e| e.into_inner());
            *n = n.saturating_sub(1);
        }
    }

    /// Block until the worker has drained every queued notice (bounded).
    pub fn flush(&self, timeout: Duration) -> bool {
        let deadline = Instant::now() + timeout;
        let (lock, cv) = &*self.pending;
        let mut n = lock.lock().unwrap_or_else(|e| e.into_inner());
        while *n > 0 {
            let left = deadline.saturating_duration_since(Instant::now());
            if left.is_zero() {
                return false;
            }
            let (guard, wait) = cv.wait_timeout(n, left).unwrap_or_else(|e| e.into_inner());
            n = guard;
            if wait.timed_out() && *n > 0 {
                return false;
            }
        }
        true
    }

    pub fn configured_channels(&self) -> usize {
        self.config().configured_channel_names().len()
    }
}

fn deliver_channel(cfg: &NotifyConfig, channel: &str, n: &Notice, timeout: Duration) -> bool {
    let now = timefmt::now();
    match (n, channel) {
        (
            Notice::Fall {
                room,
                confidence,
                details,
                bpm,
                event_id,
                ..
            },
            CHAN_PUSHPLUS,
        ) => {
            let title = "🚨【紧急预警】SafeHalo 检测到疑似跌倒！";
            let content = format!(
                "### 紧急安全事件通知\n\n- **监测位置**: {}\n- **发生时间**: {}\n- **预警级别**: 红色紧急（检测到剧烈冲击并持续倒地静止）\n- **置信度**: {:.0}%\n- **呼吸估计**: {:.0} bpm\n- **事件编号**: {}\n- **附带信息**: {}\n\n> 请立即通过家中监控或电话确认老人安全！如无法取得联系，请尽快前往或联系邻居协助。",
                room,
                now.date_time,
                confidence * 100.0,
                bpm,
                event_id,
                details
            );
            let body = format!(
                "{{\"token\":\"{}\",\"title\":\"{}\",\"content\":\"{}\",\"template\":\"markdown\"}}",
                json::escape(&cfg.pushplus_token),
                json::escape(title),
                json::escape(&content)
            );
            post_json("http://www.pushplus.plus/send", &body, timeout)
        }
        (
            Notice::Fall {
                room,
                confidence,
                details,
                bpm,
                event_id,
                ..
            },
            CHAN_SERVERCHAN,
        ) => {
            let title = "🚨【紧急预警】SafeHalo 检测到疑似跌倒！";
            let content = format!(
                "### 紧急安全事件通知\n\n- **监测位置**: {}\n- **发生时间**: {}\n- **预警级别**: 红色紧急（检测到剧烈冲击并持续倒地静止）\n- **置信度**: {:.0}%\n- **呼吸估计**: {:.0} bpm\n- **事件编号**: {}\n- **附带信息**: {}\n\n> 请立即通过家中监控或电话确认老人安全！如无法取得联系，请尽快前往或联系邻居协助。",
                room,
                now.date_time,
                confidence * 100.0,
                bpm,
                event_id,
                details
            );
            let url = format!("https://sctapi.ftqq.com/{}.send", cfg.serverchan_key);
            let body = format!(
                "{{\"title\":\"{}\",\"desp\":\"{}\"}}",
                json::escape(title),
                json::escape(&content)
            );
            post_json(&url, &body, timeout)
        }
        (
            Notice::Fall {
                room,
                confidence,
                details,
                bpm,
                event_id,
                ..
            },
            CHAN_WEBHOOK,
        ) => {
            let title = "🚨【紧急预警】SafeHalo 检测到疑似跌倒！";
            let content = format!(
                "### 紧急安全事件通知\n\n- **监测位置**: {}\n- **发生时间**: {}\n- **预警级别**: 红色紧急（检测到剧烈冲击并持续倒地静止）\n- **置信度**: {:.0}%\n- **呼吸估计**: {:.0} bpm\n- **事件编号**: {}\n- **附带信息**: {}\n\n> 请立即通过家中监控或电话确认老人安全！如无法取得联系，请尽快前往或联系邻居协助。",
                room,
                now.date_time,
                confidence * 100.0,
                bpm,
                event_id,
                details
            );
            let body = format!(
                "{{\"msgtype\":\"markdown\",\"markdown\":{{\"content\":\"{}\"}}}}",
                json::escape(&format!("## {}\n{}", title, content))
            );
            post_json(&cfg.webhook_url, &body, timeout)
        }
        (
            Notice::Fall {
                room,
                confidence,
                details,
                bpm,
                event_id,
                household,
            },
            CHAN_HA_WEBHOOK,
        ) => {
            let body = format!(
                "{{\"source\":\"SafeHalo\",\"event\":\"fall_alarm\",\"room\":\"{}\",\"confidence\":{:.2},\"confidence_str\":\"{:.0}%\",\"respiration_bpm\":{:.1},\"event_id\":\"{}\",\"household_id\":\"{}\",\"details\":\"{}\",\"timestamp\":\"{}\"}}",
                json::escape(room),
                confidence,
                confidence * 100.0,
                bpm,
                json::escape(event_id),
                json::escape(household),
                json::escape(details),
                json::escape(&now.iso)
            );
            post_json(&cfg.ha_webhook_url, &body, timeout)
        }
        (
            Notice::Fall {
                room,
                confidence,
                details,
                bpm,
                event_id,
                household,
            },
            CHAN_SMS_WEBHOOK,
        ) => {
            let title = "🚨【紧急预警】SafeHalo 检测到疑似跌倒！";
            let body = format!(
                "{{\"source\":\"SafeHalo\",\"event\":\"fall_alarm\",\"room\":\"{}\",\"title\":\"{}\",\"confidence\":{:.2},\"confidence_str\":\"{:.0}%\",\"respiration_bpm\":{:.1},\"event_id\":\"{}\",\"household_id\":\"{}\",\"details\":\"{}\",\"priority\":\"CRITICAL\",\"timestamp\":\"{}\"}}",
                json::escape(room),
                json::escape(title),
                confidence,
                confidence * 100.0,
                bpm,
                json::escape(event_id),
                json::escape(household),
                json::escape(details),
                json::escape(&now.iso)
            );
            post_json(&cfg.sms_webhook_url, &body, timeout)
        }
        (
            Notice::Fall {
                room,
                confidence,
                details,
                bpm,
                event_id,
                household,
            },
            CHAN_MINIPROGRAM,
        ) => {
            let body = format!(
                "{{\"source\":\"SafeHalo_Phase2\",\"event_id\":\"{}\",\"household_id\":\"{}\",\"room_name\":\"{}\",\"event_type\":\"FALL_ALARM\",\"confidence\":{:.2},\"respiration_bpm\":{:.1},\"details\":\"{}\",\"timestamp\":{},\"time_str\":\"{}\",\"miniprogram_page\":\"pages/alert/detail?event_id={}\",\"feedback_callback_path\":\"/api/incidents/feedback\"}}",
                json::escape(event_id),
                json::escape(household),
                json::escape(room),
                confidence,
                bpm,
                json::escape(details),
                now_epoch_secs(),
                json::escape(&now.date_time[..19]),
                json::escape(event_id)
            );
            let extra = if cfg.miniprogram_token.is_empty() {
                String::new()
            } else {
                format!(
                    "Authorization: Bearer {}\r\n",
                    json::escape(&cfg.miniprogram_token)
                )
            };
            post_json_with_auth(&cfg.miniprogram_url, &body, &extra, timeout)
        }
        (Notice::Generic { title, content, .. }, CHAN_PUSHPLUS) => {
            let body = format!(
                "{{\"token\":\"{}\",\"title\":\"{}\",\"content\":\"{}\",\"template\":\"markdown\"}}",
                json::escape(&cfg.pushplus_token),
                json::escape(title),
                json::escape(content)
            );
            post_json("http://www.pushplus.plus/send", &body, timeout)
        }
        (Notice::Generic { title, content, .. }, CHAN_SERVERCHAN) => {
            let url = format!("https://sctapi.ftqq.com/{}.send", cfg.serverchan_key);
            let body = format!(
                "{{\"title\":\"{}\",\"desp\":\"{}\"}}",
                json::escape(title),
                json::escape(content)
            );
            post_json(&url, &body, timeout)
        }
        (Notice::Generic { title, content, .. }, CHAN_WEBHOOK) => {
            let body = format!(
                "{{\"msgtype\":\"markdown\",\"markdown\":{{\"content\":\"{}\"}}}}",
                json::escape(&format!("## {}\n{}", title, content))
            );
            post_json(&cfg.webhook_url, &body, timeout)
        }
        (Notice::Generic { content, room, ha_event, .. }, CHAN_HA_WEBHOOK) => {
            let body = format!(
                "{{\"source\":\"SafeHalo\",\"event\":\"{}\",\"room\":\"{}\",\"confidence\":\"高\",\"details\":\"{}\",\"timestamp\":\"{}\"}}",
                json::escape(ha_event),
                json::escape(room),
                json::escape(content),
                json::escape(&timefmt::now().iso)
            );
            post_json(&cfg.ha_webhook_url, &body, timeout)
        }
        (
            Notice::Fall {
                room,
                confidence,
                details,
                bpm,
                event_id,
                ..
            },
            CHAN_BARK,
        ) => {
            let title = "🚨【紧急预警】SafeHalo 检测到疑似跌倒！";
            let body = format!(
                "监测位置: {}\n置信度: {:.0}%\n呼吸估计: {:.0} bpm\n事件编号: {}\n附带信息: {}\n请立即确认老人安全！",
                room,
                confidence * 100.0,
                bpm,
                event_id,
                details
            );
            let url = if cfg.bark_key.starts_with("http://") || cfg.bark_key.starts_with("https://") {
                cfg.bark_key.clone()
            } else {
                format!("https://api.day.app/{}", cfg.bark_key)
            };
            let json_body = format!(
                "{{\"title\":\"{}\",\"body\":\"{}\",\"level\":\"critical\",\"volume\":10,\"sound\":\"alarm\",\"group\":\"SafeHalo\"}}",
                json::escape(title),
                json::escape(&body)
            );
            post_json(&url, &json_body, timeout)
        }
        (Notice::Generic { title, content, .. }, CHAN_BARK) => {
            let url = if cfg.bark_key.starts_with("http://") || cfg.bark_key.starts_with("https://") {
                cfg.bark_key.clone()
            } else {
                format!("https://api.day.app/{}", cfg.bark_key)
            };
            let json_body = format!(
                "{{\"title\":\"{}\",\"body\":\"{}\",\"level\":\"timeSensitive\",\"sound\":\"anticipate\",\"group\":\"SafeHalo\"}}",
                json::escape(title),
                json::escape(content)
            );
            post_json(&url, &json_body, timeout)
        }
        _ => false,
    }
}

fn now_epoch_secs() -> f64 {
    timefmt::now_epoch()
}

fn report(channel: &str, ok: bool) {
    if ok {
        eprintln!("[notify] {} delivered", channel);
    } else {
        eprintln!("[notify] {} failed", channel);
    }
}

fn post_json(url: &str, body: &str, timeout: Duration) -> bool {
    post_json_with_auth(url, body, "", timeout)
}

fn post_json_with_auth(url: &str, body: &str, extra_header: &str, timeout: Duration) -> bool {
    if url.starts_with("http://") {
        post_http(url, body, extra_header, timeout)
    } else if url.starts_with("https://") {
        post_https_helper(url, body, timeout)
    } else {
        eprintln!("[notify] unsupported url scheme: {}", url);
        false
    }
}

fn post_http(url: &str, body: &str, extra_header: &str, timeout: Duration) -> bool {
    let rest = &url[7..];
    let (hostport, path) = match rest.find('/') {
        Some(i) => (&rest[..i], &rest[i..]),
        None => (rest, "/"),
    };
    let (host, port) = match hostport.rsplit_once(':') {
        Some((h, p)) => (h, p.parse::<u16>().unwrap_or(80)),
        None => (hostport, 80),
    };
    let addrs = match (host, port).to_socket_addrs() {
        Ok(a) => a,
        Err(e) => {
            eprintln!("[notify] resolve {} failed: {}", host, e);
            return false;
        }
    };
    for addr in addrs {
        let mut stream = match TcpStream::connect_timeout(&addr, timeout) {
            Ok(s) => s,
            Err(_) => continue,
        };
        let _ = stream.set_read_timeout(Some(timeout));
        let _ = stream.set_write_timeout(Some(timeout));
        let req = format!(
            "POST {} HTTP/1.1\r\nHost: {}\r\nContent-Type: application/json; charset=utf-8\r\n{}Content-Length: {}\r\nConnection: close\r\n\r\n{}",
            path,
            hostport,
            extra_header,
            body.as_bytes().len(),
            body
        );
        if stream.write_all(req.as_bytes()).is_err() {
            continue;
        }
        let mut resp = Vec::new();
        let mut buf = [0u8; 4096];
        loop {
            match stream.read(&mut buf) {
                Ok(0) => break,
                Ok(n) => {
                    resp.extend_from_slice(&buf[..n]);
                    if resp.len() > 65536 {
                        break;
                    }
                }
                Err(_) => break,
            }
        }
        let first = String::from_utf8_lossy(&resp);
        let code = first
            .lines()
            .next()
            .and_then(|l| l.split_whitespace().nth(1))
            .and_then(|c| c.parse::<u16>().ok())
            .unwrap_or(0);
        return (200..300).contains(&code);
    }
    false
}

fn post_https_helper(url: &str, body: &str, timeout: Duration) -> bool {
    let secs = timeout.as_secs().max(1).to_string();
    if let Ok(mut child) = Command::new("curl")
        .args([
            "-sS",
            "-m",
            &secs,
            "-o",
            "/dev/null",
            "-w",
            "%{http_code}",
            "-X",
            "POST",
            "-H",
            "Content-Type: application/json; charset=utf-8",
            "--data-binary",
            "@-",
            url,
        ])
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .spawn()
    {
        if let Some(mut stdin) = child.stdin.take() {
            let _ = stdin.write_all(body.as_bytes());
        }
        if let Ok(out) = child.wait_with_output() {
            let code = String::from_utf8_lossy(&out.stdout).trim().to_string();
            if code.starts_with('2') {
                return true;
            }
            if !code.is_empty() && code != "000" {
                return false;
            }
        }
    }
    if Command::new("wget")
        .args([
            "-q",
            "-T",
            &secs,
            "-O",
            "/dev/null",
            "--header=Content-Type: application/json; charset=utf-8",
            &format!("--post-data={}", body),
            url,
        ])
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status()
        .map(|s| s.success())
        .unwrap_or(false)
    {
        return true;
    }
    Command::new("uclient-fetch")
        .args([
            "-q",
            "-T",
            &secs,
            "-O",
            "/dev/null",
            "--header=Content-Type: application/json; charset=utf-8",
            "--post-data",
            body,
            url,
        ])
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status()
        .map(|s| s.success())
        .unwrap_or(false)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn config_roundtrip() {
        let dir = std::env::temp_dir().join(format!("safehalo_notify_{}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        let mut c = NotifyConfig::default();
        c.pushplus_token = "abc".into();
        c.bark_key = "device_key_123".into();
        c.timeout_secs = 7;
        assert!(c.configured_channel_names().contains(&CHAN_BARK));
        c.save(dir.to_str().unwrap());
        let loaded = NotifyConfig::load(dir.to_str().unwrap());
        assert_eq!(loaded.pushplus_token, "abc");
        assert_eq!(loaded.bark_key, "device_key_123");
        assert_eq!(loaded.timeout_secs, 7);
        assert!(loaded.configured_channel_names().contains(&CHAN_BARK));
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn test_notice_json_roundtrip() {
        let fall = Notice::Fall {
            room: "主卧".into(),
            confidence: 0.94,
            details: "冲击持续静止".into(),
            bpm: 16.5,
            event_id: "evt_123".into(),
            household: "H02".into(),
        };
        let j = fall.to_json();
        let parsed = json::parse(&j).expect("json parse");
        let restored = Notice::from_json(&parsed).expect("restore notice");
        assert_eq!(fall, restored);

        let generic = Notice::Generic {
            title: "标题".into(),
            content: "内容".into(),
            room: "客厅".into(),
            ha_event: "alert".into(),
        };
        let j2 = generic.to_json();
        let parsed2 = json::parse(&j2).expect("json parse");
        let restored2 = Notice::from_json(&parsed2).expect("restore generic");
        assert_eq!(generic, restored2);
    }

    #[test]
    fn test_backoff_calculation() {
        assert_eq!(calculate_backoff(0), Duration::from_millis(0));
        assert_eq!(calculate_backoff(1), Duration::from_secs(2));
        assert_eq!(calculate_backoff(2), Duration::from_secs(5));
        assert_eq!(calculate_backoff(3), Duration::from_secs(15));
        assert_eq!(calculate_backoff(4), Duration::from_secs(30));
        assert_eq!(calculate_backoff(5), Duration::from_secs(60));
    }

    #[test]
    fn test_pending_notification_persistence() {
        let dir = std::env::temp_dir().join(format!("safehalo_notify_pending_{}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        let _ = fs::create_dir_all(&dir);

        let notice = Notice::Fall {
            room: "卫生间".into(),
            confidence: 0.92,
            details: "滑倒".into(),
            bpm: 17.0,
            event_id: "evt_456".into(),
            household: "H01".into(),
        };
        let item = PendingNotification {
            id: "evt_456".into(),
            notice,
            attempts: 1,
            created_at: 1000.0,
            next_retry_at: 1002.0,
            failed_channels: vec![CHAN_PUSHPLUS.into(), CHAN_HA_WEBHOOK.into()],
        };

        save_pending_file(dir.to_str().unwrap(), &[item.clone()]);
        let loaded = load_pending_file(dir.to_str().unwrap());
        assert_eq!(loaded.len(), 1);
        assert_eq!(loaded[0].id, "evt_456");
        assert_eq!(loaded[0].failed_channels, vec!["pushplus", "ha_webhook"]);

        // Test dead letters
        append_dead_letter(dir.to_str().unwrap(), &loaded[0], "test_dropped");
        let dead_file = dir.join("dead_letters.jsonl");
        assert!(dead_file.exists());
        let content = fs::read_to_string(dead_file).unwrap();
        assert!(content.contains("evt_456"));
        assert!(content.contains("test_dropped"));

        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn plain_http_post() {
        use std::net::TcpListener;
        let listener = match TcpListener::bind("127.0.0.1:0") {
            Ok(l) => l,
            Err(_) => {
                eprintln!("skipping plain_http_post in sandboxed environment without loopback");
                return;
            }
        };
        let addr = listener.local_addr().unwrap();
        // Check if loopback connection is permitted in this environment before spawning server
        let can_connect = TcpStream::connect_timeout(&addr, Duration::from_millis(50)).is_ok();
        if !can_connect {
            eprintln!("skipping plain_http_post in sandbox (loopback connect not permitted)");
            return;
        }
        let handle = std::thread::spawn(move || {
            if let Ok((mut s, _)) = listener.accept() {
                let mut buf = [0u8; 4096];
                let _ = s.read(&mut buf);
                let _ = s.write_all(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n");
            }
        });
        assert!(post_json(
            &format!("http://{}/hook", addr),
            "{\"a\":1}",
            Duration::from_secs(2)
        ));
        handle.join().ok();
    }
}
