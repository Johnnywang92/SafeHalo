// DSP chain ported from src/safehalo/{csi_parser,neighbor_filter,signal_filter,feature_extractor}.py
use std::collections::VecDeque;

#[derive(Default, Clone)]
pub struct Packet {
    pub mac: String,
    pub rssi: i32,
    pub local_timestamp: i64,
    pub amplitudes: Vec<f64>,
    pub phases: Vec<f64>,
}

fn is_digits(s: &str) -> bool {
    !s.is_empty() && s.chars().all(|c| c.is_ascii_digit())
}

pub fn parse_line(line: &str) -> Option<Packet> {
    let line = line.trim();
    if line.is_empty() || !line.contains("CSI_DATA") {
        return None;
    }
    let start = line.find('[')?;
    let end = line[start..].find(']')? + start;
    let data_str = &line[start + 1..end];
    let mut raw: Vec<i32> = Vec::new();
    for v in data_str.split(',') {
        let t = v.trim();
        if t.is_empty() {
            continue;
        }
        raw.push(t.parse().ok()?);
    }
    if raw.len() < 4 {
        return None;
    }
    let prefix = line[..start].trim();
    let parts: Vec<&str> = prefix.split(',').map(|p| p.trim()).filter(|p| !p.is_empty()).collect();
    let mut p = Packet::default();
    if parts.len() >= 5 {
        if parts.len() > 3 {
            p.mac = parts[3].to_string();
        }
        if parts.len() > 4 && is_digits(parts[4].trim_start_matches('-')) {
            p.rssi = parts[4].parse().unwrap_or(0);
        }
    }
    for (idx, part) in parts.iter().enumerate() {
        if idx == 19 && is_digits(part) {
            p.local_timestamp = part.parse().unwrap_or(0);
        }
    }
    let n = raw.len() / 2;
    p.amplitudes = vec![0.0; n];
    p.phases = vec![0.0; n];
    for i in 0..n {
        let imag = raw[2 * i] as f64;
        let real = raw[2 * i + 1] as f64;
        p.amplitudes[i] = (real * real + imag * imag).sqrt();
        p.phases[i] = imag.atan2(real);
    }
    Some(p)
}

pub fn clean_mac(mac: &str) -> String {
    if mac.is_empty() {
        return String::new();
    }
    let raw = mac.trim().to_lowercase().replace('-', ":");
    let parts: Vec<String> = raw
        .split(':')
        .filter(|p| !p.is_empty())
        .map(|p| format!("{:0>2}", p))
        .collect();
    if parts.len() == 6 {
        parts.join(":")
    } else {
        raw
    }
}

fn is_random_mac(mac: &str) -> bool {
    if mac.len() < 2 {
        return false;
    }
    match u8::from_str_radix(&mac[..2], 16) {
        Ok(b) => b & 0x02 != 0,
        Err(_) => false,
    }
}

pub struct NeighborEntry {
    pub bssid: String,
    pub packet_count: u64,
    pub max_rssi: i32,
    pub avg_rssi: f64,
    pub rejection_reason: String,
}

pub struct NeighborFilter {
    pub home: Vec<String>,
    pub rssi_floor: i32,
    pub tilt_thresh: f64,
    pub registry: Vec<NeighborEntry>,
    pub total_inspected: u64,
    pub rejected: u64,
    pub accepted: u64,
    pub weak_dropped: u64,
    pub random_dropped: u64,
}

