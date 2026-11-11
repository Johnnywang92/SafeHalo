// Shared runtime state + HTTP JSON views. The /api/state and snapshot formats
// are kept byte-identical to the pre-service daemon so equivalence tests and
// existing consumers keep working.

use std::fs;
use std::path::PathBuf;
use std::sync::{Condvar, Mutex, MutexGuard};

use crate::calibrator::CalSummary;
use crate::dsp::{self, NeighborFilter};
use crate::events::{EventLog, EventRecord};
use crate::incidents::IncidentStore;
use crate::json;

pub struct NeighborSnap {
    pub purity: f64,
    pub health: String,
    pub advice: String,
    pub total: u64,
    pub accepted: u64,
    pub rejected: u64,
    pub node_count: usize,
    pub top: Vec<(String, u64, i32, f64, String)>,
}

pub fn snapshot_neighbor(nf: &NeighborFilter) -> NeighborSnap {
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
                (
                    e.bssid.clone(),
                    e.packet_count,
                    e.max_rssi,
                    dsp::round1(e.avg_rssi),
                    e.rejection_reason.clone(),
                )
            })
            .collect(),
    }
}

#[derive(Clone)]
pub struct Placement {
    pub room_type: String,
    pub room_name: String,
    pub mount_height: String,
    pub room_area_sqm: f64,
    pub has_pets: bool,
    pub has_water_splash: bool,
    pub care_nickname: String,
    pub living_arrangement: String,
    pub mobility_pace: String,
    pub wake_habit: String,
    pub enable_gentle_challenge: bool,
    pub is_configured: bool,
}

impl Default for Placement {
    fn default() -> Self {
        Placement {
            room_type: "LIVING_ROOM".into(),
            room_name: "客厅".into(),
            mount_height: "MID_DESK".into(),
            room_area_sqm: 25.0,
            has_pets: false,
            has_water_splash: false,
            care_nickname: "家里长辈".into(),
            living_arrangement: "SOLO".into(),
            mobility_pace: "BRISK".into(),
            wake_habit: "REGULAR".into(),
            enable_gentle_challenge: true,
            is_configured: false,
        }
    }
}

impl Placement {
    pub fn load(dir: &str, room_override: &str, challenge_override: Option<bool>) -> Placement {
        let mut p = Placement::default();
        let path = PathBuf::from(dir).join("placement_config.json");
        if let Ok(text) = fs::read_to_string(path) {
            if let Some(v) = json::parse(&text) {
                let gs = |k: &str, d: &str| {
                    v.get(k)
                        .and_then(|x| x.as_str())
                        .map(|s| s.to_string())
                        .unwrap_or_else(|| d.to_string())
                };
                let gb = |k: &str, d: bool| v.get(k).and_then(|x| x.as_bool()).unwrap_or(d);
                p.room_type = gs("room_type", &p.room_type);
                p.room_name = gs("room_name", &p.room_name);
                p.mount_height = gs("mount_height", &p.mount_height);
                p.room_area_sqm = v.get("room_area_sqm").and_then(|x| x.as_f64()).unwrap_or(p.room_area_sqm);
                p.has_pets = gb("has_pets", p.has_pets);
                p.has_water_splash = gb("has_water_splash", p.has_water_splash);
                p.care_nickname = gs("care_nickname", &p.care_nickname);
                p.living_arrangement = gs("living_arrangement", &p.living_arrangement);
                p.mobility_pace = gs("mobility_pace", &p.mobility_pace);
                p.wake_habit = gs("wake_habit", &p.wake_habit);
                p.enable_gentle_challenge = gb("enable_gentle_challenge", p.enable_gentle_challenge);
                p.is_configured = gb("is_configured", p.is_configured);
            }
        }
        if !room_override.is_empty() {
            p.room_name = room_override.to_string();
        }
        if let Some(c) = challenge_override {
            p.enable_gentle_challenge = c;
        }
        p
    }

    pub fn save(&self, dir: &str) {
        let _ = fs::create_dir_all(dir);
        let text = format!(
            "{{\n  \"room_type\": \"{}\",\n  \"room_name\": \"{}\",\n  \"mount_height\": \"{}\",\n  \"room_area_sqm\": {},\n  \"has_pets\": {},\n  \"has_water_splash\": {},\n  \"care_nickname\": \"{}\",\n  \"living_arrangement\": \"{}\",\n  \"mobility_pace\": \"{}\",\n  \"wake_habit\": \"{}\",\n  \"enable_gentle_challenge\": {},\n  \"is_configured\": {}\n}}\n",
            json::escape(&self.room_type),
            json::escape(&self.room_name),
            json::escape(&self.mount_height),
            self.room_area_sqm,
            self.has_pets,
            self.has_water_splash,
            json::escape(&self.care_nickname),
            json::escape(&self.living_arrangement),
            json::escape(&self.mobility_pace),
            json::escape(&self.wake_habit),
            self.enable_gentle_challenge,
            self.is_configured
        );
        let _ = fs::write(PathBuf::from(dir).join("placement_config.json"), text);
    }

