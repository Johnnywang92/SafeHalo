// Alert fan-out: incident push to PushPlus / ServerChan / generic webhook /
// Home Assistant / SMS gateway / mini-program backend.
//
// Plain HTTP/1.1 is implemented in-tree (zero dependencies). HTTPS endpoints
// are delegated to curl / wget / uclient-fetch when present, because pulling a
// TLS stack in would break the mipsel musl static build.

use std::fs;
use std::io::{Read, Write};
use std::net::{TcpStream, ToSocketAddrs};
use std::path::PathBuf;
use std::process::{Command, Stdio};
use std::sync::mpsc::{self, Sender};
use std::sync::{Arc, Condvar, Mutex};
use std::time::{Duration, Instant};

use crate::json;
use crate::timefmt;

#[derive(Clone, Default)]
pub struct NotifyConfig {
    pub pushplus_token: String,
    pub serverchan_key: String,
    pub webhook_url: String,
    pub ha_webhook_url: String,
    pub sms_webhook_url: String,
    pub miniprogram_url: String,
    pub miniprogram_token: String,
    pub timeout_secs: u64,
}

impl NotifyConfig {
    pub fn any_channel(&self) -> bool {
        !(self.pushplus_token.is_empty()
            && self.serverchan_key.is_empty()
            && self.webhook_url.is_empty()
            && self.ha_webhook_url.is_empty()
            && self.sms_webhook_url.is_empty()
            && self.miniprogram_url.is_empty())
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
            "{{\n  \"pushplus_token\": \"{}\",\n  \"serverchan_key\": \"{}\",\n  \"webhook_url\": \"{}\",\n  \"ha_webhook_url\": \"{}\",\n  \"sms_webhook_url\": \"{}\",\n  \"miniprogram_url\": \"{}\",\n  \"miniprogram_token\": \"{}\",\n  \"timeout_secs\": {}\n}}\n",
            json::escape(&self.pushplus_token),
            json::escape(&self.serverchan_key),
            json::escape(&self.webhook_url),
            json::escape(&self.ha_webhook_url),
            json::escape(&self.sms_webhook_url),
            json::escape(&self.miniprogram_url),
            json::escape(&self.miniprogram_token),
            self.timeout_secs
        );
        let _ = fs::write(PathBuf::from(dir).join("notifier_config.json"), text);
    }
}

pub enum Notice {
    Fall {
        room: String,
        confidence: String,
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
        let worker_cfg = Arc::clone(&cfg);
        let worker_pending = Arc::clone(&pending);
        std::thread::Builder::new()
            .name("notify".into())
            .stack_size(256 * 1024)
            .spawn(move || {
                for notice in rx {
                    let snapshot = worker_cfg.lock().unwrap_or_else(|e| e.into_inner()).clone();
                    if snapshot.any_channel() {
                        dispatch(&snapshot, &notice);
                    } else {
                        eprintln!("[notify] no channels configured; alarm kept local");
                    }
                    let (lock, cv) = &*worker_pending;
                    let mut n = lock.lock().unwrap_or_else(|e| e.into_inner());
                    *n = n.saturating_sub(1);
                    cv.notify_all();
                }
            })
            .ok();
        Notifier { tx, cfg, dir, pending }
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
        let c = self.config();
        [
            !c.pushplus_token.is_empty(),
            !c.serverchan_key.is_empty(),
            !c.webhook_url.is_empty(),
            !c.ha_webhook_url.is_empty(),
            !c.sms_webhook_url.is_empty(),
            !c.miniprogram_url.is_empty(),
        ]
        .iter()
        .filter(|x| **x)
        .count()
    }
}

