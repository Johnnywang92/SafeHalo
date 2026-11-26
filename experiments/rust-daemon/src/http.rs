// Standalone HTTP service: static console + JSON API + SSE event stream.
// One thread per connection so a long-lived SSE client cannot block the rest.

use std::collections::HashMap;
use std::fs;
use std::io::{Read, Write};
use std::net::{TcpListener, TcpStream};
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

const MAX_ACTIVE_HTTP_CONNS: usize = 32;
const MAX_ACTIVE_SSE_STREAMS: usize = 8;
static ACTIVE_HTTP_CONNS: AtomicUsize = AtomicUsize::new(0);
static ACTIVE_SSE_STREAMS: AtomicUsize = AtomicUsize::new(0);

struct ConnGuard;
impl Drop for ConnGuard {
    fn drop(&mut self) {
        ACTIVE_HTTP_CONNS.fetch_sub(1, Ordering::SeqCst);
    }
}

struct SseGuard;
impl Drop for SseGuard {
    fn drop(&mut self) {
        ACTIVE_SSE_STREAMS.fetch_sub(1, Ordering::SeqCst);
    }
}

use crate::auth::{AuthManager, Session};
use crate::json;
use crate::notify::{Notice, Notifier};
use crate::state::{self, Bus};
use crate::timefmt;

const INDEX_HTML: &str = include_str!("../../../src/safehalo/web/index.html");

pub fn default_html() -> &'static str {
    INDEX_HTML
}

#[derive(Clone)]
pub struct HttpConfig {
    pub token: String,
    pub html: String,
    pub cookie_secure: bool,
}

struct Req {
    method: String,
    path: String,
    query: String,
    headers: HashMap<String, String>,
    body: Vec<u8>,
}

enum Ctx {
    Machine,
    User(Session),
}

impl Ctx {
    fn role(&self) -> &str {
        match self {
            Ctx::Machine => "machine",
            Ctx::User(s) => &s.role,
        }
    }
    fn username(&self) -> &str {
        match self {
            Ctx::Machine => "api-token",
            Ctx::User(s) => &s.username,
        }
    }
}

struct RateLimiter {
    hits: Mutex<HashMap<String, (f64, u32)>>,
}

impl RateLimiter {
    fn new() -> Self {
        RateLimiter { hits: Mutex::new(HashMap::new()) }
    }

    fn allow(&self, key: &str, limit: u32, window_secs: f64) -> bool {
        let now = timefmt::now_epoch();
        let mut h = self.hits.lock().unwrap_or_else(|e| e.into_inner());
        if h.len() > 4096 {
            h.retain(|_, (t, _)| now - *t < 60.0);
            // Still oversized means a key-flood attack; drop state rather than grow unbounded.
            if h.len() > 8192 {
                h.clear();
            }
        }
        let e = h.entry(key.to_string()).or_insert((now, 0));
        if now - e.0 >= window_secs {
            *e = (now, 0);
        }
        e.1 += 1;
        e.1 <= limit
    }

    fn reset(&self, key: &str) {
        let mut h = self.hits.lock().unwrap_or_else(|e| e.into_inner());
        h.remove(key);
    }
}

#[allow(dead_code)]
fn ctype(path: &str) -> &'static str {
    if path.ends_with(".html") || path == "/" {
        "text/html; charset=utf-8"
    } else if path.ends_with(".js") {
        "application/javascript; charset=utf-8"
    } else if path.ends_with(".css") {
        "text/css; charset=utf-8"
    } else {
        "application/json; charset=utf-8"
    }
}

fn percent_decode(s: &str) -> String {
    let b = s.as_bytes();
    let mut out = Vec::with_capacity(b.len());
    let mut i = 0;
    while i < b.len() {
        if b[i] == b'%' && i + 2 < b.len() {
            let hi = (b[i + 1] as char).to_digit(16);
            let lo = (b[i + 2] as char).to_digit(16);
            if let (Some(h), Some(l)) = (hi, lo) {
                out.push(((h << 4) | l) as u8);
                i += 3;
                continue;
            }
        }
        if b[i] == b'+' {
            out.push(b' ');
        } else {
            out.push(b[i]);
        }
        i += 1;
    }
    String::from_utf8_lossy(&out).to_string()
}

fn parse_query(q: &str) -> HashMap<String, String> {
    let mut m = HashMap::new();
    for kv in q.split('&') {
        if kv.is_empty() {
            continue;
        }
        let mut it = kv.splitn(2, '=');
        let k = percent_decode(it.next().unwrap_or(""));
        let v = percent_decode(it.next().unwrap_or(""));
        m.insert(k, v);
    }
    m
}

fn read_request(st: &mut TcpStream) -> Option<Req> {
    st.set_read_timeout(Some(Duration::from_secs(5))).ok();
    let mut data: Vec<u8> = Vec::new();
    let mut buf = [0u8; 8192];
    let header_end;
    loop {
        match st.read(&mut buf) {
            Ok(0) => return None,
            Ok(n) => {
                data.extend_from_slice(&buf[..n]);
                if let Some(p) = data.windows(4).position(|w| w == b"\r\n\r\n") {
                    header_end = p;
                    break;
                }
                if data.len() > 65536 {
                    return None;
                }
            }
            Err(_) => return None,
        }
    }
    let head = String::from_utf8_lossy(&data[..header_end]).to_string();
    let mut lines = head.lines();
    let req_line = lines.next()?;
    let mut parts = req_line.split_whitespace();
    let method = parts.next()?.to_string();
    let target = parts.next()?.to_string();
    let (path, query) = match target.split_once('?') {
        Some((p, q)) => (p.to_string(), q.to_string()),
        None => (target, String::new()),
    };
    let mut headers = HashMap::new();
    for line in lines {
        if let Some((k, v)) = line.split_once(':') {
            headers.insert(k.trim().to_ascii_lowercase(), v.trim().to_string());
        }
    }
    let len: usize = headers
        .get("content-length")
        .and_then(|v| v.parse().ok())
        .unwrap_or(0)
        .min(1_048_576);
    let mut body = data[header_end + 4..].to_vec();
    while body.len() < len {
        match st.read(&mut buf) {
            Ok(0) => break,
            Ok(n) => body.extend_from_slice(&buf[..n]),
            Err(_) => break,
        }
    }
    body.truncate(len);
    Some(Req {
        method,
        path,
        query,
        headers,
        body,
    })
}

fn header<'a>(req: &'a Req, name: &str) -> &'a str {
    req.headers.get(name).map(|s| s.as_str()).unwrap_or("")
}

fn cookie_token(req: &Req) -> Option<String> {
    let c = header(req, "cookie");
    for part in c.split(';') {
        let part = part.trim();
        if let Some(v) = part.strip_prefix("safehalo_session=") {
            return Some(v.trim().to_string());
        }
    }
    None
}

fn bearer(req: &Req) -> Option<String> {
    let a = header(req, "authorization");
    a.strip_prefix("Bearer ").map(|s| s.trim().to_string())
}

fn machine_ok(req: &Req, q: &HashMap<String, String>, token: &str) -> bool {
    if token.is_empty() {
        return false;
    }
    bearer(req).as_deref() == Some(token) || q.get("token").map(|t| t == token).unwrap_or(false)
}

