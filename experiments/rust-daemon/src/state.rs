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
use crate::serial::DeviceRegistry;
use crate::timefmt;

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

#[derive(Clone, Debug)]
pub struct TunedParams {
    pub fall_spike_threshold: f64,
    pub fall_stillness_seconds: f64,
    pub pet_spr_threshold: f64,
    pub radar_floor_height_thresh: f64,
    pub hampel_window_size: usize,
    pub water_flow_damping: bool,
    pub metal_multipath_damping: bool,
    pub description: String,
    pub friendly_summary: String,
}

impl TunedParams {
    #[allow(clippy::too_many_arguments)]
    pub fn compute_for_full(
        room_type: &str,
        mount_height: &str,
        mobility_pace: &str,
        living_arrangement: &str,
        has_pets: bool,
        has_water_splash: bool,
        enable_gentle_challenge: bool,
        care_nickname: &str,
        room_name: &str,
    ) -> Self {
        let r_type = room_type.trim().to_ascii_uppercase();
        let m_height = mount_height.trim().to_ascii_uppercase();
        let pace = mobility_pace.trim().to_ascii_uppercase();
        let living = living_arrangement.trim().to_ascii_uppercase();
        let nickname = if care_nickname.trim().is_empty() { "家里长辈" } else { care_nickname.trim() };
        let r_name = if room_name.trim().is_empty() { "客厅" } else { room_name.trim() };

        let mut spike: f64;
        let mut still: f64;
        let mut pet_spr: f64 = if has_pets { 0.45 } else { 0.30 };
        let mut floor_z: f64 = 0.38;
        let mut hampel: usize = 5;
        let mut water_damping = has_water_splash;
        let mut metal_damping = false;
        let mut desc_parts: Vec<String> = Vec::new();
        let mut friendly_parts: Vec<String> = Vec::new();

        match r_type.as_str() {
            "BATHROOM" => {
                spike = 1.15;
                still = 3.0;
                floor_z = 0.35;
                hampel = 7;
                water_damping = true;
                desc_parts.push("【卫生间极速防滑响应】: 跌倒冲击阈值调至 1.15，倒地静止 3 秒快速响应，启用花洒水流微动抑制".into());
                friendly_parts.push("卫浴防滑高敏关照与花洒水流免误扰".into());
            }
            "BEDROOM" => {
                spike = 1.40;
                still = 4.5;
                floor_z = 0.30;
                hampel = 5;
                desc_parts.push("【卧室床铺安全避让】: 地面高度阈值收紧至 0.30m (避免床上坐卧误判)，强化夜间起夜灵敏度".into());
                friendly_parts.push("床边起夜贴心保护（床上翻身坐卧不误报）".into());
            }
            "KITCHEN" => {
                spike = 1.85;
                still = 3.8;
                metal_damping = true;
                desc_parts.push("【厨房金属抗反弹】: 激活冰箱与油烟机多径共振吸收，平抑高频谐振".into());
                friendly_parts.push("厨房家电电磁反弹过滤".into());
            }
            "CORRIDOR" => {
                spike = 1.60;
                still = 3.5;
                desc_parts.push("【走廊线性通道】: 快速响应直行路径变化".into());
                friendly_parts.push("走廊过道快速通行感知".into());
            }
            _ => {
                spike = 2.10;
                still = 4.0;
                hampel = 7;
                desc_parts.push("【客厅大开间多径适应】: 扩大时域滤波抗窗帘抖动，优化多人走动场景".into());
                friendly_parts.push("客厅宽敞空间与窗帘微动防误扰".into());
            }
        }

        if m_height == "LOW" {
            spike *= 1.15;
            desc_parts.push("矮柜低位安装: 补偿地面多径反射放大效应".into());
        } else if m_height == "HIGH_CEILING" || m_height == "HIGH_WALL" {
            spike *= 0.95;
            if floor_z < 0.40 {
                floor_z = 0.40;
            }
            desc_parts.push("吸顶高位安装: 增强俯视垂向下坠特征".into());
        }

        if pace == "STEADY_SLOW" {
            spike *= 0.85;
            if still < 4.5 {
                still = 4.5;
            }
            desc_parts.push("稳健慢行模式: 下调冲击阈值 15% 以捕捉扶墙/顺床沿缓慢滑落，并留足缓慢起身时间".into());
            friendly_parts.push("开启「稳健慢行」软跌倒保护".into());
        }

        if living == "COHABIT" {
            if hampel < 7 {
                hampel = 7;
            }
            desc_parts.push("多人同住场景: 增强背景活动平滑".into());
            friendly_parts.push("双人/多人同住起居适配".into());
        }

        if has_pets {
            pet_spr = 0.50;
            desc_parts.push("家有猫狗: 提升 SPR 载波参与度过滤级别至 50%".into());
            friendly_parts.push("猫狗地面走动免打扰".into());
        }

        if enable_gentle_challenge {
            friendly_parts.push("疑似磕碰先发轻柔提示音（挥手即可取消）".into());
        }

        let desc = desc_parts.join(" | ");
        let friendly = format!("✨ 已为【{}】定制【{}】守护方案：{}", nickname, r_name, friendly_parts.join("、"));
        TunedParams {
            fall_spike_threshold: ((spike * 100.0).round()) / 100.0,
            fall_stillness_seconds: ((still * 10.0).round()) / 10.0,
            pet_spr_threshold: ((pet_spr * 100.0).round()) / 100.0,
            radar_floor_height_thresh: ((floor_z * 100.0).round()) / 100.0,
            hampel_window_size: hampel,
            water_flow_damping: water_damping,
            metal_multipath_damping: metal_damping,
            description: desc,
            friendly_summary: friendly,
        }
    }