    pub fn to_json(&self) -> String {
        format!(
            "{{\"room_type\":\"{}\",\"room_name\":\"{}\",\"mount_height\":\"{}\",\"room_area_sqm\":{},\"has_pets\":{},\"has_water_splash\":{},\"care_nickname\":\"{}\",\"living_arrangement\":\"{}\",\"mobility_pace\":\"{}\",\"wake_habit\":\"{}\",\"enable_gentle_challenge\":{},\"is_configured\":{},\"updated_at\":\"\"}}",
            json::escape(&self.room_type),
            json::escape(&self.room_name),
            json::escape(&self.mount_height),
            self.room_area_sqm,
            self.has_pets,
            self.has_water_splash,
            json::escape(&self.care_nickname),
            json::escape(&self.living_arrangement),
            json::escape(&self.mobility_pace),
            json::escape(&self.wake_habit),
            self.enable_gentle_challenge,
            self.is_configured
        )
    }

    pub fn tuned_json(&self) -> String {
        let spike = if self.mobility_pace == "STEADY_SLOW" { 1.8 } else { 2.0 };
        let still = if self.mobility_pace == "STEADY_SLOW" { 6.0 } else { 5.0 };
        let desc = format!(
            "按【{} / {}】优化：冲击阈值 {:.1}，静止确诊 {:.0}s，宠物门限 0.35，{}水流阻尼",
            self.room_type,
            self.mount_height,
            spike,
            still,
            if self.has_water_splash { "启用" } else { "关闭" }
        );
        let friendly = format!(
            "已为【{}】定制【{}】守护方案：{}",
            self.care_nickname, self.room_name, desc
        );
        format!(
            "{{\"fall_spike_threshold\":{},\"fall_stillness_seconds\":{},\"pet_spr_threshold\":0.35,\"radar_floor_height_thresh\":0.45,\"hampel_window_size\":5,\"water_flow_damping\":{},\"metal_multipath_damping\":false,\"description\":\"{}\",\"friendly_summary\":\"{}\"}}",
            spike,
            still,
            self.has_water_splash,
            json::escape(&desc),
            json::escape(&friendly)
        )
    }
}

#[derive(Default)]
pub struct Control {
    pub trigger_inspect: bool,
    pub ack_safe: bool,
}

pub struct Shared {
    pub state: String,
    pub is_pet: bool,
    pub variance: f64,
    pub sample_rate: f64,
    pub jitter: f64,
    pub diff_energy: f64,
    pub spike_ratio: f64,
    pub burst_rate: u32,
    pub base_variance: f64,
    pub base_diff: f64,
    pub last_fall_bpm: f64,
    pub last_fall_confidence: f64,
    pub packets: u64,
    pub parse_dropped: u64,
    pub neighbor_rejected: u64,
    pub accepted: u64,
    pub features: u64,
    pub fall_alarms: u64,
    pub transitions: u64,
    pub home_mac: String,
    pub neighbor: NeighborSnap,
    pub calib: CalSummary,
    pub events: EventLog,
    pub incidents: IncidentStore,
    pub control: Control,
    pub placement: Placement,
    pub household_id: String,
    pub security_mode: String,
    pub started_at: f64,
}

pub struct Bus {
    pub state: Mutex<Shared>,
    pub cv: Condvar,
}

impl Bus {
    pub fn new(shared: Shared) -> Bus {
        Bus {
            state: Mutex::new(shared),
            cv: Condvar::new(),
        }
    }

    pub fn lock(&self) -> MutexGuard<'_, Shared> {
        self.state.lock().unwrap_or_else(|e| e.into_inner())
    }

    /// Append an event and wake SSE clients. Never call while holding the lock.
    pub fn push_event(&self, event_type: &str, level: &str, message: &str, data: &str) {
        {
            let mut s = self.lock();
            s.events.push(event_type, level, message, data);
        }
        self.cv.notify_all();
    }

    pub fn push_event_locked(&self, s: &mut Shared, event_type: &str, level: &str, message: &str, data: &str) {
        s.events.push(event_type, level, message, data);
        self.cv.notify_all();
    }
}

