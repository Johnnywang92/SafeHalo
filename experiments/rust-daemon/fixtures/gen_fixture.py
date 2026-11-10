#!/usr/bin/env python3
"""Deterministic golden-fixture generator for the Python <-> Rust equivalence suite.

Provenance
----------
Every fixture emitted here is fully synthetic. No recording from a real home is
used, so no BSSID, timestamp or room layout in these files identifies anyone.
The signal model is the one already validated end-to-end against the production
engine by verify/gen_replay.py (same per-subcarrier amplitude model, same 50 Hz
cadence, same neighbour / random-MAC mix), truncated to a few thousand packets
so the generated text stays well under 1 MB and the golden trace + snapshot stay
a few kilobytes.

Real captures taken with scripts/csi_recorder.py may only be added after
scrubbing: replace every BSSID with a documentation-range address, rebase
timestamps onto BASE_US below, and confirm nothing left in the file reveals the
home layout. Until such a capture exists this generator stays synthetic-only.

Reproducibility comes from a fixed seed and a single sequential RNG stream, so
the output is byte-identical on any CPython 3.x. verify/run_fixtures.sh checks
the sha256 of every generated file against fixtures/CHECKSUMS before running.

Usage: python3 fixtures/gen_fixture.py [OUT_DIR]     (default fixtures/generated)
       python3 fixtures/gen_fixture.py --list
"""
import hashlib
import math
import random
import sys
from pathlib import Path

SEED = 20261110
RATE_HZ = 50.0
PKT_INTERVAL_US = int(1_000_000 / RATE_HZ)  # 20 ms
JITTER_US = 1500                            # +/- 1.5 ms router-side jitter
SUBCARRIERS = 52
BASE_US = 1_700_000_000_000_000             # synthetic epoch, not a real date

HOME = "c0:06:c3:11:22:33"
NEIGHBORS = ["aa:bb:cc:dd:ee:01", "d8:32:14:77:88:02"]
RANDOM_MACS = ["02:11:22:33:44:55", "06:aa:bb:cc:dd:ee"]

# Amplitude model constants (same shape as verify/gen_replay.py).
MOTION_AMP = 6.0        # per-subcarrier locomotion swing
BREATH_AMP = 0.8        # per-subcarrier chest micro-motion swing
NOISE_SIGMA = 0.6       # additive RF noise, also the source of quantization peaks
BASE_AMP = 24.0         # static multipath floor
BASE_AMP_SWING = 9.0    # per-subcarrier floor variation
# Locomotion doppler phase step per packet. gen_replay.py uses i/7.0, which at
# 50 Hz advances only ~0.14 rad per frame and leaves the walk phase stuck in
# STATIC; i/4.0 (~2 Hz limb doppler) reaches MOVING through the normal
# is_moving path without tripping the high-impact clause on every frame.
MOTION_PHASE_DIV = 4.0

# Each fixture: total packet count plus a phase table of
# (end_index_exclusive, walk, still, spike) where
#   walk  -> locomotion amplitude multiplier (0 = not walking)
#   still -> chest micro-motion multiplier (0.15 = lying still and breathing)
#   spike -> impact amplitude multiplier (a short violent transient)
FIXTURES = {
    # 48 s: empty-room calibration -> walking -> impact -> lying on the floor.
    # Covers CALIBRATING/EMPTY/MOVING/FALL_SUSPECT/FALL_CONFIRMED and the whole
    # targeted-inspection chain (burst request, respiration scan, challenge).
    "main": {
        "packets": 2400,
        "phases": [
            (250, 0.0, 0.0, 0.0),     #  0-5 s   empty room, sets the baseline
            (750, 6.0, 1.0, 0.0),     #  5-15 s  walking across the room
            (762, 0.0, 1.0, 6.0),     # 15 s     impact transient, 12 packets
            (2400, 0.0, 0.15, 0.0),   # 15-48 s  down and still, chest motion
        ],
    },
}


def phase_at(phases, i):
    for end, walk, still, spike in phases:
        if i < end:
            return walk, still, spike
    return phases[-1][1], phases[-1][2], phases[-1][3]


def mac_for(rng):
    r = rng.random()
    if r < 0.86:
        return HOME, rng.randint(-58, -38)
    if r < 0.95:
        return rng.choice(NEIGHBORS), rng.randint(-92, -76)
    return rng.choice(RANDOM_MACS), rng.randint(-70, -50)


def write_fixture(name, spec, out_dir):
    rng = random.Random(SEED)
    path = out_dir / f"{name}.txt"
    phases = spec["phases"]
    with path.open("w", encoding="utf-8") as out:
        for i in range(spec["packets"]):
            mac, rssi = mac_for(rng)
            walk, still, spike = phase_at(phases, i)
            ts_us = BASE_US + i * PKT_INTERVAL_US + rng.randint(-JITTER_US, JITTER_US)

            vals = []
            for k in range(SUBCARRIERS):
                base = BASE_AMP + BASE_AMP_SWING * math.sin(k / 8.0)
                motion = walk * MOTION_AMP * math.sin(i / MOTION_PHASE_DIV + k / 5.0)
                breath = still * BREATH_AMP * math.sin(i / 25.0 + k / 9.0)
                amp = base + motion + breath + spike * math.sin(k / 3.0) + rng.gauss(0, NOISE_SIGMA)
                amp = max(2.0, min(120.0, amp))
                phase = 0.3 * math.sin(k / 6.0) + i / 500.0 + rng.gauss(0, 0.05)
                vals.append(int(amp * math.sin(phase)))  # imaginary
                vals.append(int(amp * math.cos(phase)))  # real

            fields = ["CSI_DATA", "0", str(i & 0xFFFF), mac, str(rssi), "11", "1", "7",
                      "1", "1", "0", "0", "0", "0", "1", "-95", "0", "6", "0",
                      str(ts_us), "0", "104", "0", "104", "0"]
            out.write(",".join(fields) + ",[" + ", ".join(str(v) for v in vals) + "]\n")
    return path


def sha256(path):
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--list":
        for name, spec in FIXTURES.items():
            secs = spec["packets"] / RATE_HZ
            print(f"{name}: {spec['packets']} packets ({secs:.0f} s @ {RATE_HZ:.0f} Hz)")
        return

    out_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).parent / "generated"
    out_dir.mkdir(parents=True, exist_ok=True)
    for name, spec in FIXTURES.items():
        path = write_fixture(name, spec, out_dir)
        print(f"{sha256(path)}  {path.name}")


if __name__ == "__main__":
    main()