impl NeighborFilter {
    pub fn new(home: &str, rssi_floor: i32) -> Self {
        NeighborFilter {
            home: vec![clean_mac(home)],
            rssi_floor,
            tilt_thresh: 0.45,
            registry: Vec::new(),
            total_inspected: 0,
            rejected: 0,
            accepted: 0,
            weak_dropped: 0,
            random_dropped: 0,
        }
    }
    fn record(&mut self, mac: &str, rssi: i32, reason: String) {
        if let Some(e) = self.registry.iter_mut().find(|e| e.bssid == mac) {
            e.packet_count += 1;
            e.max_rssi = e.max_rssi.max(rssi);
            e.avg_rssi = e.avg_rssi * 0.9 + rssi as f64 * 0.1;
            e.rejection_reason = reason;
        } else {
            self.registry.push(NeighborEntry {
                bssid: mac.to_string(),
                packet_count: 1,
                max_rssi: rssi,
                avg_rssi: rssi as f64,
                rejection_reason: reason,
            });
        }
    }
    fn spectral_tilt(&self, amps: &[f64]) -> f64 {
        if amps.len() < 16 {
            return 1.0;
        }
        let half = amps.len() / 2;
        let low: f64 = amps[..half].iter().map(|a| a * a).sum();
        let high: f64 = amps[half..].iter().map(|a| a * a).sum();
        if low <= 1e-6 {
            return 1.0;
        }
        high / low
    }
    pub fn filter_packet(&mut self, p: &Packet) -> bool {
        self.total_inspected += 1;
        let mac = clean_mac(&p.mac);
        let rssi = p.rssi;
        if is_random_mac(&mac) {
            self.random_dropped += 1;
            self.rejected += 1;
            self.record(&mac, rssi, "RANDOM_PROBE_MAC".into());
            return false;
        }
        if !self.home.is_empty() && !self.home.contains(&mac) {
            self.rejected += 1;
            self.record(&mac, rssi, "NOT_IN_HOME_WHITELIST".into());
            return false;
        }
        if rssi < self.rssi_floor {
            self.weak_dropped += 1;
            self.rejected += 1;
            self.record(&mac, rssi, format!("WEAK_RSSI_{}dBm", rssi));
            return false;
        }
        let tilt = self.spectral_tilt(&p.amplitudes);
        if tilt < self.tilt_thresh && rssi < self.rssi_floor + 8 {
            self.rejected += 1;
            self.record(&mac, rssi, "PARTY_WALL_SPECTRAL_FADING".into());
            return false;
        }
        self.accepted += 1;
        true
    }
    pub fn spectrum_analysis(&self) -> (f64, String, String, Vec<usize>) {
        let total = self.total_inspected.max(1);
        let purity = (self.accepted as f64 / total as f64) * 100.0;
        let (health, advice) = if purity < 70.0 {
            ("CONGESTED", "检测到严重邻居同频报文穿透！建议将路由器 2.4G 信道由自动改为手动 1/6/11 互斥信道，或切换至 5G 频段。")
        } else if purity < 88.0 {
            ("MODERATE", "存在轻微邻居穿墙信号，已被算法实时隔离过滤，感知引擎运行正常。")
        } else {
            ("EXCELLENT", "当前无线信道纯净，无显著同频邻居串扰。")
        };
        let mut order: Vec<usize> = (0..self.registry.len()).collect();
        order.sort_by(|a, b| self.registry[*b].packet_count.cmp(&self.registry[*a].packet_count));
        order.truncate(5);
        (round1(purity), health.into(), advice.into(), order)
    }
}

pub fn round1(x: f64) -> f64 {
    format!("{:.1}", x).parse().unwrap()
}
pub fn round_n(x: f64, n: usize) -> f64 {
    format!("{:.*}", n, x).parse().unwrap()
}

fn hampel(series: &[f64], hampel_threshold: f64) -> f64 {
    let n = series.len();
    if n < 3 {
        return *series.last().unwrap();
    }
    let target = series[n - 1];
    let mut sorted = series.to_vec();
    sorted.sort_by(|a, b| a.partial_cmp(b).unwrap());
    let median = sorted[n / 2];
    let mut devs: Vec<f64> = series.iter().map(|x| (x - median).abs()).collect();
    devs.sort_by(|a, b| a.partial_cmp(b).unwrap());
    let mad = devs[n / 2];
    let threshold = hampel_threshold * 1.4826 * mad;
    if (target - median).abs() > threshold && threshold > 1e-6 {
        median
    } else {
        target
    }
}

pub struct SignalFilter {
    guard: usize,
    pub hampel_window: usize,
    hampel_threshold: f64,
    history: Vec<Vec<f64>>,
}

impl SignalFilter {
    pub fn new() -> Self {
        SignalFilter { guard: 6, hampel_window: 5, hampel_threshold: 3.0, history: Vec::new() }
    }
    fn valid_indices(&self, total: usize) -> Vec<usize> {
        let center = total / 2;
        (0..total)
            .filter(|&i| i >= self.guard && i < total - self.guard && i != 0 && i != center)
            .collect()
    }
    pub fn process(&mut self, raw: &[f64]) -> Vec<f64> {
        if raw.is_empty() {
            return Vec::new();
        }
        let extracted: Vec<f64> = self.valid_indices(raw.len()).iter().map(|&i| raw[i]).collect();
        let energy: f64 = extracted.iter().map(|a| a * a).sum();
        let normed: Vec<f64> = if energy <= 1e-9 {
            vec![0.0; extracted.len()]
        } else {
            let f = energy.sqrt();
            extracted.iter().map(|a| a / f).collect()
        };
        let m = normed.len();
        if self.history.len() != m {
            self.history = normed.iter().map(|v| vec![*v]).collect();
            return normed;
        }
        let mut out = Vec::with_capacity(m);
        for i in 0..m {
            let val = normed[i];
            let buf = &mut self.history[i];
            buf.push(val);
            while buf.len() > self.hampel_window {
                buf.remove(0);
            }
            out.push(hampel(buf, self.hampel_threshold));
        }
        out
    }
}

