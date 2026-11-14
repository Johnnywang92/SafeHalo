// SafeHalo Rust standalone resident daemon.
// Inputs: --replay FILE | --udp ADDR:PORT | --serial DEV|auto | --stdin
// Outputs: static web console + JSON API + SSE (/api/events/stream), serial
//          actuation, incident persistence and notification fan-out.
// Equivalence outputs (--trace / --snapshot) stay byte-identical to the
// pre-service daemon.

mod auth;
mod calibrator;
mod detector;
mod dsp;
mod events;
mod http;
mod incidents;
mod inspector;
mod json;
mod notify;
mod object_confirm;
mod pet;
mod security;
mod serial;
mod state;
mod timefmt;

use std::fs::File;
use std::io::{BufRead, BufReader, Write};
use std::net::UdpSocket;
use std::sync::Arc;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use auth::AuthManager;
use calibrator::Calibrator;
use detector::{Detector, HumanState, TraceEvent};
use dsp::{Extractor, NeighborFilter, SignalFilter};
use events::EventLog;
use http::HttpConfig;
use incidents::IncidentStore;
use notify::{Notice, NotifyConfig, Notifier};
use object_confirm::{DEFAULT_CV_THRESHOLD, MIN_INTERVALS};
use security::{SecurityGuard, SecurityMode};
use state::{snapshot_neighbor, Bus, Placement, Shared};

struct Args {
    replay: Option<String>,
    replay_rate: f64,
    udp: Option<String>,
    serial: Option<String>,
    baud: u32,
    stdin: bool,
    http: String,
    no_http: bool,
    api_token: String,
    cookie_secure: bool,
    storage_dir: String,
    data_dir: String,
    trace: Option<String>,
    snapshot: Option<String>,
    run_secs: f64,
    max_packets: u64,
    home_mac: String,
    calibration_secs: f64,
    stillness_secs: f64,
    window: usize,
    rssi_floor: i32,
    object_confirm_secs: f64,
    object_confirm_cv: f64,
    object_confirm_min_intervals: usize,
    room_name: String,
    challenge_enabled: Option<bool>,
    household_id: String,
    web_html: Option<String>,
    pushplus_token: String,
    serverchan_key: String,
    notify_webhook: String,
    ha_webhook: String,
    sms_webhook: String,
    miniprogram_url: String,
    miniprogram_token: String,
    notify_timeout: Option<u64>,
}

fn usage() -> ! {
    eprintln!(
        "SafeHalo Rust daemon\n\
         Usage: safehalo-daemon [--replay FILE [--replay-rate N]] | [--udp ADDR:PORT] | [--serial DEV|auto [--baud N]] | [--stdin]\n\
         \x20 --http ADDR:PORT        HTTP listen address (default 127.0.0.1:8091)\n\
         \x20 --no-http               disable the web service\n\
         \x20 --api-token TOKEN       machine token; read + feedback/ack/mode-switch via Bearer/query (default off)\n\
         \x20 --cookie-secure         mark session cookies Secure (enable behind HTTPS reverse proxy)\n\
         \x20 --storage-dir DIR       model / incidents / events / notifier storage (default ./daemon-storage)\n\
         \x20 --data-dir DIR          user database directory (default ./data)\n\
         \x20 --trace FILE            write state/inspector event trace (JSONL)\n\
         \x20 --snapshot FILE         write final snapshot (JSON)\n\
         \x20 --run-secs N            stop after N wall seconds (0 = unlimited)\n\
         \x20 --max-packets N         stop after N input lines (0 = unlimited)\n\
         \x20 --home-mac MAC          home BSSID whitelist (default c0:06:c3:11:22:33)\n\
         \x20 --calibration-secs N    boot calibration window (default 4.0)\n\
         \x20 --stillness-secs N      targeted inspection duration (default 5.0)\n\
         \x20 --window N              feature window frames (default 30)\n\
         \x20 --rssi-floor N          weak-signal floor dBm (default -72)\n\
         \x20 --object-confirm-secs N stage-2 OBJECT confirmation window, 0 disables (default 0)\n\
         \x20 --object-confirm-cv N   max regular peak-interval CV (default 0.35)\n\
         \x20 --object-confirm-min-intervals N  min intervals for a stage-2 verdict (default 3)\n\
         \x20 --serial DEV            serial CSI input; DEV=auto discovers /dev/ttyUSB*/ttyACM*/cu.*\n\
         \x20 --baud N                serial baud rate (default 921600)\n\
         \x20 --room-name NAME        override the monitored room name\n\
         \x20 --challenge-enabled N   enable the gentle challenge beep (1/0; default from placement)\n\
         \x20 --household-id ID       household identifier used in incident records (default H01)\n\
         \x20 --web-html FILE         override the embedded console HTML\n\
         \x20 --pushplus-token T      PushPlus channel token\n\
         \x20 --serverchan-key K      ServerChan send key\n\
         \x20 --notify-webhook URL    generic webhook endpoint\n\
         \x20 --ha-webhook URL        Home Assistant webhook endpoint\n\
         \x20 --sms-webhook URL       SMS gateway webhook endpoint\n\
         \x20 --miniprogram-url URL   mini-program backend endpoint\n\
         \x20 --miniprogram-token T   mini-program bearer token\n\
         \x20 --notify-timeout N      notification HTTP timeout seconds (default 5)\n\
         Graceful shutdown: --run-secs / --max-packets limits, or procd stop."
    );
    std::process::exit(2);
}