pub fn neighbor_json(n: &NeighborSnap, home: &str) -> String {
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

pub fn calib_json(c: &CalSummary) -> String {
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

pub fn ha_json(s: &Shared) -> String {
    let fall = s.state == "FALL_CONFIRMED";
    let presence = s.state != "EMPTY" && s.state != "CALIBRATING";
    let motion = s.state == "MOVING";
    format!(
        "{{\"fall_detected\":{},\"presence\":{},\"motion\":{},\"pet_activity\":{},\"human_state\":\"{}\",\"fused_state\":\"{}\",\"radar_height_m\":0.0,\"respiration_bpm\":0.0,\"confidence\":1.0,\"decision_reason\":\"CSI-only Rust daemon\",\"routine_has_anomaly\":false,\"wake_time\":\"\",\"bed_time\":\"\",\"night_restless_count\":0}}",
        fall, presence, motion, s.is_pet, s.state, s.state
    )
}

pub fn counters_json(s: &Shared) -> String {
    format!(
        "{{\"packets\":{},\"parse_dropped\":{},\"neighbor_rejected\":{},\"accepted\":{},\"features\":{},\"fall_alarms\":{},\"transitions\":{}}}",
        s.packets, s.parse_dropped, s.neighbor_rejected, s.accepted, s.features, s.fall_alarms, s.transitions
    )
}

pub fn state_json(s: &Shared) -> String {
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

pub fn snapshot_json(s: &Shared) -> String {
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

pub fn recent_events_json(s: &Shared, limit: usize) -> String {
    let items: Vec<String> = s
        .events
        .recent(limit, None)
        .iter()
        .map(|e| e.brief_json())
        .collect();
    format!("[{}]", items.join(","))
}

pub fn routine_json() -> String {
    let hours = vec!["0"; 24].join(",");
    "{\"wake_time\":\"尚未起床\",\"bed_time\":\"待定\",\"typical_wake\":\"07:30\",\"typical_bed\":\"22:30\",\"night_restless_count\":0,\"total_active_minutes\":0.0,\"hourly_activity\":[".to_string()
        + &hours
        + "],\"active_anomalies\":[],\"has_anomaly\":false,\"history\":[]}"
}

pub fn multilink_json(s: &Shared) -> String {
    format!(
        "{{\"active_room\":\"{}\",\"participating_links\":1,\"fused_energy\":0.0,\"links\":{{}}}}",
        json::escape(&s.placement.room_name)
    )
}

pub fn placement_json(p: &Placement) -> String {
    format!(
        "{{\"config\":{},\"tuned_parameters\":{},\"is_configured\":{}}}",
        p.to_json(),
        p.tuned_json(),
        p.is_configured
    )
}

pub fn status_json(s: &Shared) -> String {
    format!(
        "{{\"state\":\"{}\",\"sample_rate\":{},\"variance\":{},\"jitter\":{},\"spr\":0.0,\"is_pet\":{},\"security_mode\":\"{}\",\"radar_online\":false,\"radar_protocol\":\"None\",\"radar_z\":0.0,\"radar_bpm\":0.0,\"fused_state\":\"{}\",\"fused_confidence\":1.0,\"fused_reason\":\"CSI-only Rust daemon\",\"placement\":{},\"routine\":{},\"neighbor_filter\":{},\"multilink\":{},\"auto_calibration\":{},\"pilot\":{},\"recent_events\":{},\"counters\":{},\"last_fall\":{{\"bpm\":{},\"confidence\":{}}}}}",
        s.state,
        s.sample_rate,
        s.variance,
        s.jitter,
        s.is_pet,
        json::escape(&s.security_mode),
        s.state,
        s.placement.to_json(),
        routine_json(),
        neighbor_json(&s.neighbor, &s.home_mac),
        multilink_json(s),
        calib_json(&s.calib),
        pilot_json(),
        recent_events_json(s, 15),
        counters_json(s),
        s.last_fall_bpm,
        s.last_fall_confidence
    )
}

pub fn pilot_json() -> String {
    "{\"active_household_id\":\"H01\",\"enrolled_households\":1,\"total_alarms\":0,\"true_alarms\":0,\"false_alarms\":0,\"false_alarm_rate\":0.0,\"avg_response_seconds\":0.0,\"challenge_intercepts\":0}".into()
}

pub fn events_array(records: &[EventRecord]) -> String {
    let items: Vec<String> = records.iter().map(|e| e.to_json()).collect();
    format!("[{}]", items.join(","))
}