    pub fn compute_for(
        room_type: &str,
        mount_height: &str,
        mobility_pace: &str,
        has_water_splash: bool,
        care_nickname: &str,
        room_name: &str,
    ) -> Self {
        Self::compute_for_full(
            room_type,
            mount_height,
            mobility_pace,
            "SOLO",
            false,
            has_water_splash,
            true,
            care_nickname,
            room_name,
        )
    }

    pub fn to_json(&self) -> String {
        format!(
            "{{\"fall_spike_threshold\":{:.2},\"fall_stillness_seconds\":{:.1},\"pet_spr_threshold\":{:.2},\"radar_floor_height_thresh\":{:.2},\"hampel_window_size\":{},\"water_flow_damping\":{},\"metal_multipath_damping\":{},\"description\":\"{}\",\"friendly_summary\":\"{}\"}}",
            self.fall_spike_threshold,
            self.fall_stillness_seconds,
            self.pet_spr_threshold,
            self.radar_floor_height_thresh,
            self.hampel_window_size,
            self.water_flow_damping,
            self.metal_multipath_damping,
            json::escape(&self.description),
            json::escape(&self.friendly_summary)
        )
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
    pub tuned: TunedParams,
}

impl Default for Placement {
    fn default() -> Self {
        let tuned = TunedParams::compute_for("LIVING_ROOM", "MID_DESK", "BRISK", false, "家里长辈", "客厅");
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
            tuned,
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

                // Compute default tuned parameters based on loaded config
                p.recompute_tuned();

                // Load any persisted tuned parameter overrides
                if let Some(t_obj) = v.get("tuned_parameters") {
                    if let Some(sp) = t_obj.get("fall_spike_threshold").and_then(|x| x.as_f64()) {
                        p.tuned.fall_spike_threshold = sp;
                    }
                    if let Some(st) = t_obj.get("fall_stillness_seconds").and_then(|x| x.as_f64()) {
                        p.tuned.fall_stillness_seconds = st;
                    }
                    if let Some(pet) = t_obj.get("pet_spr_threshold").and_then(|x| x.as_f64()) {
                        p.tuned.pet_spr_threshold = pet;
                    }
                    if let Some(hampel) = t_obj.get("hampel_window_size").and_then(|x| x.as_u64()) {
                        p.tuned.hampel_window_size = hampel as usize;
                    }
                    if let Some(w) = t_obj.get("water_flow_damping").and_then(|x| x.as_bool()) {
                        p.tuned.water_flow_damping = w;
                    }
                    if let Some(m) = t_obj.get("metal_multipath_damping").and_then(|x| x.as_bool()) {
                        p.tuned.metal_multipath_damping = m;
                    }
                }
            }
        }
        if !room_override.is_empty() && (!p.is_configured || room_override != "客厅") {
            p.room_name = room_override.to_string();
        }
        if let Some(c) = challenge_override {
            if !p.is_configured {
                p.enable_gentle_challenge = c;
            }
        }
        p
    }

    pub fn recompute_tuned(&mut self) {
        self.tuned = TunedParams::compute_for_full(
            &self.room_type,
            &self.mount_height,
            &self.mobility_pace,
            &self.living_arrangement,
            self.has_pets,
            self.has_water_splash,
            self.enable_gentle_challenge,
            &self.care_nickname,
            &self.room_name,
        );
    }

    pub fn apply_false_alarm_feedback(&mut self, category: &str) -> String {
        let msg = match category {
            "pet" => {
                self.tuned.pet_spr_threshold = ((self.tuned.pet_spr_threshold + 0.05) * 100.0).round() / 100.0;
                if self.tuned.pet_spr_threshold > 0.65 {
                    self.tuned.pet_spr_threshold = 0.65;
                }
                format!(
                    "已针对【宠物跑跳/猫狗干扰】将宠物载波抑制阈值提升至 {:.2}",
                    self.tuned.pet_spr_threshold
                )
            }
            "shower_water" => {
                self.tuned.water_flow_damping = true;
                self.tuned.hampel_window_size = self.tuned.hampel_window_size.max(7);
                "已针对【卫生间淋浴花洒水流】开启水流微动低通阻尼滤波".to_string()
            }
            "curtain_fan" => {
                self.tuned.hampel_window_size = (self.tuned.hampel_window_size + 2).min(11);
                self.tuned.fall_spike_threshold = ((self.tuned.fall_spike_threshold * 1.08) * 100.0).round() / 100.0;
                if self.tuned.fall_spike_threshold > 3.5 {
                    self.tuned.fall_spike_threshold = 3.5;
                }
                format!(
                    "已针对【窗帘飘动/风扇空调摆风】扩大时域滤波窗至 {} 并上调冲击阈值至 {:.2}",
                    self.tuned.hampel_window_size, self.tuned.fall_spike_threshold
                )
            }
            _ => {
                self.tuned.fall_spike_threshold = ((self.tuned.fall_spike_threshold * 1.10) * 100.0).round() / 100.0;
                if self.tuned.fall_spike_threshold > 3.5 {
                    self.tuned.fall_spike_threshold = 3.5;
                }
                self.tuned.fall_stillness_seconds = ((self.tuned.fall_stillness_seconds + 0.5) * 10.0).round() / 10.0;
                if self.tuned.fall_stillness_seconds > 8.0 {
                    self.tuned.fall_stillness_seconds = 8.0;
                }
                format!(
                    "已针对【{}】上调冲击阈值至 {:.2}、静止确认至 {:.1}s",
                    category, self.tuned.fall_spike_threshold, self.tuned.fall_stillness_seconds
                )
            }
        };
        self.tuned.description = format!("{} | [已根据误报反馈自适应调优]", self.tuned.description);
        msg
    }

    pub fn save(&self, dir: &str) {
        let _ = fs::create_dir_all(dir);
        let path = PathBuf::from(dir).join("placement_config.json");
        let text = format!(
            "{{\n  \"room_type\": \"{}\",\n  \"room_name\": \"{}\",\n  \"mount_height\": \"{}\",\n  \"room_area_sqm\": {},\n  \"has_pets\": {},\n  \"has_water_splash\": {},\n  \"care_nickname\": \"{}\",\n  \"living_arrangement\": \"{}\",\n  \"mobility_pace\": \"{}\",\n  \"wake_habit\": \"{}\",\n  \"enable_gentle_challenge\": {},\n  \"is_configured\": {},\n  \"tuned_parameters\": {}\n}}\n",
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
            self.is_configured,
            self.tuned.to_json()
        );
        let tmp = path.with_extension("json.tmp");
        if fs::write(&tmp, text).is_ok() {
            #[cfg(unix)]
            {
                use std::os::unix::fs::PermissionsExt;
                let _ = fs::set_permissions(&tmp, fs::Permissions::from_mode(0o600));
            }
            let _ = fs::rename(&tmp, &path);
        }
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            let _ = fs::set_permissions(&path, fs::Permissions::from_mode(0o600));
        }
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
        self.tuned.to_json()
    }
}