fn auth_ctx(req: &Req, q: &HashMap<String, String>, auth: &AuthManager, token: &str) -> Option<Ctx> {
    if machine_ok(req, q, token) {
        return Some(Ctx::Machine);
    }
    if let Some(t) = bearer(req) {
        if let Some(s) = auth.validate(&t) {
            return Some(Ctx::User(s));
        }
    }
    if let Some(t) = cookie_token(req) {
        if let Some(s) = auth.validate(&t) {
            return Some(Ctx::User(s));
        }
    }
    None
}

fn must_change_exempt(method: &str, path: &str) -> bool {
    if path == "/api/auth/update_password" {
        return true;
    }
    if method == "GET" {
        matches!(
            path,
            "/" | "/index.html" | "/api/auth/current" | "/api/ha/status" | "/api/routine"
                | "/api/neighbor_spectrum" | "/api/placement" | "/api/multilink"
                | "/api/phase2/pilot" | "/api/phase2/pilot/report" | "/api/incidents"
                | "/api/devices" | "/api/health" | "/api/sensors/status"
        )
    } else {
        matches!(path, "/api/auth/login" | "/api/auth/logout" | "/api/challenge/acknowledge" | "/api/sensors/event")
    }
}

fn origin_ok(req: &Req) -> bool {
    let host = header(req, "host").to_ascii_lowercase();
    for name in ["origin", "referer"] {
        let v = header(req, name);
        if v.is_empty() {
            continue;
        }
        let rest = v.split_once("://").map(|(_, r)| r).unwrap_or(v);
        let authority = rest.split(['/', '?', '#']).next().unwrap_or("");
        return !host.is_empty() && authority.to_ascii_lowercase() == host;
    }
    true
}

fn send_raw(st: &mut TcpStream, status: u16, reason: &str, ctype: &str, body: &[u8], extra: &[String]) {
    let mut head = format!(
        "HTTP/1.1 {} {}\r\nContent-Type: {}\r\nContent-Length: {}\r\nConnection: close\r\n",
        status,
        reason,
        ctype,
        body.len()
    );
    for h in extra {
        head.push_str(h);
        head.push_str("\r\n");
    }
    head.push_str("\r\n");
    let _ = st.write_all(head.as_bytes());
    let _ = st.write_all(body);
}

fn send_json(st: &mut TcpStream, status: u16, body: &str) {
    let reason = match status {
        200 => "OK",
        400 => "Bad Request",
        401 => "Unauthorized",
        403 => "Forbidden",
        404 => "Not Found",
        429 => "Too Many Requests",
        500 => "Internal Server Error",
        _ => "OK",
    };
    send_raw(st, status, reason, "application/json; charset=utf-8", body.as_bytes(), &[]);
}

fn send_text(st: &mut TcpStream, status: u16, ctype: &str, body: &str) {
    let reason = match status {
        200 => "OK",
        400 => "Bad Request",
        401 => "Unauthorized",
        403 => "Forbidden",
        404 => "Not Found",
        _ => "OK",
    };
    send_raw(st, status, reason, ctype, body.as_bytes(), &[]);
}

fn json_body(req: &Req) -> json::Json {
    json::parse(&String::from_utf8_lossy(&req.body))
        .unwrap_or(json::Json::Obj(Vec::new()))
}

fn jstr(v: &json::Json, k: &str) -> String {
    v.get(k).and_then(|x| x.as_str()).unwrap_or("").to_string()
}
fn jbool(v: &json::Json, k: &str, d: bool) -> bool {
    v.get(k).and_then(|x| x.as_bool()).unwrap_or(d)
}

fn ok_msg(msg: &str) -> String {
    format!("{{\"success\":true,\"message\":\"{}\"}}", json::escape(msg))
}

pub fn serve(addr: String, bus: Arc<Bus>, auth: Arc<AuthManager>, notifier: Notifier, cfg: HttpConfig) {
    let listener = match TcpListener::bind(&addr) {
        Ok(l) => l,
        Err(e) => {
            eprintln!("[http] bind {} failed: {}", addr, e);
            return;
        }
    };
    eprintln!("[http] listening on http://{}/", addr);
    let limiter = Arc::new(RateLimiter::new());
    for mut st in listener.incoming().flatten() {
        let cur = ACTIVE_HTTP_CONNS.fetch_add(1, Ordering::SeqCst);
        if cur >= MAX_ACTIVE_HTTP_CONNS {
            ACTIVE_HTTP_CONNS.fetch_sub(1, Ordering::SeqCst);
            send_json(&mut st, 503, "{\"error\":\"ServiceUnavailable\",\"message\":\"连接数已达上限，请稍后重试\"}");
            continue;
        }
        let bus = Arc::clone(&bus);
        let auth = Arc::clone(&auth);
        let notifier = notifier.clone();
        let cfg = cfg.clone();
        let limiter = Arc::clone(&limiter);
        if std::thread::Builder::new()
            .stack_size(256 * 1024)
            .name("http".into())
            .spawn(move || {
                let _guard = ConnGuard;
                handle_conn(st, bus, auth, notifier, cfg, limiter);
            })
            .is_err()
        {
            ACTIVE_HTTP_CONNS.fetch_sub(1, Ordering::SeqCst);
        }
    }
}

fn handle_conn(
    mut st: TcpStream,
    bus: Arc<Bus>,
    auth: Arc<AuthManager>,
    notifier: Notifier,
    cfg: HttpConfig,
    limiter: Arc<RateLimiter>,
) {
    let req = match read_request(&mut st) {
        Some(r) => r,
        None => return,
    };
    let q = parse_query(&req.query);

    if let Some(c) = auth_ctx(&req, &q, &auth, &cfg.token) {
        if let Ctx::User(s) = &c {
            if s.must_change_password && !must_change_exempt(&req.method, &req.path) {
                send_json(
                    &mut st,
                    403,
                    "{\"error\":\"PasswordChangeRequired\",\"message\":\"首次登录请先修改初始密码，再进行其他操作\"}",
                );
                return;
            }
        }
        if matches!(c, Ctx::Machine)
            && req.method == "POST"
            && !matches!(
                req.path.as_str(),
                "/api/incidents/feedback"
                    | "/api/challenge/acknowledge"
                    | "/api/set_mode"
                    | "/api/sensors/event"
            )
        {
            send_json(
                &mut st,
                403,
                "{\"error\":\"Forbidden\",\"message\":\"机器 token 仅支持状态读取、传感器上报、事件反馈/应答与模式切换\"}",
            );
            return;
        }
    }

    if req.method == "POST"
        && bearer(&req).is_none()
        && !machine_ok(&req, &q, &cfg.token)
        && cookie_token(&req).is_some()
        && !origin_ok(&req)
    {
        send_json(&mut st, 403, "{\"error\":\"Forbidden\",\"message\":\"跨站请求被拒绝\"}");
        return;
    }

    if req.method == "GET" && (req.path == "/" || req.path == "/index.html") {
        send_raw(&mut st, 200, "OK", "text/html; charset=utf-8", cfg.html.as_bytes(), &[]);
        return;
    }

    if req.method == "GET" && req.path == "/api/events/stream" {
        if auth_ctx(&req, &q, &auth, &cfg.token).is_none() {
            send_json(&mut st, 401, "{\"error\":\"unauthorized\"}");
            return;
        }
        let cur_sse = ACTIVE_SSE_STREAMS.fetch_add(1, Ordering::SeqCst);
        if cur_sse >= MAX_ACTIVE_SSE_STREAMS {
            ACTIVE_SSE_STREAMS.fetch_sub(1, Ordering::SeqCst);
            send_json(&mut st, 429, "{\"error\":\"too_many_streams\"}");
            return;
        }
        let _sse_guard = SseGuard;
        stream_events(&mut st, &bus, &q);
        return;
    }

    if req.method == "GET" {
        handle_get(&mut st, &req, &q, &bus, &auth, &cfg);
    } else if req.method == "POST" {
        handle_post(&mut st, &req, &q, &bus, &auth, &notifier, &cfg, &limiter);
    } else {
        send_json(&mut st, 404, "{\"error\":\"not found\"}");
    }
}