fn dispatch(cfg: &NotifyConfig, n: &Notice) {
    let timeout = Duration::from_secs(cfg.timeout_secs.max(1));
    match n {
        Notice::Fall { room, confidence, details, bpm, event_id, household } => {
            let now = timefmt::now();
            let title = "🚨【紧急预警】SafeHalo 检测到疑似跌倒！";
            let content = format!(
                "### 紧急安全事件通知\n\n- **监测位置**: {}\n- **发生时间**: {}\n- **预警级别**: 红色紧急（检测到剧烈冲击并持续倒地静止）\n- **置信度**: {}\n- **呼吸估计**: {:.0} bpm\n- **附带信息**: {}\n\n> 请立即通过家中监控或电话确认老人安全！如无法取得联系，请尽快前往或联系邻居协助。",
                room, now.date_time, confidence, bpm, details
            );
            if !cfg.pushplus_token.is_empty() {
                let body = format!(
                    "{{\"token\":\"{}\",\"title\":\"{}\",\"content\":\"{}\",\"template\":\"markdown\"}}",
                    json::escape(&cfg.pushplus_token),
                    json::escape(title),
                    json::escape(&content)
                );
                report("pushplus", post_json("http://www.pushplus.plus/send", &body, timeout));
            }
            if !cfg.serverchan_key.is_empty() {
                let url = format!("https://sctapi.ftqq.com/{}.send", cfg.serverchan_key);
                let body = format!(
                    "{{\"title\":\"{}\",\"desp\":\"{}\"}}",
                    json::escape(title),
                    json::escape(&content)
                );
                report("serverchan", post_json(&url, &body, timeout));
            }
            if !cfg.webhook_url.is_empty() {
                let body = format!(
                    "{{\"msgtype\":\"markdown\",\"markdown\":{{\"content\":\"{}\"}}}}",
                    json::escape(&format!("## {}\n{}", title, content))
                );
                report("webhook", post_json(&cfg.webhook_url, &body, timeout));
            }
            if !cfg.ha_webhook_url.is_empty() {
                let body = format!(
                    "{{\"source\":\"SafeHalo\",\"event\":\"fall_alarm\",\"room\":\"{}\",\"confidence\":\"{}\",\"details\":\"{}\",\"timestamp\":\"{}\"}}",
                    json::escape(room),
                    json::escape(confidence),
                    json::escape(details),
                    json::escape(&now.iso)
                );
                report("ha_webhook", post_json(&cfg.ha_webhook_url, &body, timeout));
            }
            if !cfg.sms_webhook_url.is_empty() {
                let body = format!(
                    "{{\"source\":\"SafeHalo\",\"event\":\"fall_alarm\",\"room\":\"{}\",\"title\":\"{}\",\"details\":\"{}\",\"priority\":\"CRITICAL\",\"timestamp\":\"{}\"}}",
                    json::escape(room),
                    json::escape(title),
                    json::escape(details),
                    json::escape(&now.iso)
                );
                report("sms_webhook", post_json(&cfg.sms_webhook_url, &body, timeout));
            }
            if !cfg.miniprogram_url.is_empty() {
                let body = format!(
                    "{{\"source\":\"SafeHalo_Phase2\",\"event_id\":\"{}\",\"household_id\":\"{}\",\"room_name\":\"{}\",\"event_type\":\"FALL_ALARM\",\"confidence\":0.96,\"respiration_bpm\":{:.1},\"details\":\"{}\",\"timestamp\":{},\"time_str\":\"{}\",\"miniprogram_page\":\"pages/alert/detail?event_id={}\",\"feedback_callback_path\":\"/api/incidents/feedback\"}}",
                    json::escape(event_id),
                    json::escape(household),
                    json::escape(room),
                    bpm,
                    json::escape(details),
                    now_epoch_secs(),
                    json::escape(&now.date_time[..19]),
                    json::escape(event_id)
                );
                let extra = if cfg.miniprogram_token.is_empty() {
                    String::new()
                } else {
                    format!("Authorization: Bearer {}\r\n", json::escape(&cfg.miniprogram_token))
                };
                report("miniprogram", post_json_with_auth(&cfg.miniprogram_url, &body, &extra, timeout));
            }
        }
        Notice::Generic { title, content, room, ha_event } => {
            if !cfg.pushplus_token.is_empty() {
                let body = format!(
                    "{{\"token\":\"{}\",\"title\":\"{}\",\"content\":\"{}\",\"template\":\"markdown\"}}",
                    json::escape(&cfg.pushplus_token),
                    json::escape(title),
                    json::escape(content)
                );
                report("pushplus", post_json("http://www.pushplus.plus/send", &body, timeout));
            }
            if !cfg.serverchan_key.is_empty() {
                let url = format!("https://sctapi.ftqq.com/{}.send", cfg.serverchan_key);
                let body = format!(
                    "{{\"title\":\"{}\",\"desp\":\"{}\"}}",
                    json::escape(title),
                    json::escape(content)
                );
                report("serverchan", post_json(&url, &body, timeout));
            }
            if !cfg.webhook_url.is_empty() {
                let body = format!(
                    "{{\"msgtype\":\"markdown\",\"markdown\":{{\"content\":\"{}\"}}}}",
                    json::escape(&format!("## {}\n{}", title, content))
                );
                report("webhook", post_json(&cfg.webhook_url, &body, timeout));
            }
            if !cfg.ha_webhook_url.is_empty() {
                let body = format!(
                    "{{\"source\":\"SafeHalo\",\"event\":\"{}\",\"room\":\"{}\",\"confidence\":\"高\",\"details\":\"{}\",\"timestamp\":\"{}\"}}",
                    json::escape(ha_event),
                    json::escape(room),
                    json::escape(content),
                    json::escape(&timefmt::now().iso)
                );
                report("ha_webhook", post_json(&cfg.ha_webhook_url, &body, timeout));
            }
        }
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
    // curl can read the payload from stdin, avoiding argv size limits.
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
    // BusyBox wget / GNU wget.
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
    // OpenWrt uclient-fetch.
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
        c.timeout_secs = 7;
        c.save(dir.to_str().unwrap());
        let loaded = NotifyConfig::load(dir.to_str().unwrap());
        assert_eq!(loaded.pushplus_token, "abc");
        assert_eq!(loaded.timeout_secs, 7);
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn plain_http_post() {
        use std::net::TcpListener;
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let addr = listener.local_addr().unwrap();
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
