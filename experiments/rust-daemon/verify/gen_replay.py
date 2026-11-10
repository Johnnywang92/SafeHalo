#!/usr/bin/env python3
"""Deterministic CSI replay generator (serial CSI_DATA lines, 50 Hz, 40 min)."""
import math
import random
import sys

rng = random.Random(42)
N = 120_000
HOME = "c0:06:c3:11:22:33"
NEIGHBORS = ["aa:bb:cc:dd:ee:01", "d8:32:14:77:88:02"]
RANDOM_MACS = ["02:11:22:33:44:55", "06:aa:bb:cc:dd:ee"]
BASE_US = 1_700_000_000_000_000

out = open(sys.argv[1] if len(sys.argv) > 1 else "/tmp/fh_bench/replay.txt", "w")
for i in range(N):
    r = rng.random()
    if r < 0.86:
        mac, rssi = HOME, rng.randint(-58, -38)
    elif r < 0.95:
        mac, rssi = rng.choice(NEIGHBORS), rng.randint(-92, -76)
    else:
        mac, rssi = rng.choice(RANDOM_MACS), rng.randint(-70, -50)

    ts_us = BASE_US + i * 20_000 + rng.randint(-1500, 1500)
    walk = 1.0 if (i % 20_000) < 3_000 else 0.0
    fall_spike = 6.0 if 60_000 <= i < 60_012 else 0.0
    still = 0.15 if 60_012 <= i < 62_000 else 1.0

    vals = []
    for k in range(52):
        base = 24.0 + 9.0 * math.sin(k / 8.0)
        motion = walk * 6.0 * math.sin(i / 7.0 + k / 5.0)
        breath = still * 0.8 * math.sin(i / 25.0 + k / 9.0)
        amp = base + motion + breath + fall_spike * math.sin(k / 3.0) + rng.gauss(0, 0.6)
        amp = max(2.0, min(120.0, amp))
        phase = 0.3 * math.sin(k / 6.0) + i / 500.0 + rng.gauss(0, 0.05)
        real = int(amp * math.cos(phase))
        imag = int(amp * math.sin(phase))
        vals.append(imag)
        vals.append(real)

    fields = ["CSI_DATA", "0", str(i & 0xFFFF), mac, str(rssi), "11", "1", "7", "1", "1",
              "0", "0", "0", "0", "1", "-95", "0", "6", "0", str(ts_us),
              "0", "104", "0", "104", "0"]
    out.write(",".join(fields) + ",[" + ", ".join(str(v) for v in vals) + "]\n")
out.close()
print(f"wrote {N} lines")