#[derive(Clone)]
pub struct Features {
    pub timestamp: f64,
    pub mean_variance: f64,
    pub max_variance: f64,
    pub diff_energy: f64,
    pub spike_ratio: f64,
    pub respiration_energy: f64,
    pub sample_rate_hz: f64,
    pub packet_interval_jitter: f64,
    pub subcarrier_variances: Vec<f64>,
    pub current_amplitudes: Vec<f64>,
}

pub struct Extractor {
    window: usize,
    frames: VecDeque<Vec<f64>>,
    times: VecDeque<f64>,
    diffs: VecDeque<f64>,
}

impl Extractor {
    pub fn new(window: usize) -> Self {
        Extractor { window, frames: VecDeque::new(), times: VecDeque::new(), diffs: VecDeque::new() }
    }
    pub fn push_frame(&mut self, ts: f64, amps: &[f64]) -> Option<Features> {
        if amps.is_empty() {
            return None;
        }
        let diff = if let Some(prev) = self.frames.back() {
            let s: f64 = amps.iter().zip(prev.iter()).map(|(a, b)| (a - b).abs()).sum();
            s / amps.len() as f64
        } else {
            0.0
        };
        self.frames.push_back(amps.to_vec());
        self.times.push_back(ts);
        self.diffs.push_back(diff);
        while self.frames.len() > self.window {
            self.frames.pop_front();
            self.times.pop_front();
            self.diffs.pop_front();
        }
        if self.frames.len() < 5.max(self.window / 3) {
            return None;
        }
        let nf = self.frames.len();
        let ns = amps.len();
        let mut variances = Vec::with_capacity(ns);
        for sc in 0..ns {
            let mut mean = 0.0;
            for t in 0..nf {
                mean += self.frames[t][sc];
            }
            mean /= nf as f64;
            let mut var = 0.0;
            for t in 0..nf {
                let x = self.frames[t][sc] - mean;
                var += x * x;
            }
            var /= nf as f64;
            variances.push(var);
        }
        let mean_variance: f64 = variances.iter().sum::<f64>() / variances.len() as f64;
        let max_variance = variances.iter().cloned().fold(f64::NEG_INFINITY, f64::max);
        let diff_energy: f64 = self.diffs.iter().sum::<f64>() / self.diffs.len() as f64;
        let dlen = self.diffs.len();
        let short_start = if dlen >= 5 { dlen - 5 } else { 0 };
        let recent_max = self.diffs.range(short_start..).cloned().fold(f64::NEG_INFINITY, f64::max);
        let mean_diff = if diff_energy > 1e-6 { diff_energy } else { 1e-6 };
        let spike_ratio = recent_max / mean_diff;
        let mut intervals: Vec<f64> = Vec::new();
        for i in 1..self.times.len() {
            let dt = self.times[i] - self.times[i - 1];
            if dt > 0.0 {
                intervals.push(dt);
            }
        }
        let (sample_rate, jitter_ms) = if !intervals.is_empty() {
            let avg: f64 = intervals.iter().sum::<f64>() / intervals.len() as f64;
            let sr = if avg > 1e-4 { 1.0 / avg } else { 0.0 };
            let dv: f64 = intervals.iter().map(|dt| (dt - avg) * (dt - avg)).sum::<f64>() / intervals.len() as f64;
            (sr, dv.sqrt() * 1000.0)
        } else {
            (0.0, 0.0)
        };
        let respiration_energy = mean_variance / (diff_energy + 1e-5);
        Some(Features {
            timestamp: ts,
            mean_variance,
            max_variance,
            diff_energy,
            spike_ratio,
            respiration_energy,
            sample_rate_hz: sample_rate,
            packet_interval_jitter: jitter_ms,
            subcarrier_variances: variances,
            current_amplitudes: amps.to_vec(),
        })
    }
}