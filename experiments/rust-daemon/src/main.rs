// SafeHalo Rust resident-daemon prototype.
// Inputs: --replay FILE | --udp ADDR:PORT | --stdin
// Outputs: HTTP JSON (/api/ha/status, /api/state, /api/neighbor_spectrum),
//          optional --trace JSONL and --snapshot JSON for equivalence testing.

mod calibrator;
mod detector;
mod dsp;
mod inspector;
mod json;
mod object_confirm;
mod pet;

use std::fs::File;
use std::io::{BufRead, BufReader, Read, Write};
use std::net::{TcpListener, TcpStream, UdpSocket};
use std::os::unix::fs::OpenOptionsExt;
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use calibrator::{CalSummary, Calibrator};
use detector::{Detector, HumanState, TraceEvent};
use dsp::{Extractor, NeighborFilter, SignalFilter};
use object_confirm::{DEFAULT_CV_THRESHOLD, MIN_INTERVALS};

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
    storage_dir: String,
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
}

fn usage() -> ! {
    eprintln!(
        "SafeHalo Rust daemon prototype\n\
         Usage: safehalo-daemon [--replay FILE [--replay-rate N]] | [--udp ADDR:PORT] | [--serial DEV [--baud N]] | [--stdin]\n\
         \x20 --http ADDR:PORT        HTTP listen address (default 127.0.0.1:8091)\n\
         \x20 --no-http               disable HTTP server\n\
         \x20 --api-token TOKEN       require ?token= or Bearer auth on all HTTP endpoints (default off)\n\
         \x20 --storage-dir DIR       baseline model directory (default ./daemon-storage)\n\
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
         \x20 --object-confirm-cv N   max regular peak-interval CV, placeholder (default 0.35)\n\
         \x20 --object-confirm-min-intervals N  min intervals for a stage-2 verdict (default 3)\n\
         \x20 --serial DEV            read CSI lines from serial device (e.g. /dev/ttyUSB0)\n\
         \x20 --baud N                serial baud rate (default 115200)\n\
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
        baud: 115200,
        stdin: false,
        http: "127.0.0.1:8091".into(),
        no_http: false,
        api_token: String::new(),
        storage_dir: "daemon-storage".into(),
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
            "--storage-dir" => a.storage_dir = need(),
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

struct NeighborSnap {
    purity: f64,
    health: String,
    advice: String,
    total: u64,
    accepted: u64,
    rejected: u64,
    node_count: usize,
    top: Vec<(String, u64, i32, f64, String)>,
}

fn snapshot_neighbor(nf: &NeighborFilter) -> NeighborSnap {
    let (purity, health, advice, order) = nf.spectrum_analysis();
    NeighborSnap {
        purity,
        health,
        advice,
        total: nf.total_inspected,
        accepted: nf.accepted,
        rejected: nf.rejected,
        node_count: nf.registry.len(),
        top: order
            .iter()
            .map(|&i| {
                let e = &nf.registry[i];
                (e.bssid.clone(), e.packet_count, e.max_rssi, dsp::round1(e.avg_rssi), e.rejection_reason.clone())
            })
            .collect(),
    }
}

struct Shared {
    state: String,
    is_pet: bool,
    variance: f64,
    sample_rate: f64,
    jitter: f64,
    diff_energy: f64,
    spike_ratio: f64,
    burst_rate: u32,
    base_variance: f64,
    base_diff: f64,
    last_fall_bpm: f64,
    last_fall_confidence: f64,
    packets: u64,
    parse_dropped: u64,
    neighbor_rejected: u64,
    accepted: u64,
    features: u64,
    fall_alarms: u64,
    transitions: u64,
    home_mac: String,
    neighbor: NeighborSnap,
    calib: CalSummary,
}

fn neighbor_json(n: &NeighborSnap, home: &str) -> String {
    let top: Vec<String> = n
        .top
        .iter()
        .map(|(bssid, count, max_rssi, avg, reason)| {
            format!(
                "{{\"bssid\":\"{}\",\"packets_intercepted\":{},\"max_rssi_dbm\":{},\"avg_rssi_dbm\":{},\"reason\":\"{}\"}}",
                json::escape(bssid), count, max_rssi, avg, json::escape(reason)
            )
        })
        .collect();
    format!(
        "{{\"channel_purity_percent\":{},\"health_level\":\"{}\",\"home_mac_whitelist\":[\"{}\"],\"total_inspected\":{},\"accepted_home_packets\":{},\"rejected_neighbor_packets\":{},\"neighbor_nodes_detected\":{},\"top_interfering_neighbors\":[{}],\"channel_advice\":\"{}\"}}",
        n.purity,
        json::escape(&n.health),
        json::escape(home),
        n.total,
        n.accepted,
        n.rejected,
        n.node_count,
        top.join(","),
        json::escape(&n.advice)
    )
}

fn calib_json(c: &CalSummary) -> String {
    format!(
        "{{\"base_variance\":{},\"base_diff\":{},\"learning_days\":{},\"quiet_windows_learned\":{},\"maturity_percent\":{},\"boot_contamination_rejected\":{},\"drift_adaptations_count\":{},\"last_updated\":\"{}\"}}",
        c.base_variance,
        c.base_diff,
        c.learning_days,
        c.quiet_windows_learned,
        c.maturity_percent,
        c.boot_contamination_rejected,
        c.drift_adaptations_count,
        json::escape(&c.last_updated)
    )
}

fn ha_json(s: &Shared) -> String {
    let fall = s.state == "FALL_CONFIRMED";
    let presence = s.state != "EMPTY" && s.state != "CALIBRATING";
    let motion = s.state == "MOVING";
    format!(
        "{{\"fall_detected\":{},\"presence\":{},\"motion\":{},\"pet_activity\":{},\"human_state\":\"{}\",\"fused_state\":\"{}\",\"radar_height_m\":0.0,\"respiration_bpm\":0.0,\"confidence\":1.0,\"decision_reason\":\"CSI-only Rust daemon prototype\",\"routine_has_anomaly\":false,\"wake_time\":\"\",\"bed_time\":\"\",\"night_restless_count\":0}}",
        fall, presence, motion, s.is_pet, s.state, s.state
    )
}

fn counters_json(s: &Shared) -> String {
    format!(
        "{{\"packets\":{},\"parse_dropped\":{},\"neighbor_rejected\":{},\"accepted\":{},\"features\":{},\"fall_alarms\":{},\"transitions\":{}}}",
        s.packets, s.parse_dropped, s.neighbor_rejected, s.accepted, s.features, s.fall_alarms, s.transitions
    )
}

fn state_json(s: &Shared) -> String {
    format!(
        "{{\"state\":\"{}\",\"variance\":{},\"sample_rate\":{},\"jitter\":{},\"is_pet\":{},\"diff_energy\":{},\"spike_ratio\":{},\"burst_rate_hz\":{},\"base_variance\":{},\"base_diff\":{},\"last_fall\":{{\"bpm\":{},\"confidence\":{}}},\"counters\":{},\"neighbor_filter\":{},\"auto_calibration\":{}}}",
        s.state,
        s.variance,
        s.sample_rate,
        s.jitter,
        s.is_pet,
        s.diff_energy,
        s.spike_ratio,
        s.burst_rate,
        s.base_variance,
        s.base_diff,
        s.last_fall_bpm,
        s.last_fall_confidence,
        counters_json(s),
        neighbor_json(&s.neighbor, &s.home_mac),
        calib_json(&s.calib)
    )
}

fn snapshot_json(s: &Shared) -> String {
    format!(
        "{{\"final_state\":\"{}\",\"counters\":{},\"base_variance\":{},\"base_diff\":{},\"auto_calibration\":{},\"neighbor_filter\":{}}}",
        s.state,
        counters_json(s),
        s.base_variance,
        s.base_diff,
        calib_json(&s.calib),
        neighbor_json(&s.neighbor, &s.home_mac)
    )
}

fn handle_conn(mut st: TcpStream, shared: &Arc<Mutex<Shared>>, token: &str) {
    st.set_read_timeout(Some(Duration::from_millis(1000))).ok();
    let mut data: Vec<u8> = Vec::new();
    let mut buf = [0u8; 4096];
    loop {
        match st.read(&mut buf) {
            Ok(0) => break,
            Ok(n) => {
                data.extend_from_slice(&buf[..n]);
                if data.windows(4).any(|w| w == b"\r\n\r\n") || data.len() > 16384 {
                    break;
                }
            }
            Err(_) => break,
        }
    }
    let req = String::from_utf8_lossy(&data);
    let req_line = req.lines().next().unwrap_or("");
    let path_full = req_line.split_whitespace().nth(1).unwrap_or("/");
    let path = path_full.split('?').next().unwrap_or("/");
    let s = shared.lock().unwrap();
    if !token.is_empty() {
        let query_ok = path_full
            .split('?')
            .nth(1)
            .map(|q| {
                q.split('&').any(|kv| {
                    let mut it = kv.splitn(2, '=');
                    it.next() == Some("token") && it.next() == Some(token)
                })
            })
            .unwrap_or(false);
        let bearer_ok = req.lines().any(|l| {
            if !l.to_ascii_lowercase().starts_with("authorization:") {
                return false;
            }
            let value = l.splitn(2, ':').nth(1).unwrap_or("").trim();
            value == format!("Bearer {}", token) || value == token
        });
        if !query_ok && !bearer_ok {
            let body = "{\"error\":\"unauthorized\"}";
            let resp = format!(
                "HTTP/1.1 401 Unauthorized\r\nContent-Type: application/json; charset=utf-8\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
                body.len(),
                body
            );
            let _ = st.write_all(resp.as_bytes());
            return;
        }
    }
    let (status, body) = match path {
        "/api/ha/status" => (200, ha_json(&s)),
        "/api/state" => (200, state_json(&s)),
        "/api/neighbor_spectrum" => (200, neighbor_json(&s.neighbor, &s.home_mac)),
        "/" => (200, "SafeHalo Rust daemon prototype OK\n".to_string()),
        _ => (404, "{\"error\":\"not found\"}".to_string()),
    };
    let reason = if status == 200 { "OK" } else { "Not Found" };
    let resp = format!(
        "HTTP/1.1 {} {}\r\nContent-Type: application/json; charset=utf-8\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
        status, reason, body.len(), body
    );
    let _ = st.write_all(resp.as_bytes());
}

fn serve(addr: String, shared: Arc<Mutex<Shared>>, token: String) {
    let listener = match TcpListener::bind(&addr) {
        Ok(l) => l,
        Err(e) => {
            eprintln!("[http] bind {} failed: {}", addr, e);
            return;
        }
    };
    eprintln!("[http] listening on http://{}/", addr);
    for stream in listener.incoming() {
        if let Ok(st) = stream {
            handle_conn(st, &shared, &token);
        }
    }
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
        let rc = unsafe {
            setsockopt(fd, SOL_SOCKET, SO_RCVBUF, &bytes as *const i32 as *const c_void, 4)
        };
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
    packets: u64,
    parse_dropped: u64,
    neighbor_rejected: u64,
    accepted: u64,
    features: u64,
    trace_len: usize,
}

fn process_line(line: &str, p: &mut Pipeline, shared: &Arc<Mutex<Shared>>) {
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

    let mut burst: Option<u32> = None;
    let mut new_transitions = 0u64;
    for ev in &p.detector.trace[p.trace_len..] {
        match ev {
            TraceEvent::Burst { rate, .. } => burst = Some(*rate),
            TraceEvent::StateChange { .. } => new_transitions += 1,
            _ => {}
        }
    }
    p.trace_len = p.detector.trace.len();

    let neighbor_snap = if p.packets % 80 == 0 {
        Some(snapshot_neighbor(&p.neighbor))
    } else {
        None
    };
    let calib = p.detector.calibrator.status_summary();

    let mut s = shared.lock().unwrap();
    s.state = state.name().to_string();
    s.is_pet = state == HumanState::PetActivity;
    s.variance = f.mean_variance;
    s.sample_rate = f.sample_rate_hz;
    s.jitter = f.packet_interval_jitter;
    s.diff_energy = f.diff_energy;
    s.spike_ratio = f.spike_ratio;
    if let Some(r) = burst {
        s.burst_rate = r;
    }
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
        TraceEvent::FallAlarm { ts } => format!("{{\"type\":\"fall_alarm\",\"ts\":{}}}", ts),
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

#[cfg(target_os = "macos")]
const O_NONBLOCK: i32 = 0x0004;
#[cfg(target_os = "linux")]
const O_NONBLOCK: i32 = 0x0800;

fn open_serial(path: &str, baud: u32) -> File {
    let flag = if cfg!(target_os = "macos") { "-f" } else { "-F" };
    match std::process::Command::new("stty")
        .arg(flag)
        .arg(path)
        .arg(baud.to_string())
        .args(["raw", "-echo", "cs8", "-parenb", "-cstopb"])
        .status()
    {
        Ok(s) if s.success() => {}
        Ok(s) => eprintln!("[serial] stty exited with {}; keeping current port settings", s),
        Err(e) => eprintln!("[serial] stty unavailable ({}); keeping current port settings", e),
    }
    // O_NONBLOCK so open() does not hang waiting for modem control lines.
    std::fs::OpenOptions::new()
        .read(true)
        .custom_flags(O_NONBLOCK)
        .open(path)
        .unwrap_or_else(|e| {
            eprintln!("cannot open serial {}: {}", path, e);
            std::process::exit(1);
        })
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
        packets: 0,
        parse_dropped: 0,
        neighbor_rejected: 0,
        accepted: 0,
        features: 0,
        trace_len: 0,
    };

    let shared = Arc::new(Mutex::new(Shared {
        state: "CALIBRATING".into(),
        is_pet: false,
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
    }));

    if !args.no_http {
        let sh = Arc::clone(&shared);
        let addr = args.http.clone();
        let token = args.api_token.clone();
        if !token.is_empty() {
            eprintln!("[http] token authentication enabled");
        }
        std::thread::spawn(move || serve(addr, sh, token));
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
            process_line(&line, &mut p, &shared);
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
                        process_line(line, &mut p, &shared);
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
    } else if let Some(path) = &args.serial {
        let mut file = open_serial(path, args.baud);
        eprintln!("[serial] reading {} @ {} baud", path, args.baud);
        let mut buf = [0u8; 8192];
        let mut pending: Vec<u8> = Vec::new();
        while !stop_check(&p) {
            let n = match file.read(&mut buf) {
                Ok(0) => break,
                Ok(n) => n,
                Err(ref e) if e.kind() == std::io::ErrorKind::WouldBlock => {
                    std::thread::sleep(Duration::from_millis(5));
                    continue;
                }
                Err(ref e) if e.kind() == std::io::ErrorKind::Interrupted => continue,
                Err(e) => {
                    eprintln!("[serial] read error: {}", e);
                    break;
                }
            };
            pending.extend_from_slice(&buf[..n]);
            while let Some(pos) = pending.iter().position(|&b| b == b'\n') {
                let raw_line: Vec<u8> = pending.drain(..=pos).collect();
                let text = String::from_utf8_lossy(&raw_line);
                let line = text.trim();
                if line.is_empty() {
                    continue;
                }
                process_line(line, &mut p, &shared);
                if stop_check(&p) {
                    break;
                }
            }
            // CSI lines are ~600 bytes; a megabyte without a newline is line noise.
            if pending.len() > 1_048_576 {
                pending.clear();
            }
        }
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
            process_line(&line, &mut p, &shared);
            if stop_check(&p) {
                break;
            }
        }
    }

    // Final neighbor snapshot (service refreshes every 80 packets; flush at exit).
    {
        let ns = snapshot_neighbor(&p.neighbor);
        let calib = p.detector.calibrator.status_summary();
        let mut s = shared.lock().unwrap();
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
    let snapshot = snapshot_json(&shared.lock().unwrap());
    if let Some(path) = &args.snapshot {
        if let Err(e) = std::fs::write(path, snapshot.clone()) {
            eprintln!("cannot write snapshot {}: {}", path, e);
        }
    }

    let elapsed = start.elapsed().as_secs_f64();
    let s = shared.lock().unwrap();
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
    std::process::exit(0);
}