#[derive(Default)]
pub struct Control {
    pub trigger_inspect: bool,
    pub ack_safe: bool,
    pub recalibrate: bool,
    pub pending_commands: Vec<String>,
}

pub struct Shared {
    pub state: String,
    pub is_pet: bool,
    pub spr: f64,
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
    pub devices: DeviceRegistry,
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

    #[allow(dead_code)]
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
        "{{\"fall_detected\":{},\"presence\":{},\"motion\":{},\"pet_activity\":{},\"human_state\":\"{}\",\"fused_state\":\"{}\",\"security_mode\":\"{}\",\"radar_height_m\":0.0,\"respiration_bpm\":0.0,\"confidence\":1.0,\"decision_reason\":\"CSI-only Rust daemon\",\"routine_has_anomaly\":false,\"wake_time\":\"\",\"bed_time\":\"\",\"night_restless_count\":0}}",
        fall, presence, motion, s.is_pet, s.state, s.state, json::escape(&s.security_mode)
    )
}

pub fn health_json(s: &Shared) -> String {
    let now = timefmt::now_epoch();
    let uptime = (now - s.started_at).max(0.0);
    format!(
        "{{\"status\":\"healthy\",\"uptime_seconds\":{:.1},\"state\":\"{}\",\"security_mode\":\"{}\",\"packets\":{},\"fall_alarms\":{},\"transitions\":{}}}",
        uptime,
        json::escape(&s.state),
        json::escape(&s.security_mode),
        s.packets,
        s.fall_alarms,
        s.transitions
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
    "{\"enabled\":false,\"supported\":false,\"wake_time\":\"尚未起床\",\"bed_time\":\"待定\",\"typical_wake\":\"07:30\",\"typical_bed\":\"22:30\",\"night_restless_count\":0,\"total_active_minutes\":0.0,\"hourly_activity\":[".to_string()
        + &hours
        + "],\"active_anomalies\":[],\"has_anomaly\":false,\"history\":[]}"
}

pub fn multilink_json(s: &Shared) -> String {
    format!(
        "{{\"enabled\":false,\"supported\":false,\"active_room\":\"{}\",\"participating_links\":1,\"fused_energy\":0.0,\"links\":{{}}}}",
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
        "{{\"state\":\"{}\",\"sample_rate\":{},\"variance\":{},\"jitter\":{},\"spr\":{:.4},\"is_pet\":{},\"security_mode\":\"{}\",\"radar_online\":false,\"radar_protocol\":\"unsupported\",\"radar_z\":0.0,\"radar_bpm\":0.0,\"fused_state\":\"{}\",\"fused_confidence\":1.0,\"fused_reason\":\"CSI单模态引擎(毫米波雷达未接入)\",\"placement\":{},\"routine\":{},\"neighbor_filter\":{},\"multilink\":{},\"auto_calibration\":{},\"pilot\":{},\"recent_events\":{},\"counters\":{},\"last_fall\":{{\"bpm\":{},\"confidence\":{}}}}}",
        s.state,
        s.sample_rate,
        s.variance,
        s.jitter,
        s.spr,
        s.is_pet,
        json::escape(&s.security_mode),
        s.state,
        s.placement.to_json(),
        routine_json(),
        neighbor_json(&s.neighbor, &s.home_mac),
        multilink_json(s),
        calib_json(&s.calib),
        pilot_json(s),
        recent_events_json(s, 15),
        counters_json(s),
        s.last_fall_bpm,
        s.last_fall_confidence
    )
}

pub fn pilot_json(s: &Shared) -> String {
    let total_alarms = s.incidents.items.iter().filter(|i| i.event_type == "FALL_ALARM").count();
    let true_alarms = s.incidents.items.iter().filter(|i| i.status == "CONFIRMED_TRUE").count();
    let false_alarms = s.incidents.items.iter().filter(|i| i.status == "FALSE_ALARM").count();
    let intercepts = s.incidents.items.iter().filter(|i| i.status == "CHALLENGE_INTERCEPTED").count();
    let fa_rate = if total_alarms > 0 { false_alarms as f64 / total_alarms as f64 } else { 0.0 };
    format!(
        "{{\"active_household_id\":\"{}\",\"enrolled_households\":5,\"total_alarms\":{},\"true_alarms\":{},\"false_alarms\":{},\"false_alarm_rate\":{:.2},\"avg_response_seconds\":0.0,\"challenge_intercepts\":{}}}",
        json::escape(&s.household_id),
        total_alarms,
        true_alarms,
        false_alarms,
        fa_rate,
        intercepts
    )
}

pub fn pilot_dashboard_json(s: &Shared) -> String {
    let now = timefmt::now();
    let month = if now.date_time.len() >= 7 { &now.date_time[..7] } else { "2026-11" };
    let total_alarms = s.incidents.items.iter().filter(|i| i.event_type == "FALL_ALARM").count();
    let true_alarms = s.incidents.items.iter().filter(|i| i.status == "CONFIRMED_TRUE").count();
    let false_alarms = s.incidents.items.iter().filter(|i| i.status == "FALSE_ALARM").count();
    let intercepts = s.incidents.items.iter().filter(|i| i.status == "CHALLENGE_INTERCEPTED").count();

    let households = [
        ("H01", "1号试点户（张奶奶·78岁独居）", "客厅、卫生间", false, 2),
        ("H02", "2号试点户（王阿姨·72岁独居携猫狗）", "客厅、主卧", true, 2),
        ("H03", "3号试点户（李爷爷夫妇·81岁/79岁）", "客厅、主卧、次卧", false, 2),
        ("H04", "4号试点户（赵大爷·84岁慢病虚弱）", "卧室", false, 2),
        ("H05", "5号试点户（陈奶奶·76岁老旧密集公寓）", "客厅、走廊", false, 2),
    ];
    let mut rows = Vec::new();
    let mut compliant_count = 0;
    for (h_id, name, rooms, has_pets, target_max) in households {
        let h_fa = s.incidents.items.iter().filter(|i| i.household_id == h_id && i.status == "FALSE_ALARM").count();
        let h_ta = s.incidents.items.iter().filter(|i| i.household_id == h_id && i.status == "CONFIRMED_TRUE").count();
        let h_ci = s.incidents.items.iter().filter(|i| i.household_id == h_id && i.status == "CHALLENGE_INTERCEPTED").count();
        let passed = h_fa <= target_max;
        if passed { compliant_count += 1; }
        rows.push(format!(
            "{{\"household_id\":\"{}\",\"name\":\"{}\",\"rooms\":\"{}\",\"has_pets\":{},\"month\":\"{}\",\"false_alarms_count\":{},\"true_alarms_count\":{},\"challenge_intercepted_count\":{},\"target_max_false_alarms\":{},\"passed_standard\":{}}}",
            h_id, json::escape(name), json::escape(rooms), has_pets, month, h_fa, h_ta, h_ci, target_max, passed
        ));
    }
    let avg_fa = false_alarms as f64 / 5.0;
    format!(
        "{{\"month\":\"{}\",\"active_household_id\":\"{}\",\"pilot_households_count\":5,\"compliant_households_count\":{},\"avg_monthly_false_alarms_per_household\":{:.2},\"total_alarms\":{},\"total_false_alarms\":{},\"total_true_alarms\":{},\"total_challenge_intercepts\":{},\"pilot_overall_passed\":{},\"households\":[{}]}}",
        month,
        json::escape(&s.household_id),
        compliant_count,
        avg_fa,
        total_alarms,
        false_alarms,
        true_alarms,
        intercepts,
        compliant_count == 5,
        rows.join(",")
    )
}

pub fn pilot_markdown_report(s: &Shared) -> String {
    let now = timefmt::now();
    let month = if now.date_time.len() >= 7 { &now.date_time[..7] } else { "2026-11" };
    let false_alarms = s.incidents.items.iter().filter(|i| i.status == "FALSE_ALARM").count();
    let true_alarms = s.incidents.items.iter().filter(|i| i.status == "CONFIRMED_TRUE").count();
    let intercepts = s.incidents.items.iter().filter(|i| i.status == "CHALLENGE_INTERCEPTED").count();

    let households = [
        ("H01", "1号试点户（张奶奶·78岁独居）", "客厅、卫生间", 2),
        ("H02", "2号试点户（王阿姨·72岁独居携猫狗）", "客厅、主卧", 2),
        ("H03", "3号试点户（李爷爷夫妇·81岁/79岁）", "客厅、主卧、次卧", 2),
        ("H04", "4号试点户（赵大爷·84岁慢病虚弱）", "卧室", 2),
        ("H05", "5号试点户（陈奶奶·76岁老旧密集公寓）", "客厅、走廊", 2),
    ];
    let mut compliant = 0;
    let mut table_rows = String::new();
    for (h_id, name, rooms, target_max) in households {
        let h_fa = s.incidents.items.iter().filter(|i| i.household_id == h_id && i.status == "FALSE_ALARM").count();
        let h_ta = s.incidents.items.iter().filter(|i| i.household_id == h_id && i.status == "CONFIRMED_TRUE").count();
        let h_ci = s.incidents.items.iter().filter(|i| i.household_id == h_id && i.status == "CHALLENGE_INTERCEPTED").count();
        let passed = h_fa <= target_max;
        if passed { compliant += 1; }
        let status = if passed { "✅ 达标" } else { "⚠️ 超标需调优" };
        table_rows.push_str(&format!(
            "| `{}` | {} | {} | **{}** (目标 ≤{}) | {} | {} | {} |\n",
            h_id, name, rooms, h_fa, target_max, h_ci, h_ta, status
        ));
    }
    let avg_fa = false_alarms as f64 / 5.0;

    format!(
        "# SafeHalo 阶段 2 入户试点月度评估报告 ({})\n\n- **试点户数**: 5 户\n- **达标户数 (月误报 ≤ 2 次)**: {} / 5 户\n- **户均月度误报次数**: {:.2} 次/户/月\n- **本地质询窗口无感拦截次数**: {} 次\n- **真实告警确认次数**: {} 次\n\n| 户号 | 试点家庭画像 | 覆盖房间 | 当月误报次数 | 质询拦截次数 | 真实告警 | 考核结论 |\n|---|---|---|---|---|---|---|\n{}",
        month, compliant, avg_fa, intercepts, true_alarms, table_rows
    )
}

pub fn events_array(records: &[EventRecord]) -> String {
    let items: Vec<String> = records.iter().map(|e| e.to_json()).collect();
    format!("[{}]", items.join(","))
}