fn parse_args() -> Args {
    let mut a = Args {
        replay: None,
        replay_rate: 0.0,
        udp: None,
        serial: None,
        baud: 921600,
        stdin: false,
        http: "127.0.0.1:8091".into(),
        no_http: false,
        api_token: String::new(),
        cookie_secure: false,
        storage_dir: "daemon-storage".into(),
        data_dir: "data".into(),
        trace: None,
        snapshot: None,
        run_secs: 0.0,
        max_packets: 0,
        home_mac: "c0:06:c3:11:22:33".into(),
        calibration_secs: 4.0,
        stillness_secs: 5.0,
        window: 30,
        rssi_floor: -72,
        object_confirm_secs: 0.0,
        object_confirm_cv: DEFAULT_CV_THRESHOLD,
        object_confirm_min_intervals: MIN_INTERVALS,
        room_name: String::new(),
        challenge_enabled: None,
        household_id: "H01".into(),
        web_html: None,
        pushplus_token: String::new(),
        serverchan_key: String::new(),
        notify_webhook: String::new(),
        ha_webhook: String::new(),
        sms_webhook: String::new(),
        miniprogram_url: String::new(),
        miniprogram_token: String::new(),
        notify_timeout: None,
    };
    let mut it = std::env::args().skip(1);
    while let Some(arg) = it.next() {
        let mut need = || -> String {
            it.next().unwrap_or_else(|| {
                eprintln!("missing value for {}", arg);
                usage();
            })
        };
        match arg.as_str() {
            "--replay" => a.replay = Some(need()),
            "--replay-rate" => a.replay_rate = need().parse().unwrap_or_else(|_| usage()),
            "--udp" => a.udp = Some(need()),
            "--serial" => a.serial = Some(need()),
            "--baud" => a.baud = need().parse().unwrap_or_else(|_| usage()),
            "--stdin" => a.stdin = true,
            "--http" => a.http = need(),
            "--no-http" => a.no_http = true,
            "--api-token" => a.api_token = need(),
            "--cookie-secure" => a.cookie_secure = true,
            "--storage-dir" => a.storage_dir = need(),
            "--data-dir" => a.data_dir = need(),
            "--trace" => a.trace = Some(need()),
            "--snapshot" => a.snapshot = Some(need()),
            "--run-secs" => a.run_secs = need().parse().unwrap_or_else(|_| usage()),
            "--max-packets" => a.max_packets = need().parse().unwrap_or_else(|_| usage()),
            "--home-mac" => a.home_mac = need(),
            "--calibration-secs" => a.calibration_secs = need().parse().unwrap_or_else(|_| usage()),
            "--stillness-secs" => a.stillness_secs = need().parse().unwrap_or_else(|_| usage()),
            "--window" => a.window = need().parse().unwrap_or_else(|_| usage()),
            "--rssi-floor" => a.rssi_floor = need().parse().unwrap_or_else(|_| usage()),
            "--object-confirm-secs" => a.object_confirm_secs = need().parse().unwrap_or_else(|_| usage()),
            "--object-confirm-cv" => a.object_confirm_cv = need().parse().unwrap_or_else(|_| usage()),
            "--object-confirm-min-intervals" => {
                a.object_confirm_min_intervals = need().parse().unwrap_or_else(|_| usage())
            }
            "--room-name" => a.room_name = need(),
            "--challenge-enabled" => {
                let v: i64 = need().parse().unwrap_or_else(|_| usage());
                a.challenge_enabled = Some(v != 0);
            }
            "--household-id" => a.household_id = need(),
            "--web-html" => a.web_html = Some(need()),
            "--pushplus-token" => a.pushplus_token = need(),
            "--serverchan-key" => a.serverchan_key = need(),
            "--notify-webhook" => a.notify_webhook = need(),
            "--ha-webhook" => a.ha_webhook = need(),
            "--sms-webhook" => a.sms_webhook = need(),
            "--miniprogram-url" => a.miniprogram_url = need(),
            "--miniprogram-token" => a.miniprogram_token = need(),
            "--notify-timeout" => a.notify_timeout = Some(need().parse().unwrap_or_else(|_| usage())),
            "--help" | "-h" => usage(),
            other => {
                eprintln!("unknown argument: {}", other);
                usage();
            }
        }
    }
    if a.replay.is_none() && a.udp.is_none() && a.serial.is_none() && !a.stdin {
        eprintln!("one of --replay / --udp / --serial / --stdin is required");
        usage();
    }
    a
}

