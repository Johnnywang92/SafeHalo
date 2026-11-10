#!/usr/bin/env python3
"""Paced UDP sender + live HTTP contract checks against the Rust daemon."""
import json
import socket
import sys
import time
import urllib.request

SUBSET = sys.argv[1] if len(sys.argv) > 1 else "/tmp/fh_bench/eq/udp_subset.txt"
DEST = ("127.0.0.1", 9911)
RATE = 2000.0

HA_KEYS = {
    "fall_detected", "presence", "motion", "pet_activity", "human_state",
    "fused_state", "radar_height_m", "respiration_bpm", "confidence",
    "decision_reason", "routine_has_anomaly", "wake_time", "bed_time",
    "night_restless_count",
}

sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
lines = [l.rstrip("\n") for l in open(SUBSET, encoding="utf-8") if l.strip()]
start = time.monotonic()
checked = False
for i, line in enumerate(lines):
    sock.sendto(line.encode("utf-8"), DEST)
    if not checked and i == 6000:
        checked = True
        for path in ("/api/ha/status", "/api/state", "/api/neighbor_spectrum"):
            with urllib.request.urlopen("http://127.0.0.1:8091" + path, timeout=3) as r:
                body = json.loads(r.read().decode("utf-8"))
                print(f"GET {path} -> HTTP {r.status}, keys={len(body)}")
                if path == "/api/ha/status":
                    assert set(body) == HA_KEYS, f"HA key mismatch: {set(body) ^ HA_KEYS}"
                    print("  ha payload:", json.dumps(body, ensure_ascii=False))
        print("HTTP contract checks passed (ha/status key set identical to web_portal)")
    target = (i + 1) / RATE
    dt = target - (time.monotonic() - start)
    if dt > 0:
        time.sleep(dt)
sock.sendto(b"EOF", DEST)
sock.sendto(b"EOF", DEST)
print(f"sent {len(lines)} lines + EOF in {time.monotonic()-start:.2f}s")