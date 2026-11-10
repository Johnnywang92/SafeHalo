#!/usr/bin/env python3
"""Feed a replay subset through a pseudo-terminal into the daemon's --serial input.

Usage: python3 pty_send.py [SUBSET_FILE] [OUT_DIR]
"""
import os
import pty
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
DAEMON = HERE.parent / "target" / "release" / "safehalo-daemon"
SUBSET = sys.argv[1] if len(sys.argv) > 1 else "/tmp/fh_bench/eq/udp_subset.txt"
OUT = sys.argv[2] if len(sys.argv) > 2 else "/tmp/fh_bench/eq/pty"
RATE = 2000.0

os.makedirs(OUT, exist_ok=True)
storage = os.path.join(OUT, "storage")
os.makedirs(storage, exist_ok=True)
model = os.path.join(storage, "baseline_model.json")
if os.path.exists(model):
    os.remove(model)

master, slave = pty.openpty()
slave_name = os.ttyname(slave)
proc = subprocess.Popen(
    [str(DAEMON), "--serial", slave_name, "--baud", "115200", "--no-http",
     "--storage-dir", storage,
     "--trace", os.path.join(OUT, "trace.jsonl"),
     "--snapshot", os.path.join(OUT, "snapshot.json"),
     "--run-secs", "60"],
    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
)
time.sleep(1.0)  # let stty + open finish before feeding data
lines = [l for l in open(SUBSET, encoding="utf-8") if l.strip()]
start = time.monotonic()
for i, line in enumerate(lines):
    os.write(master, line.encode("utf-8"))
    dt = (i + 1) / RATE - (time.monotonic() - start)
    if dt > 0:
        time.sleep(dt)
os.write(master, b"EOF\n")
out, _ = proc.communicate(timeout=15)
print(out.strip())
os.close(master)
os.close(slave)
sys.exit(proc.returncode)