fn handle_get(
    st: &mut TcpStream,
    req: &Req,
    q: &HashMap<String, String>,
    bus: &Arc<Bus>,
    auth: &Arc<AuthManager>,
    cfg: &HttpConfig,
) {
    match req.path.as_str() {
        "/api/auth/current" => {
            match auth_ctx(req, q, auth, &cfg.token) {
                Some(Ctx::User(s)) => send_json(
                    st,
                    200,
                    &format!("{{\"authenticated\":true,\"user\":{}}}", s.to_json()),
                ),
                _ => send_json(st, 200, "{\"authenticated\":false,\"user\":null}"),
            }
        }
        "/api/ha/status" => {
            let s = bus.lock();
            send_json(st, 200, &state::ha_json(&s));
        }
        "/api/routine" => send_json(st, 200, &state::routine_json()),
        "/api/neighbor_spectrum" => {
            let s = bus.lock();
            send_json(st, 200, &state::neighbor_json(&s.neighbor, &s.home_mac));
        }
        "/api/placement" => {
            let s = bus.lock();
            send_json(st, 200, &state::placement_json(&s.placement));
        }
        "/api/multilink" => {
            let s = bus.lock();
            send_json(st, 200, &state::multilink_json(&s));
        }
        "/api/phase2/pilot" => {
            let s = bus.lock();
            let incidents: Vec<String> = s.incidents.list_recent(10).iter().map(|i| i.to_json()).collect();
            if q.get("format").map(|v| v.as_str()) == Some("markdown") {
                send_text(st, 200, "text/markdown; charset=utf-8", &state::pilot_markdown_report(&s));
            } else {
                send_json(
                    st,
                    200,
                    &format!(
                        "{{\"pilot_dashboard\":{},\"pilot_summary\":{},\"auto_calibration\":{},\"recent_incidents\":[{}]}}",
                        state::pilot_dashboard_json(&s),
                        state::pilot_json(&s),
                        state::calib_json(&s.calib),
                        incidents.join(",")
                    ),
                );
            }
        }
        "/api/phase2/pilot/report" => {
            let s = bus.lock();
            send_text(st, 200, "text/markdown; charset=utf-8", &state::pilot_markdown_report(&s));
        }
        "/api/incidents" => {
            let s = bus.lock();
            let incidents: Vec<String> = s.incidents.list_recent(30).iter().map(|i| i.to_json()).collect();
            send_json(st, 200, &format!("{{\"incidents\":[{}]}}", incidents.join(",")));
        }
        "/api/devices" => {
            let s = bus.lock();
            send_json(st, 200, &s.devices.to_json());
        }
        "/api/sensors/status" => {
            let s = bus.lock();
            let items: Vec<String> = s.devices.devices.iter().map(|d| d.to_json()).collect();
            send_json(
                st,
                200,
                &format!(
                    "{{\"total_devices\":{},\"security_mode\":\"{}\",\"devices\":[{}]}}",
                    s.devices.devices.len(),
                    json::escape(&s.security_mode),
                    items.join(",")
                ),
            );
        }
        "/api/health" => {
            let s = bus.lock();
            send_json(st, 200, &state::health_json(&s));
        }
        "/api/state" => {
            if auth_ctx(req, q, auth, &cfg.token).is_none() {
                send_json(st, 401, "{\"error\":\"unauthorized\"}");
                return;
            }
            let s = bus.lock();
            send_json(st, 200, &state::state_json(&s));
        }
        "/api/events" => {
            if auth_ctx(req, q, auth, &cfg.token).is_none() {
                send_json(st, 401, "{\"error\":\"unauthorized\"}");
                return;
            }
            let s = bus.lock();
            let latest = s.events.latest_seq();
            let since: u64 = q.get("since").and_then(|v| v.parse().ok()).unwrap_or(latest.saturating_sub(50));
            let limit: usize = q
                .get("limit")
                .and_then(|v| v.parse().ok())
                .unwrap_or(100)
                .min(500);
            let items = s.events.since(since, limit);
            send_json(
                st,
                200,
                &format!(
                    "{{\"latest_seq\":{},\"events\":{}}}",
                    s.events.latest_seq(),
                    state::events_array(&items)
                ),
            );
        }
        "/api/logs" => {
            if auth_ctx(req, q, auth, &cfg.token).is_none() {
                send_json(st, 401, "{\"error\":\"Unauthorized\",\"message\":\"未登录或登录已过期，请重新登录\"}");
                return;
            }
            let s = bus.lock();
            let events = s.events.recent(80, None);
            let tail = s.events.tail(80);
            let tail_json: Vec<String> = tail.iter().map(|l| format!("\"{}\"", json::escape(l))).collect();
            send_json(
                st,
                200,
                &format!(
                    "{{\"events\":{},\"tail\":[{}],\"log_file_size\":{}}}",
                    state::events_array(&events),
                    tail_json.join(","),
                    s.events.log_size()
                ),
            );
        }
        "/api/logs/download" => {
            if auth_ctx(req, q, auth, &cfg.token).is_none() {
                send_json(st, 401, "{\"error\":\"Unauthorized\"}");
                return;
            }
            let path = {
                let s = bus.lock();
                s.events.log_path().to_path_buf()
            };
            let body = fs::read(path).unwrap_or_default();
            send_raw(
                st,
                200,
                "OK",
                "text/plain; charset=utf-8",
                &body,
                &["Content-Disposition: attachment; filename=\"safehalo.log\"".to_string()],
            );
        }
        "/api/users" => match auth_ctx(req, q, auth, &cfg.token) {
            Some(c) if c.role() == "admin" => {
                let users: Vec<String> = auth.list_users().iter().map(|u| u.to_json()).collect();
                send_json(st, 200, &format!("{{\"users\":[{}]}}", users.join(",")));
            }
            Some(_) => send_json(st, 403, "{\"error\":\"Forbidden\",\"message\":\"权限不足，仅管理员可查看成员列表\"}"),
            None => send_json(st, 401, "{\"error\":\"Unauthorized\",\"message\":\"未登录或登录已过期，请重新登录\"}"),
        },
        "/api/status" => {
            if auth_ctx(req, q, auth, &cfg.token).is_none() {
                send_json(st, 401, "{\"error\":\"Unauthorized\",\"message\":\"未登录或登录已过期，请重新登录\"}");
                return;
            }
            let s = bus.lock();
            send_json(st, 200, &state::status_json(&s));
        }
        _ => send_json(st, 404, "{\"error\":\"not found\"}"),
    }
}

