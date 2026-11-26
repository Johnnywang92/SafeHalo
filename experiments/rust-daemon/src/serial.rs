// Serial transport: read+write open, auto discovery and bounded command writes.

use std::fs::{File, OpenOptions};
use std::io::{Read, Write};
use std::os::unix::fs::OpenOptionsExt;
use std::time::Duration;

#[cfg(target_os = "macos")]
const O_NONBLOCK: i32 = 0x0004;
#[cfg(target_os = "linux")]
const O_NONBLOCK: i32 = 0x0800;

pub fn open(path: &str, baud: u32) -> Result<File, String> {
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
    OpenOptions::new()
        .read(true)
        .write(true)
        .custom_flags(O_NONBLOCK)
        .open(path)
        .map_err(|e| format!("cannot open serial {}: {}", path, e))
}

pub fn discover() -> Option<String> {
    let preferred = ["cu.usbmodem", "cu.usbserial", "ttyUSB", "ttyACM"];
    let entries = std::fs::read_dir("/dev").ok()?;
    let mut names: Vec<String> = entries
        .filter_map(|e| e.ok())
        .filter_map(|e| e.file_name().into_string().ok())
        .collect();
    names.sort();
    for p in preferred {
        if let Some(n) = names.iter().find(|n| n.starts_with(p)) {
            return Some(format!("/dev/{}", n));
        }
    }
    None
}

pub fn write_command(file: &mut File, cmd: &str) -> bool {
    let bytes = cmd.as_bytes();
    let mut off = 0usize;
    let mut stalls = 0u32;
    while off < bytes.len() {
        match file.write(&bytes[off..]) {
            Ok(0) => return false,
            Ok(n) => off += n,
            Err(ref e) if e.kind() == std::io::ErrorKind::WouldBlock => {
                stalls += 1;
                if stalls > 50 {
                    return false;
                }
                std::thread::sleep(Duration::from_millis(2));
            }
            Err(ref e) if e.kind() == std::io::ErrorKind::Interrupted => {}
            Err(_) => return false,
        }
    }
    let _ = file.flush();
    true
}

pub fn read_into(file: &mut File, buf: &mut [u8]) -> std::io::Result<usize> {
    file.read(buf)
}

#[allow(dead_code)]
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum DeviceRole {
    Sensor,
    Actuator,
    SensorActuator,
}

impl DeviceRole {
    pub fn as_str(&self) -> &'static str {
        match self {
            DeviceRole::Sensor => "sensor",
            DeviceRole::Actuator => "actuator",
            DeviceRole::SensorActuator => "sensor_actuator",
        }
    }
}

#[derive(Clone, Debug)]
pub struct DeviceInfo {
    pub id: String,
    pub device_type: String,
    pub role: DeviceRole,
    pub path: String,
    pub baud: u32,
    pub status: String,
    pub last_seen: f64,
}

impl DeviceInfo {
    pub fn to_json(&self) -> String {
        format!(
            "{{\"id\":\"{}\",\"type\":\"{}\",\"role\":\"{}\",\"path\":\"{}\",\"baud\":{},\"status\":\"{}\",\"last_seen\":{:.1}}}",
            crate::json::escape(&self.id),
            crate::json::escape(&self.device_type),
            self.role.as_str(),
            crate::json::escape(&self.path),
            self.baud,
            crate::json::escape(&self.status),
            self.last_seen
        )
    }
}

#[derive(Default, Clone)]
pub struct DeviceRegistry {
    pub devices: Vec<DeviceInfo>,
}

const MAX_REGISTERED_DEVICES: usize = 64;

impl DeviceRegistry {
    pub fn new() -> Self {
        Self { devices: Vec::new() }
    }

    pub fn register(&mut self, dev: DeviceInfo) {
        if let Some(existing) = self.devices.iter_mut().find(|d| d.id == dev.id) {
            *existing = dev;
        } else if self.devices.len() < MAX_REGISTERED_DEVICES {
            self.devices.push(dev);
        }
    }

    pub fn to_json(&self) -> String {
        let items: Vec<String> = self.devices.iter().map(|d| d.to_json()).collect();
        format!("{{\"devices\":[{}]}}", items.join(","))
    }
}

/// Strict whitelist for device commands (protect against arbitrary command injection)
pub fn is_command_allowed(cmd: &str) -> bool {
    let trimmed = cmd.trim();
    if let Some(rate_str) = trimmed.strip_prefix("BURST_RATE:") {
        return matches!(rate_str, "20" | "30" | "50" | "100");
    }
    if let Some(arg) = trimmed.strip_prefix("CHALLENGE_BEEP:") {
        return matches!(arg, "0" | "1");
    }
    if trimmed.starts_with("CHALLENGE_VOICE:") || trimmed.starts_with("PLAY_TTS:") {
        return true;
    }
    matches!(trimmed, "PING" | "REBOOT" | "STATUS" | "ALARM_SIREN")
}

#[cfg(test)]
pub mod tests {
    use super::*;

    #[test]
    pub fn test_device_registry_operations() {
        let mut reg = DeviceRegistry::new();
        let dev = DeviceInfo {
            id: "door_front".into(),
            device_type: "door".into(),
            role: DeviceRole::Sensor,
            path: "network".into(),
            baud: 0,
            status: "closed".into(),
            last_seen: 100.0,
        };
        reg.register(dev);
        assert_eq!(reg.devices.len(), 1);
        let json = reg.to_json();
        assert!(json.contains("door_front"));
        assert!(json.contains("closed"));
    }

    #[test]
    pub fn test_command_whitelist_expanded() {
        assert!(is_command_allowed("BURST_RATE:50"));
        assert!(is_command_allowed("CHALLENGE_BEEP:1"));
        assert!(is_command_allowed("CHALLENGE_VOICE:请问您摔倒了吗？"));
        assert!(is_command_allowed("PLAY_TTS:紧急告警"));
        assert!(is_command_allowed("ALARM_SIREN"));
        assert!(!is_command_allowed("rm -rf /"));
        assert!(!is_command_allowed("BURST_RATE:999"));
    }
}