fn wall_now() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0)
}

// std does not expose socket buffer sizing; call setsockopt directly.
// SOL_SOCKET/SO_RCVBUF differ by platform family (Linux non-MIPS vs MIPS/macOS).
#[cfg(unix)]
mod sockopt {
    use std::ffi::c_void;
    use std::net::UdpSocket;
    use std::os::unix::io::AsRawFd;

    #[cfg(all(target_os = "linux", not(target_arch = "mips")))]
    const SOL_SOCKET: i32 = 1;
    #[cfg(all(target_os = "linux", not(target_arch = "mips")))]
    const SO_RCVBUF: i32 = 8;
    #[cfg(any(target_os = "macos", all(target_os = "linux", target_arch = "mips")))]
    const SOL_SOCKET: i32 = 0xffff;
    #[cfg(any(target_os = "macos", all(target_os = "linux", target_arch = "mips")))]
    const SO_RCVBUF: i32 = 0x1002;

    extern "C" {
        fn setsockopt(fd: i32, level: i32, name: i32, value: *const c_void, len: u32) -> i32;
        fn getsockopt(fd: i32, level: i32, name: i32, value: *mut c_void, len: *mut u32) -> i32;
    }

    pub fn set_udp_recv_buffer(sock: &UdpSocket, bytes: i32) -> Option<i32> {
        let fd = sock.as_raw_fd();
        let rc =
            unsafe { setsockopt(fd, SOL_SOCKET, SO_RCVBUF, &bytes as *const i32 as *const c_void, 4) };
        if rc != 0 {
            return None;
        }
        let mut actual: i32 = 0;
        let mut len: u32 = 4;
        let rc = unsafe {
            getsockopt(fd, SOL_SOCKET, SO_RCVBUF, &mut actual as *mut i32 as *mut c_void, &mut len)
        };
        if rc != 0 {
            None
        } else {
            Some(actual)
        }
    }
}

struct Pipeline {
    neighbor: NeighborFilter,
    signal: SignalFilter,
    extractor: Extractor,
    detector: Detector,
    security: SecurityGuard,
    packets: u64,
    parse_dropped: u64,
    neighbor_rejected: u64,
    accepted: u64,
    features: u64,
    trace_len: usize,
}

