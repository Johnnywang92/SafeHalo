// Local wall-clock formatting without libc/tzfile parsing.
// The UTC offset is read from `date +%z` (busybox/GNU/BSD all support it) and
// cached for 30 minutes; civil-date conversion is pure arithmetic.

use std::sync::Mutex;
use std::time::{Instant, SystemTime, UNIX_EPOCH};

struct OffsetCache {
    offset_secs: i64,
    fetched: Option<Instant>,
}

static OFFSET: Mutex<OffsetCache> = Mutex::new(OffsetCache {
    offset_secs: 0,
    fetched: None,
});

fn query_offset() -> i64 {
    let out = std::process::Command::new("date").arg("+%z").output();
    let text = match out {
        Ok(o) if o.status.success() => String::from_utf8_lossy(&o.stdout).trim().to_string(),
        _ => return 0,
    };
    // +0800 / -0530
    let b = text.as_bytes();
    if b.len() != 5 || (b[0] != b'+' && b[0] != b'-') {
        return 0;
    }
    let hh: i64 = text[1..3].parse().unwrap_or(0);
    let mm: i64 = text[3..5].parse().unwrap_or(0);
    let v = hh * 3600 + mm * 60;
    if b[0] == b'-' {
        -v
    } else {
        v
    }
}

pub fn local_offset_secs() -> i64 {
    let mut c = OFFSET.lock().unwrap_or_else(|e| e.into_inner());
    let stale = match c.fetched {
        Some(t) => t.elapsed().as_secs() >= 1800,
        None => true,
    };
    if stale {
        c.offset_secs = query_offset();
        c.fetched = Some(Instant::now());
    }
    c.offset_secs
}

fn civil_from_days(z: i64) -> (i64, u32, u32) {
    let z = z + 719_468;
    let era = if z >= 0 { z } else { z - 146_096 } / 146_097;
    let doe = z - era * 146_097;
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
    let y = yoe + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = (doy - (153 * mp + 2) / 5 + 1) as u32;
    let m = if mp < 10 { mp + 3 } else { mp - 9 } as u32;
    (if m <= 2 { y + 1 } else { y }, m, d)
}

pub struct Stamp {
    pub date_time: String, // 2026-11-11 09:30:00.123
    pub time_short: String, // 09:30:00
    pub iso: String,       // 2026-11-11T09:30:00
}

pub fn format_epoch(secs: f64) -> Stamp {
    let off = local_offset_secs();
    let total = secs.floor() as i64 + off;
    let days = total.div_euclid(86_400);
    let sod = total.rem_euclid(86_400);
    let (y, m, d) = civil_from_days(days);
    let hh = sod / 3600;
    let mm = (sod % 3600) / 60;
    let ss = sod % 60;
    let ms = ((secs.fract() * 1000.0).round() as i64).clamp(0, 999);
    Stamp {
        date_time: format!("{:04}-{:02}-{:02} {:02}:{:02}:{:02}.{:03}", y, m, d, hh, mm, ss, ms),
        time_short: format!("{:02}:{:02}:{:02}", hh, mm, ss),
        iso: format!("{:04}-{:02}-{:02}T{:02}:{:02}:{:02}", y, m, d, hh, mm, ss),
    }
}

pub fn now_epoch() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0)
}

pub fn now() -> Stamp {
    format_epoch(now_epoch())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn civil_epoch() {
        let (y, m, d) = civil_from_days(0);
        assert_eq!((y, m, d), (1970, 1, 1));
        let (y, m, d) = civil_from_days(20_000);
        assert_eq!((y, m, d), (2024, 10, 4));
    }
}
