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