#[allow(clippy::too_many_arguments)]
fn handle_post(
    st: &mut TcpStream,
    req: &Req,
    q: &HashMap<String, String>,
    bus: &Arc<Bus>,
    auth: &Arc<AuthManager>,
    notifier: &Notifier,
    cfg: &HttpConfig,
    limiter: &Arc<RateLimiter>,
) {
    let data = json_body(req);
    match req.path.as_str() {
        "/api/auth/login" => {
            let username = jstr(&data, "username");
            let password = jstr(&data, "password");
            let ip = st.peer_addr().map(|a| a.ip().to_string()).unwrap_or_else(|_| "unknown".into());
            let key = format!("login:{}", ip);
            if !limiter.allow(&key, 5, 60.0) {
                {
                    let mut s = bus.lock();
                    s.events.push(
                        "LOGIN_LOCKED",
                        "WARN",
                        &format!("登录尝试过于频繁，已临时限制（来源 {}）", ip),
                        "{}",
                    );
                }
                bus.cv.notify_all();
                let body = format!(
                    "{{\"success\":false,\"message\":\"{}\"}}",
                    json::escape("登录尝试过于频繁，请 60 秒后再试")
                );
                send_raw(
                    st,
                    429,
                    "Too Many Requests",
                    "application/json; charset=utf-8",
                    body.as_bytes(),
                    &["Retry-After: 60".to_string()],
                );
                return;
            }
            match auth.login(username.trim(), &password, &ip) {
                Ok((token, sess)) => {
                    limiter.reset(&key);
                    {
                        let mut s = bus.lock();
                        s.events.push(
                            "LOGIN_OK",
                            "INFO",
                            &format!("成员 [{}] 登录成功（来源 {}）", sess.username, ip),
                            "{}",
                        );
                    }
                    bus.cv.notify_all();
                    let secure = if cfg.cookie_secure { "; Secure" } else { "" };
                    let cookie = format!(
                        "Set-Cookie: safehalo_session={}; Path=/; HttpOnly; SameSite=Lax; Max-Age={}{}",
                        token,
                        7 * 24 * 3600,
                        secure
                    );
                    let body = format!(
                        "{{\"success\":true,\"token\":\"{}\",\"user\":{},\"message\":\"登录成功\"}}",
                        json::escape(&token),
                        sess.to_json()
                    );
                    send_raw(st, 200, "OK", "application/json; charset=utf-8", body.as_bytes(), &[cookie]);
                }
                Err(msg) => {
                    {
                        let mut s = bus.lock();
                        s.events.push(
                            "LOGIN_FAILED",
                            "WARN",
                            &format!("登录失败：用户 [{}]（来源 {}）", username.trim(), ip),
                            "{}",
                        );
                    }
                    bus.cv.notify_all();
                    send_json(
                        st,
                        401,
                        &format!("{{\"success\":false,\"message\":\"{}\"}}", json::escape(&msg)),
                    );
                }
            }
        }
        "/api/placement/save" => {
            let configured = { bus.lock().placement.is_configured };
            if configured {
                match auth_ctx(req, q, auth, &cfg.token) {
                    Some(c) if c.role() == "admin" => {}
                    Some(_) => {
                        send_json(st, 403, "{\"error\":\"Forbidden\",\"message\":\"权限不足，仅管理员可修改守护方案\"}");
                        return;
                    }
                    None => {
                        send_json(st, 401, "{\"error\":\"Unauthorized\",\"message\":\"初始化已完成，请先登录管理员账号再修改守护方案\"}");
                        return;
                    }
                }
            }
            let mut p = {
                let s = bus.lock();
                s.placement.clone()
            };
            let rt = jstr(&data, "room_type");
            if !rt.is_empty() { p.room_type = rt; }
            let rn = jstr(&data, "room_name");
            if !rn.is_empty() { p.room_name = rn; }
            let mh = jstr(&data, "mount_height");
            if !mh.is_empty() { p.mount_height = mh; }
            p.room_area_sqm = data.get("room_area_sqm").and_then(|x| x.as_f64()).unwrap_or(p.room_area_sqm);
            p.has_pets = jbool(&data, "has_pets", p.has_pets);
            p.has_water_splash = jbool(&data, "has_water_splash", p.has_water_splash);
            let cn = jstr(&data, "care_nickname");
            if !cn.is_empty() { p.care_nickname = cn; }
            let la = jstr(&data, "living_arrangement");
            if !la.is_empty() { p.living_arrangement = la; }
            let mp = jstr(&data, "mobility_pace");
            if !mp.is_empty() { p.mobility_pace = mp; }
            let wh = jstr(&data, "wake_habit");
            if !wh.is_empty() { p.wake_habit = wh; }
            p.enable_gentle_challenge = jbool(&data, "enable_gentle_challenge", p.enable_gentle_challenge);
            p.is_configured = true;
            p.recompute_tuned();
            let token = jstr(&data, "pushplus_token");
            let webhook = jstr(&data, "webhook_url");
            let bark = jstr(&data, "bark_key");
            if !token.is_empty() || !webhook.is_empty() || !bark.is_empty() {
                notifier.update(|c| {
                    if !token.is_empty() {
                        c.pushplus_token = token.clone();
                    }
                    if !webhook.is_empty() {
                        c.webhook_url = webhook.clone();
                    }
                    if !bark.is_empty() {
                        c.bark_key = bark.clone();
                    }
                });
            }
            p.save(&notifier_dir(bus));
            let cfg_json = p.to_json();
            let tuned = p.tuned_json();
            let msg = format!(
                "已为【{}】开启【{}】安心守护方案，正在进行 5 秒静默环境自适应！",
                p.care_nickname, p.room_name
            );
            {
                let mut s = bus.lock();
                s.placement = p;
                s.control.recalibrate = true;
            }
            send_json(
                st,
                200,
                &format!(
                    "{{\"success\":true,\"message\":\"{}\",\"config\":{},\"tuned_parameters\":{}}}",
                    json::escape(&msg),
                    cfg_json,
                    tuned
                ),
            );
        }
        "/api/onboarding/test_push" => {
            let configured = { bus.lock().placement.is_configured };
            if configured {
                match auth_ctx(req, q, auth, &cfg.token) {
                    Some(c) if c.role() == "admin" => {}
                    Some(_) => {
                        send_json(st, 403, "{\"error\":\"Forbidden\",\"message\":\"权限不足，仅管理员可发送测试通知\"}");
                        return;
                    }
                    None => {
                        send_json(st, 401, "{\"error\":\"Unauthorized\",\"message\":\"初始化已完成，请先登录管理员账号再发送测试通知\"}");
                        return;
                    }
                }
            }
            let token = jstr(&data, "pushplus_token");
            let webhook = jstr(&data, "webhook_url");
            let bark = jstr(&data, "bark_key");
            let nickname = {
                let s = bus.lock();
                let n = jstr(&data, "care_nickname");
                if n.is_empty() { s.placement.care_nickname.clone() } else { n }
            };
            let room = {
                let s = bus.lock();
                let r = jstr(&data, "room_name");
                if r.is_empty() { s.placement.room_name.clone() } else { r }
            };
            if !token.is_empty() || !webhook.is_empty() || !bark.is_empty() {
                notifier.update(|c| {
                    if !token.is_empty() {
                        c.pushplus_token = token.clone();
                    }
                    if !webhook.is_empty() {
                        c.webhook_url = webhook.clone();
                    }
                    if !bark.is_empty() {
                        c.bark_key = bark.clone();
                    }
                });
            }
            notifier.send(Notice::Generic {
                title: format!("【平安联结测试】{}的{}守护已就绪", nickname, room),
                content: "这是一条来自 SafeHalo 的温和测试消息。全屋无摄像头纯无线电波守护已开启，遇到需要关心的情况时会第一时间通知您。".into(),
                room,
                ha_event: "routine_anomaly".into(),
            });
            send_json(
                st,
                200,
                &format!(
                    "{{\"success\":true,\"message\":\"{}\"}}",
                    json::escape(&format!("平安测试消息已发送（守护对象：{}），请查看手机微信或推送终端！", nickname))
                ),
            );
        }
        "/api/challenge/acknowledge" => {
            let ip = st.peer_addr().map(|a| a.ip().to_string()).unwrap_or_else(|_| "unknown".into());
            if !limiter.allow(&format!("ack:{}", ip), 1, 5.0) {
                send_json(st, 429, "{\"success\":false,\"message\":\"操作过于频繁，请稍后再试\"}");
                return;
            }
            let source = {
                let s = jstr(&data, "source");
                if s.is_empty() { "本地按钮/语音安全应答".to_string() } else { s }
            };
            {
                let mut s = bus.lock();
                s.control.ack_safe = true;
                s.state = "MOVING".into();
                s.events.push(
                    "CHALLENGE_ACK",
                    "INFO",
                    &format!("收到本地安全确认应答（{}，来源 {}），疑似跌倒警报已本地解除", source, ip),
                    "{}",
                );
            }
            bus.cv.notify_all();
            send_json(st, 200, &ok_msg("已收到安全确认应答，疑似跌倒警报已本地解除，未打扰家属。"));
        }
        "/api/incidents/feedback" => {
            if auth_ctx(req, q, auth, &cfg.token).is_none() {
                send_json(st, 401, "{\"error\":\"Unauthorized\",\"message\":\"未登录或登录已过期，请重新登录\"}");
                return;
            }
            let event_id = jstr(&data, "event_id");
            let is_false = jbool(&data, "is_false_alarm", true);
            let category = jstr(&data, "category");
            let notes = jstr(&data, "notes");
            let operator = {
                let o = jstr(&data, "operator");
                if o.is_empty() { "miniprogram_family".into() } else { o }
            };
            let dir = notifier_dir(bus);
            let (ok, msg, rec, pilot_dash) = {
                let mut s = bus.lock();
                let cat = if category.is_empty() { "other" } else { &category };
                let (ok, msg, rec) = s.incidents.submit_feedback(
                    if event_id.is_empty() { None } else { Some(&event_id) },
                    is_false,
                    cat,
                    &notes,
                    &operator,
                );
                if ok && is_false {
                    let adapt_msg = s.placement.apply_false_alarm_feedback(cat);
                    s.placement.save(&dir);
                    s.events.push(
                        "FALSE_ALARM_FEEDBACK",
                        "INFO",
                        &format!("针对误报反馈进行自适应参数微调: {}", adapt_msg),
                        "{}",
                    );
                }
                let dash = state::pilot_dashboard_json(&s);
                (ok, msg, rec, dash)
            };
            bus.cv.notify_all();
            let rec_json = rec.map(|r| r.to_json()).unwrap_or_else(|| "null".into());
            send_json(
                st,
                200,
                &format!(
                    "{{\"success\":{},\"message\":\"{}\",\"incident\":{},\"pilot_dashboard\":{}}}",
                    ok,
                    json::escape(&msg),
                    rec_json,
                    pilot_dash
                ),
            );
        }
        "/api/test_push" => match auth_ctx(req, q, auth, &cfg.token) {
            Some(c) if c.role() == "admin" => {
                if !limiter.allow(&format!("test_push:{}", c.username()), 1, 10.0) {
                    send_json(st, 429, "{\"success\":false,\"message\":\"测试推送过于频繁，请 10 秒后再试\"}");
                    return;
                }
                let token = jstr(&data, "pushplus_token");
                let serverchan = jstr(&data, "serverchan_key");
                let webhook = jstr(&data, "webhook_url");
                let sms = jstr(&data, "sms_webhook_url");
                let bark = jstr(&data, "bark_key");
                if !token.is_empty() || !serverchan.is_empty() || !webhook.is_empty() || !sms.is_empty() || !bark.is_empty() {
                    notifier.update(|c| {
                        if !token.is_empty() {
                            c.pushplus_token = token.clone();
                        }
                        if !serverchan.is_empty() {
                            c.serverchan_key = serverchan.clone();
                        }
                        if !webhook.is_empty() {
                            c.webhook_url = webhook.clone();
                        }
                        if !sms.is_empty() {
                            c.sms_webhook_url = sms.clone();
                        }
                        if !bark.is_empty() {
                            c.bark_key = bark.clone();
                        }
                    });
                }
                notifier.send(Notice::Fall {
                    room: "家庭测试区域".into(),
                    confidence: 0.96,
                    details: format!(
                        "管理员 [{}] 点击控制台【发送测试告警】",
                        c.username()
                    ),
                    bpm: 18.0,
                    event_id: "evt_test".into(),
                    household: "H01".into(),
                });
                send_json(st, 200, &ok_msg("测试告警已发送并保存配置，请检查微信通知！"));
            }
            Some(_) => send_json(st, 403, "{\"error\":\"Forbidden\",\"message\":\"权限不足，仅管理员可触发测试推送\"}"),
            None => send_json(st, 401, "{\"error\":\"Unauthorized\",\"message\":\"未登录或登录已过期，请重新登录\"}"),
        },
        "/api/notifier/config" => match auth_ctx(req, q, auth, &cfg.token) {
            Some(c) if c.role() == "admin" => {
                notifier.update(|cfg_mut| {
                    if data.get("pushplus_token").is_some() {
                        cfg_mut.pushplus_token = jstr(&data, "pushplus_token").trim().to_string();
                    }
                    if data.get("serverchan_key").is_some() {
                        cfg_mut.serverchan_key = jstr(&data, "serverchan_key").trim().to_string();
                    }
                    if data.get("webhook_url").is_some() {
                        cfg_mut.webhook_url = jstr(&data, "webhook_url").trim().to_string();
                    }
                    if data.get("ha_webhook_url").is_some() {
                        cfg_mut.ha_webhook_url = jstr(&data, "ha_webhook_url").trim().to_string();
                    }
                    if data.get("sms_webhook_url").is_some() {
                        cfg_mut.sms_webhook_url = jstr(&data, "sms_webhook_url").trim().to_string();
                    }
                    if data.get("bark_key").is_some() {
                        cfg_mut.bark_key = jstr(&data, "bark_key").trim().to_string();
                    }
                });
                {
                    let mut s = bus.lock();
                    s.events.push(
                        "NOTIFIER_CONFIG",
                        "INFO",
                        &format!("管理员 [{}] 更新了告警通知渠道配置", c.username()),
                        "{}",
                    );
                }
                bus.cv.notify_all();
                send_json(st, 200, &ok_msg("告警通知配置已成功保存！"));
            }
            Some(_) => send_json(st, 403, "{\"error\":\"Forbidden\",\"message\":\"权限不足，仅管理员可修改通知配置\"}"),
            None => send_json(st, 401, "{\"error\":\"Unauthorized\",\"message\":\"未登录或登录已过期，请重新登录\"}"),
        },
        "/api/trigger_inspect" => match auth_ctx(req, q, auth, &cfg.token) {
            Some(c) => {
                {
                    let mut s = bus.lock();
                    s.control.trigger_inspect = true;
                    s.state = "FALL_SUSPECT".into();
                    s.sample_rate = 100.0;
                    s.events.push(
                        "INSPECTION",
                        "INFO",
                        &format!("成员 [{}] 手动请求启动定向深度排查 (激振提频至 100Hz)", c.username()),
                        "{}",
                    );
                }
                bus.cv.notify_all();
                send_json(
                    st,
                    200,
                    &ok_msg("已向射频节点下发指令：提频至 100Hz，正在定向聚焦地面生命呼吸体征..."),
                );
            }
            None => send_json(st, 401, "{\"error\":\"Unauthorized\",\"message\":\"未登录或登录已过期，请重新登录\"}"),
        },
        "/api/set_mode" => match auth_ctx(req, q, auth, &cfg.token) {
            Some(c) if c.role() == "admin" || matches!(c, Ctx::Machine) => {
                if !limiter.allow(&format!("set_mode:{}", c.username()), 1, 2.0) {
                    send_json(st, 429, "{\"success\":false,\"message\":\"模式切换过于频繁，请稍后再试\"}");
                    return;
                }
                let mode = {
                    let m = jstr(&data, "mode");
                    if m.is_empty() { "FALL_GUARD".into() } else { m }
                };
                {
                    let mut s = bus.lock();
                    s.security_mode = mode.clone();
                    s.events.push(
                        "SECURITY_MODE",
                        "INFO",
                        &format!("安防守护模式已由管理员 [{}] 切换为: {}", c.username(), mode),
                        "{}",
                    );
                }
                bus.cv.notify_all();
                send_json(st, 200, &ok_msg(&format!("模式已切换为 {}", mode)));
            }
            Some(_) => send_json(st, 403, "{\"error\":\"Forbidden\",\"message\":\"权限不足，仅管理员可修改安防守护模式\"}"),
            None => send_json(st, 401, "{\"error\":\"Unauthorized\",\"message\":\"未登录或登录已过期，请重新登录\"}"),
        },
        "/api/feedback/false_alarm" => match auth_ctx(req, q, auth, &cfg.token) {
            Some(_) => {
                let reason = {
                    let r = jstr(&data, "reason");
                    if r.is_empty() { "家属在控制台标记误报".into() } else { r }
                };
                let dir = notifier_dir(bus);
                let tuned = {
                    let mut s = bus.lock();
                    s.state = "EMPTY".into();
                    let adapt_msg = s.placement.apply_false_alarm_feedback("other");
                    s.placement.save(&dir);
                    s.events.push(
                        "FALSE_ALARM_FEEDBACK",
                        "INFO",
                        &format!("{}: {}", reason, adapt_msg),
                        "{}",
                    );
                    s.placement.tuned_json()
                };
                bus.cv.notify_all();
                send_json(
                    st,
                    200,
                    &format!(
                        "{{\"success\":true,\"message\":\"{}\",\"tuned_parameters\":{}}}",
                        json::escape("已记录误报反馈并更新自适应参数"),
                        tuned
                    ),
                );
            }
            None => send_json(st, 401, "{\"error\":\"Unauthorized\"}"),
        },
        "/api/devices/command" => match auth_ctx(req, q, auth, &cfg.token) {
            Some(c) if c.role() == "admin" => {
                let cmd = jstr(&data, "command").trim().to_string();
                if !crate::serial::is_command_allowed(&cmd) {
                    send_json(st, 400, "{\"success\":false,\"message\":\"Command not permitted in whitelist\"}");
                    return;
                }
                let dev_id = {
                    let d = jstr(&data, "device_id");
                    if d.is_empty() { "csi_probe_0".into() } else { d }
                };
                {
                    let mut s = bus.lock();
                    if s.control.pending_commands.len() < 32 {
                        s.control.pending_commands.push(format!("{}\n", cmd));
                    }
                    s.events.push(
                        "DEVICE_COMMAND",
                        "INFO",
                        &format!("管理员 [{}] 向设备 [{}] 下发白名单指令: {}", c.username(), dev_id, cmd),
                        &format!("{{\"device_id\":\"{}\",\"command\":\"{}\"}}", json::escape(&dev_id), json::escape(&cmd)),
                    );
                }
                bus.cv.notify_all();
                send_json(
                    st,
                    200,
                    &format!(
                        "{{\"success\":true,\"message\":\"{}\",\"device_id\":\"{}\",\"command\":\"{}\"}}",
                        json::escape("指令已成功下发"),
                        json::escape(&dev_id),
                        json::escape(&cmd)
                    ),
                );
            }
            Some(_) => send_json(st, 403, "{\"error\":\"Forbidden\",\"message\":\"权限不足，仅管理员可向硬件设备下发指令\"}"),
            None => send_json(st, 401, "{\"error\":\"Unauthorized\",\"message\":\"未登录或登录已过期，请重新登录\"}"),
        },
        "/api/sensors/actuate" => match auth_ctx(req, q, auth, &cfg.token) {
            Some(c) if c.role() == "admin" => {
                let cmd = jstr(&data, "command").trim().to_string();
                let dev_id = jstr(&data, "device_id").trim().to_string();
                if cmd.is_empty() {
                    send_json(st, 400, "{\"success\":false,\"message\":\"Missing command\"}");
                    return;
                }
                {
                    let mut s = bus.lock();
                    if crate::serial::is_command_allowed(&cmd) && s.control.pending_commands.len() < 32 {
                        s.control.pending_commands.push(format!("{}\n", cmd));
                    }
                    s.events.push(
                        "SENSOR_ACTUATE",
                        "INFO",
                        &format!("管理员 [{}] 向生态设备 [{}] 发送作动指令: {}", c.username(), dev_id, cmd),
                        "{}",
                    );
                }
                bus.cv.notify_all();
                send_json(
                    st,
                    200,
                    &format!(
                        "{{\"success\":true,\"message\":\"{}\",\"device_id\":\"{}\",\"command\":\"{}\"}}",
                        json::escape(&format!("作动指令已下发: {}", cmd)),
                        json::escape(&dev_id),
                        json::escape(&cmd)
                    ),
                );
            }
            Some(_) => send_json(st, 403, "{\"error\":\"Forbidden\",\"message\":\"权限不足，仅管理员可执行作动测试\"}"),
            None => send_json(st, 401, "{\"error\":\"Unauthorized\",\"message\":\"未登录或登录已过期，请重新登录\"}"),
        },
        "/api/sensors/event" => {
            if !cfg.token.is_empty() && auth_ctx(req, q, auth, &cfg.token).is_none() {
                send_json(st, 401, "{\"error\":\"Unauthorized\",\"message\":\"传感器事件上报需要有效登录态或 api_token\"}");
                return;
            }
            let ip = st.peer_addr().map(|a| a.ip().to_string()).unwrap_or_else(|_| "unknown".into());
            if !limiter.allow(&format!("sensor_ev:{}", ip), 30, 10.0) {
                send_json(st, 429, "{\"success\":false,\"message\":\"传感器事件上报过于频繁，请稍后再试\"}");
                return;
            }
            let device_id = jstr(&data, "device_id").trim().to_string();
            let event = {
                let e = jstr(&data, "event");
                if e.is_empty() { jstr(&data, "state") } else { e }
            }
            .trim()
            .to_string();
            if device_id.is_empty() || event.is_empty() {
                send_json(st, 400, "{\"success\":false,\"message\":\"Missing device_id or event\"}");
                return;
            }
            let ev_lower = event.to_ascii_lowercase();
            if matches!(
                ev_lower.as_str(),
                "voice_dismiss" | "dismiss" | "button_press" | "voice_help" | "help" | "sos" | "sos_pressed"
            ) && !limiter.allow(&format!("sensor_crit:{}", ip), 2, 5.0)
            {
                send_json(st, 429, "{\"success\":false,\"message\":\"关键语音/SOS操作过于频繁，请稍后再试\"}");
                return;
            }

            let now = timefmt::now_epoch();
            let mut action = "none";
            let mut message = "事件已接收并记录".to_string();
            let mut pending_notice: Option<Notice> = None;

            {
                let mut s = bus.lock();
                let room = {
                    let r = jstr(&data, "room");
                    if r.is_empty() { s.placement.room_name.clone() } else { r }
                };
                let household = s.household_id.clone();

                // 1. 查找或更新设备
                let dev = crate::serial::DeviceInfo {
                    id: device_id.clone(),
                    device_type: if device_id.contains("door") { "door".into() }
                        else if device_id.contains("pir") { "pir".into() }
                        else if device_id.contains("speaker") { "speaker".into() }
                        else if device_id.contains("sos") { "sos_button".into() }
                        else { "sensor".into() },
                    role: crate::serial::DeviceRole::Sensor,
                    path: "network".into(),
                    baud: 0,
                    status: event.clone(),
                    last_seen: now,
                };
                s.devices.register(dev);

                // 2. 联动逻辑判定 + 真实告警与串口指令下发
                if ev_lower == "voice_dismiss" || ev_lower == "dismiss" || ev_lower == "button_press" {
                    s.control.ack_safe = true;
                    if s.state == "FALL_SUSPECT" {
                        s.state = "RESTING".into();
                        action = "challenge_dismissed";
                        message = "老人语音应答正常，跌倒嫌疑已成功化解".into();
                        s.events.push("CHALLENGE_DISMISSED", "INFO", &format!("[{}] {}", device_id, message), "{}");
                    } else {
                        action = "challenge_dismissed";
                        message = "已收到语音/按键平安应答".into();
                    }
                } else if ev_lower == "voice_help" || ev_lower == "help" {
                    s.state = "FALL_CONFIRMED".into();
                    s.fall_alarms += 1;
                    action = "fall_escalated";
                    message = "老人语音呼救（喊救命），秒级升级确诊跌倒！".into();
                    let inc = s.incidents.record(&room, "FALL_ALARM", 1.0, 0.0, &message, &household);
                    s.events.push("FALL_ESCALATED", "CRITICAL", &format!("[{}] {}", device_id, message), "{}");
                    pending_notice = Some(Notice::Fall {
                        room,
                        confidence: 1.0,
                        details: message.clone(),
                        bpm: 0.0,
                        event_id: inc.event_id,
                        household,
                    });
                } else if ev_lower == "sos_pressed" || ev_lower == "sos" {
                    s.state = "FALL_CONFIRMED".into();
                    s.fall_alarms += 1;
                    action = "sos_emergency_alert";
                    message = format!("老人按下紧急 SOS 求救按钮！位置: {}", room);
                    let inc = s.incidents.record(&room, "FALL_ALARM", 1.0, 0.0, &message, &household);
                    s.events.push("SOS_EMERGENCY", "CRITICAL", &format!("[{}] {}", device_id, message), "{}");
                    pending_notice = Some(Notice::Fall {
                        room,
                        confidence: 1.0,
                        details: message.clone(),
                        bpm: 0.0,
                        event_id: inc.event_id,
                        household,
                    });
                } else if ev_lower.contains("open") {
                    let mode_norm = crate::security::SecurityMode::parse(&s.security_mode);
                    if mode_norm == crate::security::SecurityMode::ArmedAway {
                        action = "perimeter_breach_alert";
                        message = format!("离家布防模式下门磁触发！外围防线突破: {}", device_id);
                        s.incidents.record(&room, "SECURITY_INTRUSION", 0.95, 0.0, &message, &household);
                        s.events.push("SECURITY_INTRUSION", "ALARM", &message, "{}");
                        pending_notice = Some(Notice::Generic {
                            title: "🛡️【防盗告警】外围门磁防线突破！".into(),
                            content: message.clone(),
                            room,
                            ha_event: "security_intrusion".into(),
                        });
                    } else if mode_norm == crate::security::SecurityMode::Curfew {
                        action = "curfew_wandering_alert";
                        message = format!("老人防走失模式告警：深夜外门推开: {}", device_id);
                        s.incidents.record(&room, "SECURITY_INTRUSION", 0.95, 0.0, &message, &household);
                        s.events.push("CURFEW_WANDERING", "ALARM", &message, "{}");
                        pending_notice = Some(Notice::Generic {
                            title: "⚠️【防走失告警】深夜外门被推开！".into(),
                            content: message.clone(),
                            room,
                            ha_event: "curfew_wandering".into(),
                        });
                    }
                } else if ev_lower.contains("motion") {
                    let mode_norm = crate::security::SecurityMode::parse(&s.security_mode);
                    if mode_norm == crate::security::SecurityMode::ArmedAway {
                        action = "confirmed_intrusion_alert";
                        message = format!("离家布防模式下 PIR 人体红外触发！确认入侵: {}", room);
                        s.incidents.record(&room, "SECURITY_INTRUSION", 0.98, 0.0, &message, &household);
                        s.events.push("SECURITY_INTRUSION", "ALARM", &message, "{}");
                        pending_notice = Some(Notice::Generic {
                            title: "🛡️【防盗告警】PIR 确认非法人员入侵！".into(),
                            content: message.clone(),
                            room,
                            ha_event: "security_intrusion".into(),
                        });
                    } else {
                        action = "burst_rate_triggered";
                        message = "PIR 动能联动触发 CSI 探针提频采样".into();
                        if s.control.pending_commands.len() < 32 {
                            s.control.pending_commands.push("BURST_RATE:50\n".into());
                        }
                        s.events.push("BURST_RATE_TRIGGER", "INFO", &format!("[{}] {}", device_id, message), "{}");
                    }
                }
            }
            if let Some(notice) = pending_notice {
                notifier.send(notice);
            }
            bus.cv.notify_all();

            send_json(
                st,
                200,
                &format!(
                    "{{\"success\":true,\"action\":\"{}\",\"message\":\"{}\",\"device_id\":\"{}\"}}",
                    action,
                    json::escape(&message),
                    json::escape(&device_id)
                ),
            );
        }
        "/api/sensors/register" => match auth_ctx(req, q, auth, &cfg.token) {
            Some(c) if c.role() == "admin" => {
                let dev_id = jstr(&data, "device_id");
                let stype = jstr(&data, "sensor_type");
                let path = jstr(&data, "path");
                if dev_id.is_empty() {
                    send_json(st, 400, "{\"success\":false,\"message\":\"Missing device_id\"}");
                    return;
                }
                let now = timefmt::now_epoch();
                {
                    let mut s = bus.lock();
                    let dev = crate::serial::DeviceInfo {
                        id: dev_id.clone(),
                        device_type: if stype.is_empty() { "sensor".into() } else { stype },
                        role: crate::serial::DeviceRole::Sensor,
                        path: if path.is_empty() { "network".into() } else { path },
                        baud: 0,
                        status: "online".into(),
                        last_seen: now,
                    };
                    s.devices.register(dev);
                    s.events.push(
                        "SENSOR_REGISTERED",
                        "INFO",
                        &format!("管理员 [{}] 注册新传感器设备: {}", c.username(), dev_id),
                        "{}",
                    );
                }
                bus.cv.notify_all();
                send_json(st, 200, &format!("{{\"success\":true,\"device_id\":\"{}\"}}", json::escape(&dev_id)));
            }
            Some(_) => send_json(st, 403, "{\"error\":\"Forbidden\",\"message\":\"仅管理员可注册新传感器\"}"),
            None => send_json(st, 401, "{\"error\":\"Unauthorized\"}"),
        },
        "/api/auth/logout" => {
            if let Some(t) = cookie_token(req) {
                auth.logout(&t);
            }
            let secure = if cfg.cookie_secure { "; Secure" } else { "" };
            send_raw(
                st,
                200,
                "OK",
                "application/json; charset=utf-8",
                ok_msg("已安全退出登录").as_bytes(),
                &[format!(
                    "Set-Cookie: safehalo_session=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0{}",
                    secure
                )],
            );
        }
        "/api/auth/update_password" => match auth_ctx(req, q, auth, &cfg.token) {
            Some(c) => {
                let (ok, msg) = auth.update_password(
                    c.username(),
                    &jstr(&data, "old_password"),
                    &jstr(&data, "new_password"),
                );
                send_json(
                    st,
                    if ok { 200 } else { 400 },
                    &format!(
                        "{{\"success\":{},\"message\":\"{}\"}}",
                        ok,
                        json::escape(&msg)
                    ),
                );
            }
            None => send_json(st, 401, "{\"error\":\"Unauthorized\",\"message\":\"未登录或登录已过期，请重新登录\"}"),
        },
        "/api/users/create" | "/api/users/delete" => match auth_ctx(req, q, auth, &cfg.token) {
            Some(c) if c.role() == "admin" => {
                let (ok, msg) = if req.path.ends_with("/create") {
                    auth.create_user(
                        &jstr(&data, "username"),
                        &jstr(&data, "password"),
                        {
                            let r = jstr(&data, "role");
                            if r.is_empty() { "family".into() } else { r }
                        }
                        .as_str(),
                        &jstr(&data, "display_name"),
                    )
                } else {
                    auth.delete_user(&jstr(&data, "username"), c.username())
                };
                send_json(
                    st,
                    if ok { 200 } else { 400 },
                    &format!(
                        "{{\"success\":{},\"message\":\"{}\"}}",
                        ok,
                        json::escape(&msg)
                    ),
                );
            }
            Some(_) => send_json(st, 403, "{\"error\":\"Forbidden\",\"message\":\"权限不足，仅管理员可管理成员\"}"),
            None => send_json(st, 401, "{\"error\":\"Unauthorized\",\"message\":\"未登录或登录已过期，请重新登录\"}"),
        },
        "/api/logs/clear" => match auth_ctx(req, q, auth, &cfg.token) {
            Some(c) if c.role() == "admin" => {
                {
                    let mut s = bus.lock();
                    s.events.clear();
                    s.events.push(
                        "LOG_CLEAR",
                        "INFO",
                        &format!("管理员 [{}] 清空了系统与事件审计日志", c.username()),
                        "{}",
                    );
                }
                bus.cv.notify_all();
                send_json(st, 200, &ok_msg("系统与事件日志已成功清空"));
            }
            Some(_) => send_json(st, 403, "{\"error\":\"Forbidden\",\"message\":\"权限不足，仅管理员可清空系统日志\"}"),
            None => send_json(st, 401, "{\"error\":\"Unauthorized\",\"message\":\"未登录或登录已过期，请重新登录\"}"),
        },
        _ => send_json(st, 404, "{\"error\":\"not found\"}"),
    }
}