enum Action {
    Burst(u32),
    Challenge,
    Alarm { bpm: f64, confidence: f64 },
    State(&'static str),
    Watch(&'static str),
}

fn drain_control(p: &mut Pipeline, bus: &Arc<Bus>) {
    let (trigger, ack, sec_mode, tuned, is_configured) = {
        let mut s = bus.lock();
        let t = s.control.trigger_inspect;
        let a = s.control.ack_safe;
        let m = s.security_mode.clone();
        let tuned = s.placement.tuned.clone();
        let conf = s.placement.is_configured;
        s.control.trigger_inspect = false;
        s.control.ack_safe = false;
        (t, a, m, tuned, conf)
    };
    if trigger {
        p.detector.force_inspect(wall_now());
    }
    if ack {
        p.detector.acknowledge_safe();
    }
    let parsed_mode = SecurityMode::parse(&sec_mode);
    if p.security.mode != parsed_mode {
        p.security.set_mode(parsed_mode);
    }
    if is_configured {
        p.detector.fall_spike_threshold = tuned.fall_spike_threshold;
        p.detector.inspector.duration = tuned.fall_stillness_seconds;
        p.detector.pet.spr_threshold = tuned.pet_spr_threshold;
        p.signal.hampel_window = tuned.hampel_window_size;
    }
}

fn process_line(
    line: &str,
    p: &mut Pipeline,
    bus: &Arc<Bus>,
    notifier: &Notifier,
    mut serial_out: Option<&mut File>,
) {
    drain_control(p, bus);

    p.packets += 1;
    let pkt = match dsp::parse_line(line) {
        Some(v) => v,
        None => {
            p.parse_dropped += 1;
            return;
        }
    };
    if !p.neighbor.filter_packet(&pkt) {
        p.neighbor_rejected += 1;
        return;
    }
    p.accepted += 1;
    let ts = if pkt.local_timestamp > 0 {
        pkt.local_timestamp as f64 / 1e6
    } else {
        wall_now()
    };
    let clean = p.signal.process(&pkt.amplitudes);
    let f = match p.extractor.push_frame(ts, &clean) {
        Some(f) => f,
        None => return,
    };
    p.features += 1;
    let state = p.detector.update(&f);
    drain_control(p, bus);

    let mut actions: Vec<Action> = Vec::new();
    let mut new_transitions = 0u64;
    for ev in &p.detector.trace[p.trace_len..] {
        match ev {
            TraceEvent::Burst { rate, .. } => actions.push(Action::Burst(*rate)),
            TraceEvent::Challenge { .. } => actions.push(Action::Challenge),
            TraceEvent::StateChange { to, .. } => {
                new_transitions += 1;
                if matches!(*to, "FALL_SUSPECT" | "FALL_CONFIRMED" | "ROUTER_OFFLINE") {
                    actions.push(Action::State(to));
                }
            }
            TraceEvent::FallAlarm { bpm, confidence, .. } => {
                actions.push(Action::Alarm { bpm: *bpm, confidence: *confidence })
            }
            TraceEvent::ObjectWatch { phase, .. } => actions.push(Action::Watch(phase)),
            _ => {}
        }
    }
    p.trace_len = p.detector.trace.len();

    let (room, household, challenge_on) = {
        let s = bus.lock();
        (
            s.placement.room_name.clone(),
            s.household_id.clone(),
            s.placement.enable_gentle_challenge,
        )
    };

    for action in actions {
        match action {
            Action::Burst(rate) => {
                if let Some(f) = serial_out.as_deref_mut() {
                    if serial::write_command(f, &format!("BURST_RATE:{}\n", rate)) {
                        bus.push_event(
                            "ACTUATION",
                            "INFO",
                            &format!("已向射频节点下发提频指令: {} Hz", rate),
                            &format!("{{\"cmd\":\"BURST_RATE:{}\"}}", rate),
                        );
                    } else {
                        bus.push_event("ACTUATION", "WARN", "串口写入失败: BURST_RATE", "{}");
                    }
                }
            }
            Action::Challenge => {
                if challenge_on {
                    if let Some(f) = serial_out.as_deref_mut() {
                        if serial::write_command(f, "CHALLENGE_BEEP:1\n") {
                            bus.push_event(
                                "ACTUATION",
                                "INFO",
                                "已下发温和提示音（挑战应答）",
                                "{\"cmd\":\"CHALLENGE_BEEP:1\"}",
                            );
                        } else {
                            bus.push_event("ACTUATION", "WARN", "串口写入失败: CHALLENGE_BEEP", "{}");
                        }
                    }
                } else {
                    bus.push_event("ACTUATION", "INFO", "温和提示音已按配置关闭，跳过下发", "{}");
                }
            }
            Action::Alarm { bpm, confidence } => {
                let details = "CSI 冲击后持续静止，定向探测捕获异常体征".to_string();
                let inc = {
                    let mut s = bus.lock();
                    let inc = s.incidents.record(
                        &room,
                        "FALL_ALARM",
                        confidence,
                        bpm,
                        &details,
                        &household,
                    );
                    s.events.push(
                        "FALL_ALARM",
                        "CRITICAL",
                        &format!(
                            "检测到疑似跌倒：{}（置信度 {:.0}%，呼吸 {:.0} bpm）",
                            room,
                            confidence * 100.0,
                            bpm
                        ),
                        &format!(
                            "{{\"event_id\":\"{}\",\"room_name\":\"{}\",\"confidence\":{}}}",
                            json::escape(&inc.event_id),
                            json::escape(&room),
                            inc.confidence
                        ),
                    );
                    inc
                };
                bus.cv.notify_all();
                let event_id = inc.event_id.clone();
                notifier.send(Notice::Fall {
                    room: room.clone(),
                    confidence,
                    details,
                    bpm,
                    event_id: event_id.clone(),
                    household: household.clone(),
                });
                if notifier.configured_channels() > 0 {
                    let mut s = bus.lock();
                    s.incidents.mark_pushed(&event_id, true);
                }
            }
            Action::State(to) => {
                let (level, text) = match to {
                    "FALL_CONFIRMED" => ("CRITICAL", "跌倒事件已确诊，开始通知家属"),
                    "FALL_SUSPECT" => ("WARN", "检测到剧烈冲击，启动定向深度排查"),
                    _ => ("WARN", "路由器射频信号中断，进入离线守护"),
                };
                bus.push_event("STATE_CHANGE", level, text, &format!("{{\"to\":\"{}\"}}", to));
            }
            Action::Watch(phase) => {
                if phase != "none" {
                    let level = if matches!(phase, "escalate" | "confirmed") {
                        "CRITICAL"
                    } else {
                        "INFO"
                    };
                    bus.push_event(
                        "OBJECT_CONFIRM",
                        level,
                        &format!("第二阶段目标确认: {}", phase),
                        &format!("{{\"phase\":\"{}\"}}", phase),
                    );
                }
            }
        }
    }

    // Security Guard evaluation
    if let Some(alert) = p.security.evaluate(state.name(), f.timestamp, f.diff_energy) {
        let (room, household) = {
            let s = bus.lock();
            (s.placement.room_name.clone(), s.household_id.clone())
        };
        bus.push_event(
            "SECURITY_INTRUSION",
            alert.level,
            &alert.message,
            &format!(
                "{{\"mode\":\"{}\",\"diff_energy\":{:.4}}}",
                p.security.mode.as_str(),
                f.diff_energy
            ),
        );
        {
            let mut s = bus.lock();
            s.incidents.record(
                &room,
                "SECURITY_INTRUSION",
                0.95,
                0.0,
                &alert.message,
                &household,
            );
        }
        notifier.send(Notice::Generic {
            title: alert.title,
            content: alert.message,
            room,
            ha_event: "security_intrusion".into(),
        });
    }

    let neighbor_snap = if p.packets % 80 == 0 {
        Some(snapshot_neighbor(&p.neighbor))
    } else {
        None
    };
    let calib = p.detector.calibrator.status_summary();

    let mut s = bus.lock();
    s.state = state.name().to_string();
    s.is_pet = state == HumanState::PetActivity;
    s.spr = p.detector.latest_spr;
    s.variance = f.mean_variance;
    s.sample_rate = f.sample_rate_hz;
    s.jitter = f.packet_interval_jitter;
    s.diff_energy = f.diff_energy;
    s.spike_ratio = f.spike_ratio;
    s.base_variance = p.detector.base_variance;
    s.base_diff = p.detector.base_diff;
    s.last_fall_bpm = p.detector.last_fall_bpm;
    s.last_fall_confidence = p.detector.last_fall_confidence;
    s.packets = p.packets;
    s.parse_dropped = p.parse_dropped;
    s.neighbor_rejected = p.neighbor_rejected;
    s.accepted = p.accepted;
    s.features = p.features;
    s.fall_alarms = p.detector.fall_alarms;
    s.transitions += new_transitions;
    if let Some(ns) = neighbor_snap {
        s.neighbor = ns;
    }
    s.calib = calib;
}

fn trace_line(ev: &TraceEvent) -> String {
    match ev {
        TraceEvent::StateChange { ts, from, to, variance, diff_energy } => format!(
            "{{\"type\":\"state_change\",\"ts\":{},\"from\":\"{}\",\"to\":\"{}\",\"variance\":{},\"diff_energy\":{}}}",
            ts, from, to, variance, diff_energy
        ),
        TraceEvent::Impact { ts, spike_ratio, diff_energy } => format!(
            "{{\"type\":\"impact\",\"ts\":{},\"spike_ratio\":{},\"diff_energy\":{}}}",
            ts, spike_ratio, diff_energy
        ),
        TraceEvent::Burst { ts, rate } => {
            format!("{{\"type\":\"burst\",\"ts\":{},\"rate\":{}}}", ts, rate)
        }
        TraceEvent::Challenge { ts } => format!("{{\"type\":\"challenge\",\"ts\":{}}}", ts),
        TraceEvent::Inspection { ts, state } => {
            format!("{{\"type\":\"inspection\",\"ts\":{},\"state\":\"{}\"}}", ts, state)
        }
        TraceEvent::FallAlarm { ts, .. } => format!("{{\"type\":\"fall_alarm\",\"ts\":{}}}", ts),
        TraceEvent::ChallengeIntercepted { ts } => {
            format!("{{\"type\":\"challenge_intercepted\",\"ts\":{}}}", ts)
        }
        TraceEvent::ObjectWatch { ts, phase, peaks, intervals, cv } => {
            let mut s = format!(
                "{{\"type\":\"object_watch\",\"ts\":{},\"phase\":\"{}\"",
                ts, phase
            );
            if let Some(p) = peaks {
                s.push_str(&format!(",\"peaks\":{}", p));
            }
            if let Some(i) = intervals {
                s.push_str(&format!(",\"intervals\":{}", i));
            }
            if let Some(c) = cv {
                s.push_str(&format!(",\"cv\":{}", c));
            }
            s.push('}');
            s
        }
    }
}

fn mark_router_offline(bus: &Arc<Bus>, why: &str) {
    {
        let mut s = bus.lock();
        s.state = "ROUTER_OFFLINE".into();
        s.events.push("ROUTER_OFFLINE", "WARN", why, "{}");
    }
    bus.cv.notify_all();
}

fn run_serial(
    bus: &Arc<Bus>,
    notifier: &Notifier,
    p: &mut Pipeline,
    device: &str,
    baud: u32,
    stop_check: &dyn Fn(&Pipeline) -> bool,
) {
    let mut buf = [0u8; 8192];
    let mut pending: Vec<u8> = Vec::new();
    while !stop_check(p) {
        let path = if device == "auto" {
            match serial::discover() {
                Some(path) => path,
                None => {
                    mark_router_offline(bus, "未发现射频节点串口，等待重新接入");
                    std::thread::sleep(Duration::from_secs(2));
                    continue;
                }
            }
        } else {
            device.to_string()
        };
        let mut file = match serial::open(&path, baud) {
            Ok(f) => f,
            Err(e) => {
                eprintln!("[serial] {}", e);
                mark_router_offline(bus, "射频节点串口打开失败，等待自愈重连");
                std::thread::sleep(Duration::from_secs(2));
                continue;
            }
        };
        eprintln!("[serial] reading {} @ {} baud", path, baud);
        {
            let mut s = bus.lock();
            s.state = "CALIBRATING".into();
            s.events.push(
                "SERIAL_CONNECTED",
                "INFO",
                &format!("射频节点串口已连接: {}", path),
                "{}",
            );
        }
        bus.cv.notify_all();
        pending.clear();

        loop {
            if stop_check(p) {
                return;
            }
            match serial::read_into(&mut file, &mut buf) {
                Ok(0) => break,
                Ok(n) => {
                    pending.extend_from_slice(&buf[..n]);
                    let mut lines: Vec<String> = Vec::new();
                    while let Some(pos) = pending.iter().position(|&b| b == b'\n') {
                        let raw: Vec<u8> = pending.drain(..=pos).collect();
                        let text = String::from_utf8_lossy(&raw);
                        let line = text.trim();
                        if !line.is_empty() {
                            lines.push(line.to_string());
                        }
                    }
                    // CSI lines are ~600 bytes; a megabyte without a newline is line noise.
                    if pending.len() > 1_048_576 {
                        pending.clear();
                    }
                    for line in lines {
                        process_line(&line, p, bus, notifier, Some(&mut file));
                        if stop_check(p) {
                            return;
                        }
                    }
                }
                Err(ref e) if e.kind() == std::io::ErrorKind::WouldBlock => {
                    std::thread::sleep(Duration::from_millis(5));
                }
                Err(ref e) if e.kind() == std::io::ErrorKind::Interrupted => {}
                Err(e) => {
                    eprintln!("[serial] read error: {}", e);
                    break;
                }
            }
        }

        mark_router_offline(bus, "射频节点串口断开，等待自愈重连");
        eprintln!("[serial] disconnected from {}; retrying", path);
        std::thread::sleep(Duration::from_secs(2));
    }
}

fn main() {
    let args = parse_args();
    let calibrator = Calibrator::new(&args.storage_dir);
    let detector = Detector::new(
        args.calibration_secs,
        args.stillness_secs,
        args.object_confirm_secs,
        args.object_confirm_cv,
        args.object_confirm_min_intervals,
        calibrator,
    );
    let home_clean = dsp::clean_mac(&args.home_mac);

    let mut p = Pipeline {
        neighbor: NeighborFilter::new(&args.home_mac, args.rssi_floor),
        signal: SignalFilter::new(),
        extractor: Extractor::new(args.window),
        detector,
        security: SecurityGuard::new(SecurityMode::FallGuard),
        packets: 0,
        parse_dropped: 0,
        neighbor_rejected: 0,
        accepted: 0,
        features: 0,
        trace_len: 0,
    };

    let mut notify_cfg = NotifyConfig::load(&args.storage_dir);
    let mut notify_overridden = false;
    let overrides = [
        (&args.pushplus_token, &mut notify_cfg.pushplus_token),
        (&args.serverchan_key, &mut notify_cfg.serverchan_key),
        (&args.notify_webhook, &mut notify_cfg.webhook_url),
        (&args.ha_webhook, &mut notify_cfg.ha_webhook_url),
        (&args.sms_webhook, &mut notify_cfg.sms_webhook_url),
        (&args.miniprogram_url, &mut notify_cfg.miniprogram_url),
        (&args.miniprogram_token, &mut notify_cfg.miniprogram_token),
    ];
    for (value, slot) in overrides {
        if !value.is_empty() {
            *slot = value.clone();
            notify_overridden = true;
        }
    }
    if let Some(t) = args.notify_timeout {
        notify_cfg.timeout_secs = t.max(1);
        notify_overridden = true;
    }
    if notify_overridden {
        notify_cfg.save(&args.storage_dir);
    }
    let notifier = Notifier::new(notify_cfg, &args.storage_dir);

    let placement = Placement::load(&args.storage_dir, &args.room_name, args.challenge_enabled);
    let mut events = EventLog::new(&args.storage_dir);
    events.push("SYSTEM", "INFO", "SafeHalo Rust 守护服务启动", "{}");
    let incidents = IncidentStore::new(&args.storage_dir);

    let mut devices = serial::DeviceRegistry::new();
    if let Some(ref path) = args.serial {
        devices.register(serial::DeviceInfo {
            id: "csi_probe_0".into(),
            device_type: "serial".into(),
            role: serial::DeviceRole::SensorActuator,
            path: path.clone(),
            baud: args.baud,
            status: "online".into(),
            last_seen: timefmt::now_epoch(),
        });
    } else if let Some(ref addr) = args.udp {
        devices.register(serial::DeviceInfo {
            id: "csi_udp_receiver".into(),
            device_type: "udp".into(),
            role: serial::DeviceRole::Sensor,
            path: addr.clone(),
            baud: 0,
            status: "online".into(),
            last_seen: timefmt::now_epoch(),
        });
    } else {
        devices.register(serial::DeviceInfo {
            id: "stdin_stream".into(),
            device_type: "stdin".into(),
            role: serial::DeviceRole::Sensor,
            path: "stdin".into(),
            baud: 0,
            status: "online".into(),
            last_seen: timefmt::now_epoch(),
        });
    }

    let shared = Shared {
        state: "CALIBRATING".into(),
        is_pet: false,
        spr: 0.0,
        variance: 0.0,
        sample_rate: 0.0,
        jitter: 0.0,
        diff_energy: 0.0,
        spike_ratio: 0.0,
        burst_rate: 30,
        base_variance: p.detector.base_variance,
        base_diff: p.detector.base_diff,
        last_fall_bpm: 0.0,
        last_fall_confidence: 0.0,
        packets: 0,
        parse_dropped: 0,
        neighbor_rejected: 0,
        accepted: 0,
        features: 0,
        fall_alarms: 0,
        transitions: 0,
        home_mac: home_clean,
        neighbor: snapshot_neighbor(&p.neighbor),
        calib: p.detector.calibrator.status_summary(),
        events,
        incidents,
        control: Default::default(),
        placement,
        household_id: args.household_id.clone(),
        security_mode: "FALL_GUARD".into(),
        devices,
        started_at: timefmt::now_epoch(),
    };
    let bus = Arc::new(Bus::new(shared));

    if !args.no_http {
        let auth = Arc::new(AuthManager::new(&args.data_dir));
        let html = match &args.web_html {
            Some(path) => std::fs::read_to_string(path).unwrap_or_else(|e| {
                eprintln!("[http] cannot read {}: {}; using embedded console", path, e);
                http::default_html().to_string()
            }),
            None => http::default_html().to_string(),
        };
        let cfg = HttpConfig {
            token: args.api_token.clone(),
            html,
            cookie_secure: args.cookie_secure,
        };
        if !cfg.token.is_empty() {
            eprintln!("[http] machine token authentication enabled");
        }
        let sh = Arc::clone(&bus);
        let addr = args.http.clone();
        let notifier_http = notifier.clone();
        std::thread::spawn(move || http::serve(addr, sh, auth, notifier_http, cfg));
    }

    let start = Instant::now();
    let stop_check = |p: &Pipeline| -> bool {
        (args.run_secs > 0.0 && start.elapsed().as_secs_f64() >= args.run_secs)
            || (args.max_packets > 0 && p.packets >= args.max_packets)
    };

    if let Some(path) = &args.replay {
        let file = File::open(path).unwrap_or_else(|e| {
            eprintln!("cannot open replay file {}: {}", path, e);
            std::process::exit(1);
        });
        let reader = BufReader::new(file);
        let mut count = 0u64;
        for line in reader.lines() {
            let line = match line {
                Ok(l) => l,
                Err(_) => break,
            };
            if line.trim().is_empty() {
                continue;
            }
            process_line(&line, &mut p, &bus, &notifier, None);
            count += 1;
            if args.replay_rate > 0.0 {
                let target = Duration::from_secs_f64(count as f64 / args.replay_rate);
                let elapsed = start.elapsed();
                if target > elapsed {
                    std::thread::sleep(target - elapsed);
                }
            }
            if stop_check(&p) {
                break;
            }
        }
    } else if let Some(addr) = &args.udp {
        let sock = UdpSocket::bind(addr).unwrap_or_else(|e| {
            eprintln!("cannot bind UDP {}: {}", addr, e);
            std::process::exit(1);
        });
        sock.set_read_timeout(Some(Duration::from_millis(200))).ok();
        match sockopt::set_udp_recv_buffer(&sock, 4 * 1024 * 1024) {
            Some(actual) => eprintln!("[udp] listening on {} (rcvbuf {} KiB)", addr, actual / 1024),
            None => eprintln!("[udp] listening on {}", addr),
        }
        let mut buf = vec![0u8; 65536];
        while !stop_check(&p) {
            match sock.recv_from(&mut buf) {
                Ok((n, _peer)) => {
                    let text = String::from_utf8_lossy(&buf[..n]);
                    for line in text.split('\n') {
                        let line = line.trim();
                        if line.is_empty() {
                            continue;
                        }
                        process_line(line, &mut p, &bus, &notifier, None);
                        if stop_check(&p) {
                            break;
                        }
                    }
                }
                Err(ref e)
                    if e.kind() == std::io::ErrorKind::WouldBlock
                        || e.kind() == std::io::ErrorKind::TimedOut => {}
                Err(_) => break,
            }
        }
    } else if let Some(device) = &args.serial {
        run_serial(&bus, &notifier, &mut p, device, args.baud, &stop_check);
    } else {
        let stdin = std::io::stdin();
        for line in stdin.lock().lines() {
            let line = match line {
                Ok(l) => l,
                Err(_) => break,
            };
            if line.trim().is_empty() {
                continue;
            }
            process_line(&line, &mut p, &bus, &notifier, None);
            if stop_check(&p) {
                break;
            }
        }
    }

    // Final neighbor snapshot (service refreshes every 80 packets; flush at exit).
    {
        let ns = snapshot_neighbor(&p.neighbor);
        let calib = p.detector.calibrator.status_summary();
        let mut s = bus.lock();
        s.neighbor = ns;
        s.calib = calib;
        s.packets = p.packets;
        s.parse_dropped = p.parse_dropped;
        s.neighbor_rejected = p.neighbor_rejected;
        s.accepted = p.accepted;
        s.features = p.features;
    }

    if let Some(path) = &args.trace {
        let mut out = String::new();
        for ev in &p.detector.trace {
            out.push_str(&trace_line(ev));
            out.push('\n');
        }
        if let Err(e) = std::fs::write(path, out) {
            eprintln!("cannot write trace {}: {}", path, e);
        }
    }
    let snapshot = state::snapshot_json(&bus.lock());
    if let Some(path) = &args.snapshot {
        if let Err(e) = std::fs::write(path, snapshot.clone()) {
            eprintln!("cannot write snapshot {}: {}", path, e);
        }
    }

    let elapsed = start.elapsed().as_secs_f64();
    let s = bus.lock();
    println!(
        "packets={} accepted={} features={} final_state={} fall_alarms={} transitions={} elapsed={:.3}s rate={:.0}pkt/s",
        s.packets,
        s.accepted,
        s.features,
        s.state,
        s.fall_alarms,
        s.transitions,
        elapsed,
        if elapsed > 0.0 { s.packets as f64 / elapsed } else { 0.0 }
    );
    let _ = std::io::stdout().flush();
    drop(s);
    if notifier.configured_channels() > 0 && !notifier.flush(Duration::from_secs(30)) {
        eprintln!("[notify] shutdown before the queue drained");
    }
    std::process::exit(0);
}