fn stream_events(st: &mut TcpStream, bus: &Arc<Bus>, q: &HashMap<String, String>) {
    let _ = st.set_write_timeout(Some(Duration::from_secs(5)));
    let head = "HTTP/1.1 200 OK\r\nContent-Type: text/event-stream; charset=utf-8\r\nCache-Control: no-cache\r\nConnection: keep-alive\r\nX-Accel-Buffering: no\r\n\r\nretry: 3000\n\n";
    if st.write_all(head.as_bytes()).is_err() {
        return;
    }
    let mut last: u64 = {
        let s = bus.lock();
        q.get("since")
            .and_then(|v| v.parse().ok())
            .unwrap_or_else(|| s.events.latest_seq())
    };
    let mut guard = bus.lock();
    loop {
        let batch = guard.events.since(last, 128);
        if batch.is_empty() {
            let (g, timeout) = bus
                .cv
                .wait_timeout(guard, Duration::from_secs(15))
                .unwrap_or_else(|e| e.into_inner());
            guard = g;
            if timeout.timed_out() && st.write_all(b": ping\n\n").is_err() {
                return;
            }
            continue;
        }
        drop(guard);
        for ev in &batch {
            let frame = format!("id: {}\ndata: {}\n\n", ev.seq, ev.to_json());
            if st.write_all(frame.as_bytes()).is_err() {
                return;
            }
            last = ev.seq;
        }
        guard = bus.lock();
    }
}

fn notifier_dir(bus: &Arc<Bus>) -> String {
    let s = bus.lock();
    s.events.dir().display().to_string()
}

// Compile-time reminder to keep the embedded UI and the Python tree in sync.
#[allow(dead_code)]
fn _timestamp_hint() {
    let _ = timefmt::now();
}
